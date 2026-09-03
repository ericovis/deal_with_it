"""A real browser against a real server.

These tests exist for what a test client cannot see: the CSS-only
interactions (Before/After, the full-screen view, the theme switch) and the
htmx wiring that actually swaps the DOM. Everything cheaper to assert lives
in ``tests/test_ui.py`` -- this suite is deliberately small, because it is
the slow one.

The stack is real but self-contained: uvicorn in a thread, an RQ worker in
another, fakeredis instead of a server, and the genuine face detector doing
the work. Nothing here reaches the network either.
"""

import os
import socket
import threading
import time
from contextlib import closing

import pytest

# Playwright is an optional group (`uv sync --group e2e`); without it there is
# nothing here to collect, and `uv run pytest` still passes.
try:
    import pytest_playwright  # noqa: F401
except ImportError:  # pragma: no cover - depends on what is installed
    collect_ignore_glob = ['test_*.py']

#: Generous: a browser, a queue and a real detector are all in the way.
TIMEOUT = 90_000


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _wait_for(check, what: str, seconds: float = 30.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise RuntimeError(f'{what} never came up')


def _serve_queue(connection, stop: threading.Event) -> None:
    """Work the queue until told to stop, with the real worker class, one
    burst at a time so the stop flag is honoured promptly."""
    from src.config import get_settings
    from src.worker import ThreadWorker

    name = get_settings().queue_name
    while not stop.is_set():
        ThreadWorker([name], connection=connection).work(
            burst=True, max_jobs=1, logging_level='CRITICAL',
        )
        time.sleep(0.05)


@pytest.fixture(scope='session', autouse=True)
def _needs_the_face_stack():
    pytest.importorskip('cv2', reason="the real worker runs here: install the 'worker' extra")


@pytest.fixture(scope='session', autouse=True)
def _needs_a_browser(playwright):
    try:
        playwright.chromium.launch().close()
    except Exception as exc:  # noqa: BLE001 - any launch failure means skip
        pytest.skip(f'chromium is not installed -- `playwright install chromium` ({exc})')


@pytest.fixture(scope='session')
def live_server(_needs_the_face_stack):
    import uvicorn
    from fakeredis import FakeStrictRedis

    # Short, because one hung fetch would block the single worker thread and
    # every job queued behind it.
    previous = os.environ.get('DWI_HTTP_TIMEOUT')
    os.environ['DWI_HTTP_TIMEOUT'] = '1'

    from src import jobs
    from src.app import app

    connection = FakeStrictRedis()
    jobs.configure(connection, is_async=True)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='warning'))
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    _wait_for(lambda: server.started, 'the server')

    stop = threading.Event()
    worker = threading.Thread(target=_serve_queue, args=(connection, stop), daemon=True)
    worker.start()

    yield f'http://127.0.0.1:{port}'

    stop.set()
    server.should_exit = True
    worker.join(timeout=5)
    jobs.reset()
    if previous is None:
        os.environ.pop('DWI_HTTP_TIMEOUT', None)
    else:
        os.environ['DWI_HTTP_TIMEOUT'] = previous


@pytest.fixture(scope='session')
def base_url(live_server):
    """What pytest-playwright resolves a relative page.goto() against."""
    return live_server


@pytest.fixture(scope='session')
def browser_context_args(browser_context_args):
    return {**browser_context_args, 'viewport': {'width': 1280, 'height': 900}}


@pytest.fixture(autouse=True)
def quiet_console(page):
    """No test may leave the browser complaining.

    The whole point of this interface is that it ships no JavaScript of its
    own, so anything in the console is either htmx failing or markup we got
    wrong.
    """
    problems: list[str] = []
    page.on('console', lambda m: problems.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda exc: problems.append(str(exc)))
    yield
    assert not problems, f'the browser complained: {problems}'
