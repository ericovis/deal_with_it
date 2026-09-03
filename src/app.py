"""Composition root.

FastAPI is the outer app and FastHTML is mounted at ``/``. The other way
round also works, but FastHTML installs a static-file catch-all as its first
route, which silently swallows any mounted path ending in something that
looks like an asset extension -- ``/api/report.csv`` included.
"""

import logging
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.api import router as api_router
from src.ui import create_ui

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'INFO'),
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)


def create_app() -> FastAPI:
    app = FastAPI(
        title='Deal With It!',
        description='A Python API for creating "Deal With It"-like images.',
        version='1.0.0',
        docs_url='/api/docs',
        openapi_url='/api/openapi.json',
        redoc_url=None,
    )
    app.include_router(api_router, prefix='/api')
    # StaticFiles rather than FastHTML's catch-all: it resolves paths against
    # the directory and rejects anything that escapes it.
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')
    # Mounted last, and at the root, so it only sees what nothing above claimed.
    app.mount('/', create_ui())
    return app


app = create_app()
