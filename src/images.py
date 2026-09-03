"""Turning a user-supplied image reference into a numpy array, safely.

Everything here runs in the *worker*, never in the web process: fetching a
remote URL is slow, blocking and attacker-influenced, so it stays off the
request path entirely.
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

from src.config import Settings, get_settings
from src.errors import DealWithItError

DATA_URI_RE = re.compile(r'^data:image/[\w.+-]+;base64,')
ALLOWED_SCHEMES = ('http', 'https')

#: The three rejections a caller can be told about verbatim. They are
#: constants because the web tier shows the same sentences in the live hint
#: under the URL field, and two wordings for one rule is a bug waiting to
#: happen.
SCHEME_ERROR = f'Only {" and ".join(ALLOWED_SCHEMES)} URLs are supported.'
NO_HOST_ERROR = 'The URL has no host.'


def non_public_error(host: str) -> str:
    return f'The host {host!r} resolves to a non-public address, which is not allowed.'


#: Hosts a shape-only check can rule out on sight. Deliberately not the real
#: guard: that one resolves the name, and resolving is exactly what the web
#: tier must not do. Anything this lets through is checked again in the worker.
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

    Feeds the hint under the URL field. A handler may not block on DNS, so
    this only catches what is visible in the string itself; returning None
    means "nothing obviously wrong", not "safe to fetch".
    """
    try:
        parts = urlparse(url)
        host = parts.hostname
    except ValueError:
        # A malformed IPv6 literal, e.g. "http://[oops/". urlparse raises on
        # some of those and defers the rest to .hostname, so both are here.
        return NO_HOST_ERROR
    if parts.scheme not in ALLOWED_SCHEMES:
        return SCHEME_ERROR
    if not host:
        return NO_HOST_ERROR
    if _PRIVATE_LITERAL.match(host) or host.endswith('.local'):
        return non_public_error(host)
    return None


class ImageSourceError(DealWithItError):
    """An image could not be obtained or decoded, for a reason worth showing
    to the caller. The message is user-facing, so keep it free of internals."""


def is_data_uri(value: str) -> bool:
    return DATA_URI_RE.match(value) is not None


def decode_data_uri(value: str) -> bytes:
    """Decode a ``data:image/...;base64,`` string into raw bytes."""
    if not is_data_uri(value):
        raise ImageSourceError('The base64 image must have a data URI header.')
    # Strip whitespace before the strict decode: some clients wrap long data
    # URIs, and validate=True would reject the newlines outright.
    payload = re.sub(r'\s+', '', DATA_URI_RE.sub('', value, count=1))
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageSourceError('The base64 image could not be decoded.') from exc


def _assert_public_address(host: str, settings: Settings) -> None:
    """Refuse hosts that resolve to anything but a public unicast address.

    Without this the API happily fetches ``http://169.254.169.254/`` or
    anything else reachable from inside the container network on a caller's
    behalf. Note this validates the DNS answer we see now; a rebinding
    attacker could still return a different address to the socket that
    follows. Blocking that properly means pinning the connection to the
    validated IP, which breaks TLS hostname verification -- out of scope.
    """
    if settings.allow_private_addresses:
        return
    try:
        resolved = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ImageSourceError(f'The host {host!r} could not be resolved.') from exc

    for info in resolved:
        # is_global is False for loopback, private, link-local, multicast and
        # every other reserved range, which is exactly the set we want to bar.
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ImageSourceError(non_public_error(host))


def _validated_target(url: str, settings: Settings) -> str:
    parts = urlparse(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ImageSourceError(SCHEME_ERROR)
    if not parts.hostname:
        raise ImageSourceError(NO_HOST_ERROR)
    _assert_public_address(parts.hostname, settings)
    return urlunparse(parts)


def _read_capped(response: requests.Response, limit: int) -> bytes:
    """Read a response body, refusing to buffer more than ``limit`` bytes.

    The advertised Content-Length is only a hint, so we also stop short while
    streaming -- a lying or chunked response cannot make us allocate the disk.
    """
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
    """Download an image, with redirects re-validated and the body size capped."""
    settings = settings or get_settings()
    target = _validated_target(url, settings)

    for _ in range(settings.max_redirects + 1):
        try:
            response = requests.get(
                target,
                timeout=settings.http_timeout,
                stream=True,
                allow_redirects=False,
                headers={'user-agent': 'deal-with-it/1.0'},
            )
        except requests.RequestException as exc:
            raise ImageSourceError(f'The URL could not be fetched: {type(exc).__name__}.') from exc

        with response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get('location')
                if not location:
                    raise ImageSourceError('The URL redirected without a target.')
                # Re-validate every hop: the first URL being public says
                # nothing about where it points.
                target = _validated_target(urljoin(target, location), settings)
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
    """Decode image bytes into an EXIF-oriented RGB array.

    Replaces the abandoned ``image_to_numpy`` package: the only thing it did
    for us was honour the EXIF orientation tag, which Pillow does itself.
    """
    settings = settings or get_settings()
    if len(data) > settings.max_image_bytes:
        raise ImageSourceError(
            f'The image is larger than the {settings.max_image_bytes} byte limit.'
        )

    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageSourceError('The image could not be decoded.') from exc

    # open() only reads the header, so the dimensions are known before we
    # commit to allocating the decoded pixels.
    width, height = image.size
    if width * height > settings.max_image_pixels:
        raise ImageSourceError(
            f'The image has more than the {settings.max_image_pixels} pixel limit.'
        )

    try:
        image = ImageOps.exif_transpose(image)
        return np.asarray(image.convert('RGB'), dtype=np.uint8)
    except (OSError, ValueError) as exc:
        raise ImageSourceError('The image could not be decoded.') from exc


def load(url: str | None = None, base64_data: str | None = None,
         settings: Settings | None = None) -> NDArray[np.uint8]:
    """Resolve either source into pixels."""
    settings = settings or get_settings()
    if base64_data is not None:
        return to_rgb_array(decode_data_uri(base64_data), settings)
    if url is not None:
        return to_rgb_array(fetch_url(url, settings), settings)
    raise ImageSourceError('An url or a base64 string must be passed.')
