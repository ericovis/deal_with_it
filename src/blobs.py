"""The blob store: where a job's pictures live, on a disk both tiers share.

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
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from src.config import get_settings

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


def extension_for(media_type: str | None) -> str:
    return EXTENSIONS.get((media_type or '').split(';')[0].strip().lower(),
                          OPAQUE_EXTENSION)


class UnsafeReference(ValueError):
    """A job id or file name that will not be turned into a path."""


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


def read_json(job_id: str, name: str = 'meta.json') -> dict | None:
    """A job's own record of itself, or None once it has been swept.

    What the share page renders from: it outlives the Redis job, which
    expires at ``result_ttl``, and it dies exactly when the pictures do.
    """
    try:
        return json.loads(path(ref(job_id, name)).read_bytes())
    except (OSError, ValueError, UnsafeReference):
        return None


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


def put(job_id: str, name: str, data: bytes) -> str:
    """Store bytes and return the reference. Opaque: nothing here reads them."""
    with open_for_write(job_id, name) as temporary:
        temporary.write_bytes(data)
    return ref(job_id, name)


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


def delete(reference: str) -> None:
    with suppress(UnsafeReference):
        path(reference).unlink(missing_ok=True)


def forget(job_id: str) -> None:
    """Drop a job's whole directory."""
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


@dataclass(frozen=True)
class Entry:
    """One job's directory, as the sweep sees it."""

    job_id: str
    modified: float
    size: int


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
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            response.headers['cache-control'] = cache_control()
        return response
