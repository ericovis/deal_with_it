"""The blob store: where a job's pictures live.

On one disk both tiers share, when there is one, or behind ``python -m
src.store`` when there is not: every public function here asks
:func:`_remote` first and, with ``DWI_BLOB_STORE_URL`` set, becomes a call
over HTTP/3 to that service, which runs the very same function's ``.local``
half on its own disk. The tests and the compose file use the disk; a
deployment that gives every service its own filesystem uses the service.

Web-safe on purpose. This module imports no imaging library -- no Pillow, no
numpy, no OpenCV -- so the web tier can mint paths, hand out URLs and store
opaque bytes without ever decoding an image. Everything that turns pixels into
files is in :mod:`src.derivatives`, which only the worker imports.
``tests/test_blobs.py`` pins that split.

A job owns one directory. That is what makes a job independently deletable,
which is the whole basis of :func:`reap`.
"""

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from uuid import uuid4

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from src.config import get_settings
from src.h3 import H3Client, H3Error

logger = logging.getLogger(__name__)

#: Where a blob is served from. Caddy serves this path off the same directory
#: in production; ``BlobFiles`` below is what answers in development.
PREFIX = '/i/'

#: No dots at all, in either half, so no relative path can be spelled.
_SAFE_ID = re.compile(r'\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_SAFE_NAME = re.compile(r'\A[a-z0-9_]+\.[a-z0-9]{2,4}\Z')


#: What a submitted media type is stored as. Never the client's own string:
#: these files are served straight off disk, so a `.svg` or an `.html` that
#: talked its way in would be served as itself. Anything unrecognised lands
#: as `.bin`, which is inert -- the worker decodes by content anyway.
EXTENSIONS = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
    'image/bmp': 'bmp',
    'image/tiff': 'tiff',
}
OPAQUE_EXTENSION = 'bin'
EXTENSIONS_TO_TYPES = {ext: kind for kind, ext in EXTENSIONS.items()} | {'json': 'application/json'}


def extension_for(media_type: str | None) -> str:
    return EXTENSIONS.get((media_type or '').split(';')[0].strip().lower(),
                          OPAQUE_EXTENSION)


class UnsafeReference(ValueError):
    """A job id or file name that will not be turned into a path."""


def _dispatched(function: Callable) -> Callable:
    """Send the call to the store service when there is one.

    The decorated body is the filesystem and stays reachable as
    ``function.local``, which is what :mod:`src.store` runs on the other
    side. Decided per call, not at import: the app is built once and the
    store's location is configuration.
    """
    @wraps(function)
    def wrapper(*args, **kwargs):
        remote = _remote()
        if remote is not None:
            return getattr(remote, function.__name__)(*args, **kwargs)
        return function(*args, **kwargs)
    wrapper.local = function
    return wrapper


def root() -> Path:
    """The directory holding every job's directory, created if need be."""
    settings = get_settings()
    path = Path(settings.blob_dir) if settings.blob_dir else Path('.data/blobs')
    path.mkdir(parents=True, exist_ok=True)
    return path


def ref(job_id: str, name: str) -> str:
    """``'<job_id>/<name>'`` -- how a blob is named everywhere else.

    Validation only: nothing here touches the disk, so an id that came off the
    wire can be checked without creating anything.
    """
    if not _SAFE_ID.match(job_id or ''):
        raise UnsafeReference(f'not a usable job id: {job_id!r}')
    if not _SAFE_NAME.match(name or ''):
        raise UnsafeReference(f'not a usable blob name: {name!r}')
    return f'{job_id}/{name}'


def split(reference: str) -> tuple[str, str]:
    """A reference back into its two validated halves."""
    job_id, _, name = (reference or '').partition('/')
    ref(job_id, name)
    return job_id, name


def job_dir(job_id: str) -> Path:
    if not _SAFE_ID.match(job_id or ''):
        raise UnsafeReference(f'not a usable job id: {job_id!r}')
    return root() / job_id


def path(reference: str) -> Path:
    """The absolute path a reference names. Both halves are validated, so a
    reference that came off the wire cannot escape the store."""
    job_id, name = split(reference)
    return job_dir(job_id) / name


def url(reference: str) -> str:
    """The URL a browser fetches a blob from."""
    return PREFIX + '/'.join(split(reference))


@_dispatched
def get(reference: str) -> bytes:
    """A blob's bytes. Raises ``OSError`` for one that is not there."""
    return path(reference).read_bytes()


@_dispatched
def read_json(job_id: str, name: str = 'meta.json') -> dict | None:
    """A job's own record of itself, or None once it has been swept.

    What the share page renders from: it outlives the Redis job, which
    expires at ``result_ttl``, and it dies exactly when the pictures do.
    """
    try:
        return json.loads(path(ref(job_id, name)).read_bytes())
    except (OSError, ValueError, UnsafeReference):
        return None


@_dispatched
def exists(reference: str) -> bool:
    try:
        return path(reference).is_file()
    except UnsafeReference:
        return False


