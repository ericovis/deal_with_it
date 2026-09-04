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

from src.config import get_settings

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


@pytest.fixture(scope='session', autouse=True)
def blob_root(tmp_path_factory):
    """One blob store for the whole session, set once and left set.

    Overrides the per-test fixture in tests/conftest.py on purpose. The
    server and the worker here are threads in *this* process and read the
    settings cache the tests share, so a per-test store would be repointed
    under a session-scoped server between tests -- the worker writes a job's
    pictures into one directory and the next test moves the store somewhere
    else. Cards then hang instead of finishing, in whichever test happens to
    run next, which is a miserable thing to debug.
    """
    root = tmp_path_factory.mktemp('e2e-blobs')
    os.environ['DWI_BLOB_DIR'] = str(root)
    get_settings.cache_clear()
    return root


@pytest.fixture(scope='session')
def live_server(_needs_the_face_stack, blob_root):
    import uvicorn
    from fakeredis import FakeStrictRedis

    # Short, because one hung fetch would block the single worker thread and
    # every job queued behind it.
    #
    # No rate limit, because the whole suite submits from 127.0.0.1 and the
    # default is thirty a minute: the run is already past that, so whether a
    # test passed depended on how many of its submissions landed inside the
    # same fixed window. Being refused is worth a test, but a deterministic
    # one -- tests/test_throttle.py has it.
    pinned = {'DWI_HTTP_TIMEOUT': '1', 'DWI_RATE_LIMIT': '0',
              'DWI_BLOB_DIR': os.environ['DWI_BLOB_DIR']}
    previous = {name: os.environ.get(name) for name in pinned}
    os.environ.update(pinned)
    # Settings are cached and something earlier in the run may have read
    # them, so prime the cache with these -- and then put the environment
    # back, because tests/test_throttle.py has its own opinion about both.
    get_settings.cache_clear()
    get_settings()
    for name, value in previous.items():
        if name == 'DWI_BLOB_DIR':
            continue  # Session-scoped: the server keeps writing there.
        if value is None:
            del os.environ[name]
        else:
            os.environ[name] = value

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
    get_settings.cache_clear()


@pytest.fixture(scope='session')
def base_url(live_server):
    """What pytest-playwright resolves a relative page.goto() against."""
    return live_server


@pytest.fixture(scope='session')
def browser_context_args(browser_context_args):
    return {**browser_context_args, 'viewport': {'width': 1280, 'height': 900}}


#: What a browser logs for the status of the page it was asked to open. A
#: test that navigates to a 404 on purpose cannot avoid it.
_NAVIGATION_404 = 'the server responded with a status of 404'


@pytest.fixture(autouse=True)
def quiet_console(page, request):
    """No test may leave the browser complaining.

    The interface ships three lines of script -- the clipboard call, the
    one-file-per-request upload, and the expiry countdown -- so anything in
    the console is almost always htmx failing, one of those three, or markup
    we got wrong. This is what says so.

    A test marked ``expects_404`` is visiting a missing page deliberately and
    the browser logs the navigation's own status; that one line is allowed,
    and nothing else is.
    """
    problems: list[str] = []
    page.on('console', lambda m: problems.append(m.text) if m.type == 'error' else None)
    page.on('pageerror', lambda exc: problems.append(str(exc)))
    yield
    if request.node.get_closest_marker('expects_404'):
        problems = [p for p in problems if _NAVIGATION_404 not in p]
    assert not problems, f'the browser complained: {problems}'
