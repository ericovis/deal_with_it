"""Turning a user-supplied image reference into a numpy array, safely.

Everything here runs in the *worker*: fetching a remote URL is slow,
blocking and attacker-influenced, so it stays off the request path.
"""

import base64
import binascii
import ipaddress
import re
import socket
from io import BytesIO
from urllib.parse import urljoin, urlparse, urlunparse

import numpy as np
import requests
from numpy.typing import NDArray
from PIL import Image, ImageOps, UnidentifiedImageError
from requests.adapters import HTTPAdapter

from src.config import Settings, get_settings
from src.errors import DealWithItError

DATA_URI_RE = re.compile(r'^data:image/[\w.+-]+;base64,')
ALLOWED_SCHEMES = ('http', 'https')

#: Shown verbatim, and also used by the live hint under the URL field.
SCHEME_ERROR = f'Only {" and ".join(ALLOWED_SCHEMES)} URLs are supported.'
NO_HOST_ERROR = 'The URL has no host.'

#: NAT64 well-known prefix: the low 32 bits are an IPv4 address the gateway
#: will dial, which `is_global` alone does not look at.
_NAT64 = ipaddress.ip_network('64:ff9b::/96')


def non_public_error(host: str) -> str:
    return f'The host {host!r} resolves to a non-public address, which is not allowed.'


#: Hosts a shape-only check can rule out on sight. Not the real guard: that
#: one resolves the name, which a web handler must not do.
_PRIVATE_LITERAL = re.compile(
    r'\A(?:localhost|0\.0\.0\.0'
    r'|127(?:\.\d{1,3}){3}'
    r'|10(?:\.\d{1,3}){3}'
    r'|192\.168(?:\.\d{1,3}){2}'
    r'|169\.254(?:\.\d{1,3}){2}'
    r'|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\Z',
    re.IGNORECASE,
)


def shape_error(url: str) -> str | None:
    """What is wrong with a URL, judged without touching the network.

    None means "nothing obviously wrong", not "safe to fetch".
    """
    try:
        parts = urlparse(url)
        host = parts.hostname
    except ValueError:
        return NO_HOST_ERROR
    if parts.scheme not in ALLOWED_SCHEMES:
        return SCHEME_ERROR
    if not host:
        return NO_HOST_ERROR
    if _PRIVATE_LITERAL.match(host) or host.endswith('.local'):
        return non_public_error(host)
    return None


class ImageSourceError(DealWithItError):
    """An image could not be obtained or decoded. The message is user-facing."""


def is_data_uri(value: str) -> bool:
    return DATA_URI_RE.match(value) is not None


def decode_data_uri(value: str) -> bytes:
    """Decode a ``data:image/...;base64,`` string into raw bytes."""
    if not is_data_uri(value):
        raise ImageSourceError('The base64 image must have a data URI header.')
    # Some clients wrap long data URIs; validate=True would reject the newlines.
    payload = re.sub(r'\s+', '', DATA_URI_RE.sub('', value, count=1))
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageSourceError('The base64 image could not be decoded.') from exc


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF).is_global
    return ip.is_global


def resolve_public_address(host: str, settings: Settings) -> str:
    """Resolve ``host`` and return one address, refusing the whole name if any
    answer is loopback, private, link-local or otherwise reserved."""
    try:
        resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        # gaierror for an unknown name, and any other resolver failure too.
        raise ImageSourceError(f'The host {host!r} could not be resolved.') from exc
    addresses = [info[4][0] for info in resolved]
    if not addresses:
        raise ImageSourceError(f'The host {host!r} could not be resolved.')
    if not settings.allow_private_addresses:
        for address in addresses:
            if not is_public_address(address):
                raise ImageSourceError(non_public_error(host))
    return addresses[0]


