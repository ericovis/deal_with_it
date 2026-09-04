"""Composition root: FastAPI on the outside, FastHTML mounted at ``/``.

FastAPI has to be the outer app because FastHTML's static-file catch-all
matches any path ending in an asset extension, ``/api/report.csv`` included.
"""

import logging
import os

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import blobs
from src.api import router as api_router
from src.assets import STATIC_DIR, VersionedStatic
from src.blobs import BlobFiles
from src.config import get_settings
from src.throttle import BodyTooLarge, too_large_response
from src.ui import create_ui

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)

SECURITY_HEADERS = {
    b'x-content-type-options': b'nosniff',
    b'x-frame-options': b'DENY',
    b'referrer-policy': b'strict-origin-when-cross-origin',
}


class BodyLimit:
    """Refuse a request body over ``max_request_bytes``, and stamp the
    security headers on every response.

    Checks the declared Content-Length first, then counts the bytes as the
    handler reads them, so a chunked upload with no declared length is cut
    off at the same limit instead of being buffered whole.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        limit = get_settings().max_request_bytes
        headers = dict(scope.get('headers') or [])
        declared = headers.get(b'content-length', b'').decode()
        if declared.isdigit() and int(declared) > limit:
            await too_large_response(limit)(scope, receive, self._stamping(send))
            return

        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body', b''))
                if seen > limit:
                    raise BodyTooLarge(limit)
            return message

        await self.app(scope, counting_receive, self._stamping(send))

    @staticmethod
    def _stamping(send: Send) -> Send:
        async def stamping_send(message: Message) -> None:
            if message['type'] == 'http.response.start':
                message['headers'] = [*message.get('headers', []), *SECURITY_HEADERS.items()]
            await send(message)
        return stamping_send


def create_app() -> FastAPI:
    app = FastAPI(
        title='Deal With It!',
        description='A Python API for creating "Deal With It"-like images.',
        version='2.0.0',
        docs_url='/api/docs',
        openapi_url='/api/openapi.json',
        redoc_url=None,
    )
    app.add_middleware(BodyLimit)
    app.include_router(api_router, prefix='/api')
    # StaticFiles rather than FastHTML's catch-all, which is a bare
    # FileResponse over the process CWD. The subclass adds the cache
    # headers the ?v= URLs in src/assets.py are worth.
    app.mount('/static', VersionedStatic(directory=STATIC_DIR), name='static')
    # A job's pictures. In production Caddy serves this directory itself and
    # repeats the same rules; this is what answers a bare uvicorn, and what
    # makes the Caddy block a performance change rather than a correctness
    # one -- without it, /i/* simply falls through to the app.
    app.mount('/i', BlobFiles(directory=blobs.root(), check_dir=False), name='blobs')
    app.mount('/', create_ui())
    return app


app = create_app()
