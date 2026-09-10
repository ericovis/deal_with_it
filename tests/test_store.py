"""The blob store as a service, spoken to over HTTP/3.

A real hypercorn serving ``src.store`` over QUIC on a loopback UDP port, in a
thread, for the whole module; every test then talks to it through the very
same ``blobs.put`` and friends the app uses, with ``DWI_BLOB_STORE_URL``
pointing at it. The disk it writes to is still each test's own ``blob_root``:
same process, same settings.
"""

import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from src import blobs, images, store
from src.app import app
from src.config import get_settings

JOB = '9f2c4a1b-0000-4000-8000-000000000001'
OTHER = '9f2c4a1b-0000-4000-8000-000000000002'


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(('127.0.0.1', 0))
        return udp.getsockname()[1]


def _plain_http(port: int, request: bytes) -> bytes:
    """One request on the store's TCP side. A raw socket, because the
    suite stubs DNS and a literal address must not go through it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp:
        tcp.settimeout(2)
        tcp.connect(('127.0.0.1', port))
        tcp.sendall(request)
        answer = b''
        while chunk := tcp.recv(65536):
            answer += chunk
        return answer


@pytest.fixture(scope='module')
def store_url():
    port = _free_port()
    stop = threading.Event()
    thread = threading.Thread(target=store.serve, args=(f'127.0.0.1:{port}', stop),
                              name='store', daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if b'"ok":true' in _plain_http(port, b'GET /health HTTP/1.0\r\n\r\n'):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError('the store did not come up')
    yield f'https://127.0.0.1:{port}'
    stop.set()
    thread.join(timeout=5)


@pytest.fixture
def remote(store_url, monkeypatch, blob_root):
    """Every blobs call in the test goes over QUIC to the store, which writes
    to this test's blob_root."""
    monkeypatch.setenv('DWI_BLOB_STORE_URL', store_url)
    get_settings.cache_clear()
    yield blobs._remote()
    get_settings.cache_clear()


class TestRoundTrip:
    def test_bytes_go_over_the_wire_and_land_on_the_stores_disk(self, remote, blob_root):
        reference = blobs.put(JOB, 'source.jpg', b'not really a jpeg')
        assert (blob_root / JOB / 'source.jpg').read_bytes() == b'not really a jpeg'
        assert blobs.get(reference) == b'not really a jpeg'
        assert blobs.exists(reference)

    def test_it_really_is_http3(self, remote):
        blobs.put(JOB, 'view.webp', b'x')
        protocol = remote.client._protocol
        assert protocol is not None and not protocol.terminated
        assert protocol._quic.tls.alpn_negotiated == 'h3'

    def test_a_large_upload_fits(self, remote):
        data = os.urandom(6 * 1024 * 1024)
        assert blobs.get(blobs.put(JOB, 'source.jpg', data)) == data

    def test_requests_multiplex(self, remote):
        """Eight threads on one connection: streams, not a queue."""
        def roundtrip(index: int) -> bool:
            payload = bytes([index]) * 100_000
            return blobs.get(blobs.put(JOB, f'f{index}.bin', payload)) == payload

        with ThreadPoolExecutor(8) as pool:
            assert all(pool.map(roundtrip, range(8)))

    def test_what_is_not_there(self, remote):
        with pytest.raises(FileNotFoundError):
            blobs.get(f'{JOB}/view.webp')
        assert not blobs.exists(f'{JOB}/view.webp')
        assert blobs.read_json(JOB) is None

    def test_meta_json_reads_back(self, remote):
        blobs.put(JOB, 'meta.json', b'{"faces": 2}')
        assert blobs.read_json(JOB) == {'faces': 2}


class TestReferencesStayValidated:
    def test_a_bad_reference_never_leaves_the_process(self, remote, monkeypatch):
        asked = []
        monkeypatch.setattr(remote.client, 'request', lambda *a, **k: asked.append(a))
        with pytest.raises(blobs.UnsafeReference):
            blobs.put('../etc', 'passwd.txt', b'x')
        with pytest.raises(blobs.UnsafeReference):
            blobs.get('a/b/c.webp')
        assert not blobs.exists('.hidden/x.webp')
        assert asked == []

    def test_the_store_refuses_one_of_its_own(self, store_url):
        """Belt to the client's brace: the service checks too."""
        port = int(store_url.rsplit(':', 1)[1])
        answer = _plain_http(port, b'PUT /has.dots/x.webp HTTP/1.0\r\nContent-Length: 1\r\n\r\nx')
        assert answer.startswith(b'HTTP/1.0 400') or answer.startswith(b'HTTP/1.1 400')