@contextmanager
def open_for_write(job_id: str, name: str) -> Iterator[Path]:
    """Yield a temporary path, then move it into place atomically.

    The temp file is dot-prefixed and lives in the same directory as its
    destination, so ``os.replace`` is a rename within one filesystem: a reader
    sees the whole file or nothing, never half of one. Both servers refuse to
    serve a dot-prefixed name, so an in-flight write is never reachable.

    Deliberately no ``fsync``. Every byte here is derived and expires within
    the hour; paying a flush per derivative on a two-core host buys nothing
    that losing the file to a power cut would not also cost the job.
    """
    destination = path(ref(job_id, name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f'.{name}.{uuid4().hex}')
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


@_dispatched
def put(job_id: str, name: str, data: bytes) -> str:
    """Store bytes and return the reference. Opaque: nothing here reads them."""
    with open_for_write(job_id, name) as temporary:
        temporary.write_bytes(data)
    return ref(job_id, name)


@_dispatched
def link(source: str, job_id: str, name: str) -> str:
    """Point a second job's directory at an existing blob.

    A retry needs the picture the first attempt was given. Hard-linking rather
    than pointing the new job at the old directory is what keeps every
    directory independently deletable, which :func:`reap` depends on: the
    inode survives until the last link goes.
    """
    destination = path(ref(job_id, name))
    destination.parent.mkdir(parents=True, exist_ok=True)
    origin = path(source)
    try:
        os.link(origin, destination)
    except FileExistsError:
        pass
    except OSError:  # pragma: no cover - a store split across filesystems
        shutil.copyfile(origin, destination)
    return ref(job_id, name)


@_dispatched
def delete(reference: str) -> None:
    with suppress(UnsafeReference):
        path(reference).unlink(missing_ok=True)


@_dispatched
def forget(job_id: str) -> None:
    """Drop a job's whole directory."""
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


@dataclass(frozen=True)
class Entry:
    """One job's directory, as the sweep sees it."""

    job_id: str
    modified: float
    size: int


@_dispatched
def usage() -> list[Entry]:
    """Every job directory with its age and weight.

    The directory's own mtime moves on each ``os.replace`` into it, so it is
    exactly "when this job was last written".
    """
    entries = []
    try:
        listing = list(os.scandir(root()))
    except FileNotFoundError:  # pragma: no cover - root() makes it
        return []
    for item in listing:
        if not item.is_dir():
            continue
        try:
            size = sum(f.stat().st_size for f in os.scandir(item.path) if f.is_file())
            entries.append(Entry(item.name, item.stat().st_mtime, size))
        except FileNotFoundError:
            continue  # Swept by someone else between the listing and the stat.
    return entries


@_dispatched
def reap(ttl: float, max_bytes: int, min_age: float, now: float | None = None) -> list[str]:
    """Drop what has expired, then the oldest of what is left until the store
    fits. Returns the job ids that went.

    ``min_age`` is a floor no ceiling eviction may cross: a job that was
    written seconds ago may still be queued, and deleting its source would
    fail it. If the store is over its ceiling and everything in it is younger
    than that, the right answer is to say so loudly rather than eat a live
    job.
    """
    moment = time.time() if now is None else now
    entries = usage()
    doomed = [entry for entry in entries if moment - entry.modified > ttl]
    survivors = sorted((e for e in entries if e not in doomed), key=lambda e: e.modified)

    total = sum(entry.size for entry in survivors)
    for entry in survivors:
        if total <= max_bytes:
            break
        if moment - entry.modified < min_age:
            logger.error(
                'blob store is %.1f MB over its %.1f MB ceiling and the oldest job is '
                'only %.0fs old; leaving it alone',
                (total - max_bytes) / 1e6, max_bytes / 1e6, moment - entry.modified,
            )
            break
        doomed.append(entry)
        total -= entry.size

    for entry in doomed:
        shutil.rmtree(root() / entry.job_id, ignore_errors=True)
    if doomed:
        logger.info('swept %s job directories from the blob store', len(doomed))
    return [entry.job_id for entry in doomed]


class _Remote:
    """The same functions, asked of ``python -m src.store`` over HTTP/3.

    References are validated here before anything leaves, so a bad one
    raises :class:`UnsafeReference` exactly as the disk would, and the
    store's own 400 is a belt to this brace.
    """

    def __init__(self, url: str) -> None:
        self.client = H3Client(url)

    def _ask(self, method: str, path: str, body: bytes = b'',
             content_type: str | None = None):
        headers = [(b'content-type', content_type.encode())] if content_type else []
        return self.client.request(method, path, body, headers)

    def get(self, reference: str) -> bytes:
        response = self._ask('GET', '/' + '/'.join(split(reference)))
        if response.status == 404:
            raise FileNotFoundError(reference)
        if response.status != 200:
            raise OSError(f'store answered {response.status} for {reference}')
        return response.body

    def read_json(self, job_id: str, name: str = 'meta.json') -> dict | None:
        try:
            return json.loads(self.get(ref(job_id, name)))
        except (OSError, ValueError, UnsafeReference):
            return None

    def exists(self, reference: str) -> bool:
        try:
            return self._ask('HEAD', '/' + '/'.join(split(reference))).status == 200
        except (UnsafeReference, H3Error):
            return False

    def put(self, job_id: str, name: str, data: bytes) -> str:
        reference = ref(job_id, name)
        response = self._ask('PUT', '/' + reference, data, 'application/octet-stream')
        if response.status != 201:
            raise OSError(f'store refused {reference}: {response.status}')
        return reference

    def link(self, source: str, job_id: str, name: str) -> str:
        split(source)
        reference = ref(job_id, name)
        body = json.dumps({'source': source, 'job_id': job_id, 'name': name}).encode()
        response = self._ask('POST', '/link', body, 'application/json')
        if response.status == 404:
            raise FileNotFoundError(source)
        if response.status != 201:
            raise OSError(f'store would not link {source}: {response.status}')
        return reference

    def delete(self, reference: str) -> None:
        with suppress(UnsafeReference):
            self._ask('DELETE', '/' + '/'.join(split(reference)))

    def forget(self, job_id: str) -> None:
        if not _SAFE_ID.match(job_id or ''):
            raise UnsafeReference(f'not a usable job id: {job_id!r}')
        self._ask('DELETE', '/' + job_id)

    def usage(self) -> list[Entry]:
        response = self._ask('GET', '/usage')
        return [Entry(**entry) for entry in json.loads(response.body)]

    def reap(self, ttl: float, max_bytes: int, min_age: float,
             now: float | None = None) -> list[str]:
        body = json.dumps({'ttl': ttl, 'max_bytes': max_bytes, 'min_age': min_age}).encode()
        response = self._ask('POST', '/reap', body, 'application/json')
        if response.status != 200:
            raise OSError(f'store would not sweep: {response.status}')
        return json.loads(response.body)


_remotes: dict[str, _Remote] = {}
_remotes_lock = threading.Lock()
_thread = threading.local()


def serve_locally() -> None:
    """Make this thread the store: its calls stay on disk whatever the
    settings say. :func:`reap` asks :func:`usage`, and a store that asked
    itself over the network would wait on its own answer."""
    _thread.local_only = True


def _remote() -> _Remote | None:
    """The store service, if the settings name one. One client per URL, for
    the life of the process: it holds the QUIC connection."""
    if getattr(_thread, 'local_only', False):
        return None
    url = get_settings().blob_store_url
    if not url:
        return None
    with _remotes_lock:
        if url not in _remotes:
            _remotes[url] = _Remote(url)
        return _remotes[url]


def cache_control() -> str:
    """What both servers say about a blob.

    ``private`` because the picture is: a shared cache has no business keeping
    someone's photograph, and a shared *link* still works without it -- the
    recipient fetches from us. ``max-age`` tracks the store's own TTL, so an
    ``immutable`` URL can never outlive the file behind it.
    """
    return f'private, max-age={get_settings().blob_ttl}, immutable'


class BlobFiles(StaticFiles):
    """``/i`` for a bare uvicorn.

    In production Caddy serves this directory itself and has to repeat both
    rules below in its own ``handle /i/*`` block, **plus** ``nosniff`` -- which
    a response served from here gets for free from ``BodyLimit`` in
    :mod:`src.app`, and which Caddy bypasses entirely. Change one, change the
    other.

    Subclassing ``StaticFiles`` rather than writing a route: traversal
    hardening, ETags, conditional requests and ``Range`` all come with it.
    """

    def lookup_path(self, path: str) -> tuple[str, os.stat_result | None]:
        # Resolved per request, not at construction: the app is built once at
        # import and the store's location is configuration, so binding it here
        # is what lets DWI_BLOB_DIR mean something afterwards.
        self.all_directories = [root()]
        return super().lookup_path(path)

    async def get_response(self, path: str, scope) -> Response:
        if any(part.startswith('.') for part in path.split('/')):
            # A write that is still in flight. Not ours to serve.
            return Response('', status_code=404)
        if _remote() is not None:
            return await run_in_threadpool(self._fetched, path)
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            response.headers['cache-control'] = cache_control()
        return response

    @staticmethod
    def _fetched(path: str) -> Response:
        """The blob, by way of the store service. A hop and a copy per view,
        which at 320 KB a card is cheaper than teaching the edge to serve
        one directory of one container."""
        try:
            data = get(path)
        except (UnsafeReference, FileNotFoundError):
            return Response('', status_code=404)
        except (OSError, H3Error):
            logger.exception('the blob store did not answer for %s', path)
            return Response('', status_code=502)
        media_type = EXTENSIONS_TO_TYPES.get(path.rsplit('.', 1)[-1], 'application/octet-stream')
        return Response(data, media_type=media_type,
                        headers={'cache-control': cache_control()})