class PinnedAdapter(HTTPAdapter):
    """Connects each host to the address that was validated for it.

    Without this, requests resolves the name a second time when it dials, and
    a DNS answer that changes between the check and the dial (rebinding)
    lands on an address that was never checked. TLS still verifies against
    the original hostname.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.pins: dict[str, str] = {}

    def pin(self, host: str, address: str) -> None:
        self.pins[host] = address

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        host = urlparse(request.url).hostname or ''
        address = self.pins.get(host)
        if proxies and proxies.get(urlparse(request.url).scheme):
            # A proxy does the resolving; pinning would bypass it.
            return super().get_connection_with_tls_context(request, verify, proxies, cert)
        if address is None:
            raise ImageSourceError(non_public_error(host))
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert)
        host_params['host'] = address
        if host_params['scheme'] == 'https':
            pool_kwargs['server_hostname'] = host
            pool_kwargs['assert_hostname'] = host
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def _validated_target(url: str, settings: Settings, adapter: PinnedAdapter) -> str:
    parts = urlparse(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ImageSourceError(SCHEME_ERROR)
    if not parts.hostname:
        raise ImageSourceError(NO_HOST_ERROR)
    adapter.pin(parts.hostname, resolve_public_address(parts.hostname, settings))
    return urlunparse(parts)


def _read_capped(response: requests.Response, limit: int) -> bytes:
    """Read a body, refusing to buffer more than ``limit`` bytes. The declared
    length is only a hint, so the stream is counted too."""
    declared = response.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > limit:
        raise ImageSourceError(f'The image is larger than the {limit} byte limit.')

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ImageSourceError(f'The image is larger than the {limit} byte limit.')
        chunks.append(chunk)
    return b''.join(chunks)


def fetch_url(url: str, settings: Settings | None = None) -> bytes:
    """Download an image, with every redirect hop re-validated and pinned,
    and the body size capped."""
    settings = settings or get_settings()
    adapter = PinnedAdapter()
    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    target = _validated_target(url, settings, adapter)

    with session:
        for _ in range(settings.max_redirects + 1):
            try:
                response = session.get(
                    target,
                    timeout=settings.http_timeout,
                    stream=True,
                    allow_redirects=False,
                    headers={'user-agent': 'deal-with-it/1.0'},
                )
            except requests.RequestException as exc:
                raise ImageSourceError(
                    f'The URL could not be fetched: {type(exc).__name__}.') from exc

            with response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get('location')
                    if not location:
                        raise ImageSourceError('The URL redirected without a target.')
                    target = _validated_target(urljoin(target, location), settings, adapter)
                    continue

                if response.status_code != 200:
                    raise ImageSourceError(
                        f'The URL is not accessible. HTTP status: {response.status_code}.'
                    )
                content_type = response.headers.get('content-type', '')
                if not content_type.startswith('image/'):
                    raise ImageSourceError('The URL is not an image.')
                return _read_capped(response, settings.max_image_bytes)

    raise ImageSourceError('The URL redirected too many times.')


def to_rgb_array(data: bytes, settings: Settings | None = None) -> NDArray[np.uint8]:
    """Decode image bytes into an EXIF-oriented RGB array."""
    return decode(data, settings)[0]


def decode(data: bytes, settings: Settings | None = None) -> tuple[NDArray[np.uint8], str]:
    """Decode image bytes into an EXIF-oriented RGB array, plus the format
    Pillow read them as (``'JPEG'``, ``'PNG'``, ...)."""
    settings = settings or get_settings()
    if len(data) > settings.max_image_bytes:
        raise ImageSourceError(
            f'The image is larger than the {settings.max_image_bytes} byte limit.'
        )

    try:
        image = Image.open(BytesIO(data))
    except Image.DecompressionBombError as exc:
        # Pillow's own ceiling, hit from the header before ours is checked.
        raise ImageSourceError(
            f'The image has more than the {settings.max_image_pixels} pixel limit.'
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageSourceError('The image could not be decoded.') from exc

    # open() reads only the header, so this runs before any pixels are allocated.
    width, height = image.size
    if width * height > settings.max_image_pixels:
        raise ImageSourceError(
            f'The image has more than the {settings.max_image_pixels} pixel limit.'
        )

    source_format = image.format or ''
    try:
        image = ImageOps.exif_transpose(image)
        return np.asarray(image.convert('RGB'), dtype=np.uint8), source_format
    except (OSError, ValueError) as exc:
        raise ImageSourceError('The image could not be decoded.') from exc


def load(url: str | None = None, base64_data: str | None = None,
         settings: Settings | None = None) -> tuple[NDArray[np.uint8], str]:
    """Resolve either source into pixels, plus the source format."""
    settings = settings or get_settings()
    if base64_data is not None:
        return decode(decode_data_uri(base64_data), settings)
    if url is not None:
        return decode(fetch_url(url, settings), settings)
    raise ImageSourceError('An url or a base64 string must be passed.')