class TestLinkingAndDeleting:
    def test_a_link_shares_the_bytes_on_the_stores_disk(self, remote, blob_root):
        original = blobs.put(JOB, 'source.jpg', b'the original upload')
        copy = blobs.link(original, OTHER, 'source.jpg')
        assert blobs.get(copy) == b'the original upload'
        assert (blob_root / JOB / 'source.jpg').stat().st_ino == \
            (blob_root / OTHER / 'source.jpg').stat().st_ino

    def test_linking_what_is_not_there(self, remote):
        with pytest.raises(FileNotFoundError):
            blobs.link(f'{JOB}/source.jpg', OTHER, 'source.jpg')

    def test_delete_and_forget(self, remote, blob_root):
        blobs.put(JOB, 'source.jpg', b'x')
        blobs.put(JOB, 'view.webp', b'y')
        blobs.delete(f'{JOB}/source.jpg')
        assert not blobs.exists(f'{JOB}/source.jpg')
        assert blobs.exists(f'{JOB}/view.webp')
        blobs.forget(JOB)
        assert not (blob_root / JOB).exists()


class TestSweepingThroughTheService:
    def test_usage_and_reap(self, remote, blob_root):
        blobs.put(JOB, 'view.webp', b'x' * 100)
        blobs.put(OTHER, 'view.webp', b'y' * 50)
        when = time.time() - 7200
        os.utime(blob_root / JOB, (when, when))
        sizes = {entry.job_id: entry.size for entry in blobs.usage()}
        assert sizes == {JOB: 100, OTHER: 50}
        assert blobs.reap(ttl=3600, max_bytes=10**9, min_age=0) == [JOB]
        assert not (blob_root / JOB).exists()
        assert (blob_root / OTHER).exists()


class TestTheAppOnTopOfIt:
    def test_a_picture_is_served_by_way_of_the_store(self, remote):
        blobs.put(JOB, 'view.webp', b'RIFF....WEBP')
        with TestClient(app) as client:
            response = client.get(f'/i/{JOB}/view.webp')
        assert response.status_code == 200
        assert response.content == b'RIFF....WEBP'
        assert response.headers['content-type'] == 'image/webp'
        assert response.headers['cache-control'].startswith('private,')

    def test_a_missing_picture_is_a_404(self, remote):
        with TestClient(app) as client:
            assert client.get(f'/i/{JOB}/view.webp').status_code == 404
            assert client.get(f'/i/{JOB}/.view.webp.tmp').status_code == 404

    def test_the_worker_reads_a_submission_through_it(self, remote, png_bytes):
        reference = blobs.put(JOB, 'source.png', png_bytes)
        pixels, kind = images.load(blob=reference)
        assert pixels.shape[:2] == (32, 32)
        assert kind == 'PNG'

    def test_a_vanished_submission_is_the_users_error_not_a_crash(self, remote):
        with pytest.raises(images.ImageSourceError):
            images.load(blob=f'{JOB}/source.png')


class TestTheConnection:
    def test_a_dropped_connection_is_remade_on_the_next_call(self, remote):
        blobs.put(JOB, 'view.webp', b'x')
        old = remote.client._protocol
        remote.client._loop.call_soon_threadsafe(old.close)
        deadline = time.time() + 5
        while not old.terminated and time.time() < deadline:
            time.sleep(0.02)
        assert old.terminated
        assert blobs.get(f'{JOB}/view.webp') == b'x'
        assert remote.client._protocol is not old

    def test_health_answers_in_plain_http(self, store_url):
        """The TCP side exists for a health check that cannot speak QUIC."""
        port = int(store_url.rsplit(':', 1)[1])
        answer = _plain_http(port, b'GET /health HTTP/1.0\r\n\r\n')
        assert b' 200 ' in answer.split(b'\r\n', 1)[0]
        assert b'"ok":true' in answer

    def test_the_store_thread_never_asks_itself(self):
        """reap calls usage through the dispatcher; the store's own thread
        must stay on disk or it would wait on its own answer."""
        blobs.serve_locally()
        try:
            assert blobs._remote() is None
        finally:
            blobs._thread.local_only = False
