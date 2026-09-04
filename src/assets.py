"""Content-hashed URLs for the files under ``src/static``.

A browser cannot tell a changed ``style.css`` from the one it has without
asking, and asking costs a round trip on every page view. Putting the
file's content hash in the URL lets the answer be "forever": ``?v=`` moves
only when the bytes move, so a deploy invalidates exactly what it changed
and nothing else.

It is a query string rather than a new filename because the same files are
served straight off disk by the reverse proxy in front of us, which knows
nothing about a manifest of renamed files and should not have to.
"""

from functools import cache
from hashlib import blake2b
from pathlib import Path
from stat import S_ISREG

from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / 'static'
STATIC_PREFIX = '/static/'

#: A prefix of the digest. Collisions between two of our own files would
#: have to be deliberate, and the URL stays readable.
VERSION_LENGTH = 10

#: What a versioned URL is worth: a year, and no revalidation, because the
#: URL changes when the file does.
IMMUTABLE = 'public, max-age=31536000, immutable'
#: What a bare one is worth. Short, and the ETag settles it from there.
REVALIDATE = 'public, max-age=300'


@cache
def _digest(path: str, mtime_ns: int, size: int) -> str:
    """Hash the file's bytes, once per version of the file.

    Keyed on mtime and size as well as the name, so an edit invalidates the
    entry: uvicorn's ``--reload`` watches Python, not stylesheets. Hashing
    everything a page touches is 8ms and a stat is 0.07ms, which is the
    whole reason for the cache. The kernel's inode timestamps are coarse,
    so two edits of the same length within a clock tick would look like
    one -- a deploy is a new file and a new process, so this only ever
    means an editor mid-keystroke.
    """
    return blake2b((STATIC_DIR / path).read_bytes(),
                   digest_size=VERSION_LENGTH).hexdigest()[:VERSION_LENGTH]


def version(url: str) -> str | None:
    """The content hash of the static file ``url`` points at, or None if
    there is no such file."""
    if not url.startswith(STATIC_PREFIX):
        return None
    relative = url[len(STATIC_PREFIX):]
    path = STATIC_DIR / relative
    try:
        stat = path.stat()
    except OSError:
        return None
    if not S_ISREG(stat.st_mode):
        return None
    return _digest(relative, stat.st_mtime_ns, stat.st_size)


def asset(url: str) -> str:
    """``/static/...`` with its content hash on it. A path that names no
    file is returned unchanged: a missing icon should 404 on its own, not
    take the page down with it."""
    stamp = version(url)
    return f'{url}?v={stamp}' if stamp else url


class VersionedStatic(StaticFiles):
    """``/static`` with the cache policy those URLs imply.

    In production the reverse proxy serves these files itself and sets the
    same two headers; this is what makes a bare ``uvicorn src.app:app``
    behave the same way.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            versioned = b'v=' in scope.get('query_string', b'')
            response.headers['cache-control'] = IMMUTABLE if versioned else REVALIDATE
        return response
