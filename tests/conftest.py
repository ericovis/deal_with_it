import socket
from io import BytesIO
from pathlib import Path

import fakeredis
import pytest
from PIL import Image

from src import jobs
from src.config import Settings, get_settings

HERE = Path(__file__).resolve().parent
STATIC_IMG = HERE.parents[0] / 'src' / 'static' / 'img'

#: Any routable address will do; tests never actually connect to it.
PUBLIC_IP = '93.184.216.34'
#: Hosts a test wants the SSRF guard to reject.
PRIVATE_HOSTS = {'localhost', 'internal.test', 'metadata.test'}
PRIVATE_IP = '169.254.169.254'


@pytest.fixture(autouse=True)
def stub_dns(monkeypatch):
    """Resolve every hostname locally, so no test needs DNS.

    Hosts in PRIVATE_HOSTS resolve to a link-local address so the SSRF guard
    has something realistic to reject; everything else looks public.
    """

    def fake_getaddrinfo(host, *args, **kwargs):
        if host in ('unresolvable.test',):
            raise socket.gaierror('name or service not known')
        address = PRIVATE_IP if host in PRIVATE_HOSTS else PUBLIC_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 80))]

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


@pytest.fixture
def settings() -> Settings:
    """Settings with limits small enough to trip deliberately."""
    return Settings(
        max_image_bytes=200_000,
        max_image_pixels=100_000,
        http_timeout=0.5,
        max_redirects=2,
        allow_private_addresses=False,
    )


def make_png(size: tuple[int, int] = (32, 32), color: str = 'red') -> bytes:
    buffer = BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return make_png()


@pytest.fixture
def base64_image() -> str:
    """A real photograph of a face, as a data URI."""
    return (HERE / 'fixtures' / 'img.base64.txt').read_text()


@pytest.fixture
def single_face_image() -> Path:
    return STATIC_IMG / 'me.jpg'


@pytest.fixture
def multiple_faces_image() -> Path:
    return STATIC_IMG / 'multiple_people.jpg'


@pytest.fixture
def faceless_image() -> Path:
    return STATIC_IMG / 'socks_the_cat.jpg'


@pytest.fixture(autouse=True)
def clean_settings_cache():
    """Settings are cached per process; tests that tweak the env need them fresh."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def redis_conn():
    connection = fakeredis.FakeStrictRedis()
    yield connection
    connection.flushall()
    connection.close()


@pytest.fixture
def sync_queue(redis_conn):
    """A queue that runs jobs inline, on enqueue.

    RQ swallows the exception from a failing job here exactly as a real worker
    does, so failure paths are exercised for real.
    """
    queue = jobs.configure(redis_conn, is_async=False)
    yield queue
    jobs.reset()


@pytest.fixture
def async_queue(redis_conn):
    """A queue that leaves jobs pending until `drain` runs them."""
    queue = jobs.configure(redis_conn, is_async=True)
    yield queue
    jobs.reset()


@pytest.fixture
def drain(redis_conn):
    """Run every queued job in-process.

    SimpleWorker rather than Worker: the forking worker silently does nothing
    against fakeredis.
    """
    from rq import SimpleWorker

    def run(queue_name: str | None = None, max_jobs: int | None = None) -> int:
        name = queue_name or get_settings().queue_name
        worker = SimpleWorker([name], connection=redis_conn)
        return worker.work(burst=True, logging_level='CRITICAL', max_jobs=max_jobs)

    return run


@pytest.fixture
def stub_task(monkeypatch):
    """Point the queue at one of the fakes in tests.support."""

    def install(path: str):
        monkeypatch.setattr(jobs, 'TASK', path)

    return install


@pytest.fixture
def client(sync_queue, stub_task):
    """A test client whose queue runs jobs inline against fakeredis."""
    from starlette.testclient import TestClient

    from src.app import app

    stub_task('tests.support.succeeds')
    return TestClient(app)


@pytest.fixture
def hx() -> dict:
    """Headers HTMX sends, which is what makes FastHTML return a fragment."""
    return {'hx-request': 'true'}
