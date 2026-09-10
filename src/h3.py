"""A small HTTP/3 client, for talking to the blob store over QUIC.

``requests`` and ``httpx`` speak HTTP/1.1 and 2 only, so this is aioquic's
connection with a sync face on it. One background thread runs an asyncio loop
that owns a single QUIC connection; every caller, from a threadpool handler or
a worker thread, hands it a request and blocks on the answer. Requests
multiplex as QUIC streams, so a slow upload does not queue a small read behind
it.

Web-safe: nothing here knows what the bytes are.
"""

import asyncio
import ipaddress
import logging
import socket
import ssl
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, H3Event, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import ConnectionTerminated, QuicEvent

logger = logging.getLogger(__name__)

#: How long a connection may sit idle before either end drops it. The
#: client reconnects on the next request, so this is memory on the store,
#: not correctness.
IDLE_TIMEOUT = 300.0


class H3Error(ConnectionError):
    """The store could not be reached, or answered nothing usable."""


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass
class _Pending:
    """One request in flight: the answer accumulates here until the stream ends."""

    future: asyncio.Future
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)


class _Protocol(QuicConnectionProtocol):
    """The QUIC connection, with HTTP/3 framing and per-stream futures on it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)
        self._pending: dict[int, _Pending] = {}
        self.terminated = False

    async def request(self, method: str, authority: str, path: str, body: bytes,
                      headers: Iterable[tuple[bytes, bytes]]) -> Response:
        stream_id = self._quic.get_next_available_stream_id()
        pending = _Pending(asyncio.get_running_loop().create_future())
        self._pending[stream_id] = pending
        self._http.send_headers(stream_id, [
            (b':method', method.encode()),
            (b':scheme', b'https'),
            (b':authority', authority.encode()),
            (b':path', path.encode()),
            *headers,
        ], end_stream=not body)
        if body:
            self._http.send_data(stream_id, body, end_stream=True)
        self.transmit()
        return await pending.future

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ConnectionTerminated):
            self.terminated = True
            reason = event.reason_phrase or event.error_code
            self._fail_all(H3Error(f'store connection closed: {reason}'))
            return
        for h3_event in self._http.handle_event(event):
            self._h3_event_received(h3_event)

    def connection_lost(self, exc: Exception | None) -> None:
        self.terminated = True
        self._fail_all(H3Error(f'store connection lost: {exc}'))
        super().connection_lost(exc)

    def _h3_event_received(self, event: H3Event) -> None:
        pending = self._pending.get(getattr(event, 'stream_id', -1))
        if pending is None:
            return
        if isinstance(event, HeadersReceived):
            for name, value in event.headers:
                if name == b':status':
                    pending.status = int(value)
                elif not name.startswith(b':'):
                    pending.headers[name.decode()] = value.decode()
        elif isinstance(event, DataReceived):
            pending.chunks.append(event.data)
        if getattr(event, 'stream_ended', False):
            del self._pending[event.stream_id]
            if not pending.future.done():
                pending.future.set_result(
                    Response(pending.status, pending.headers, b''.join(pending.chunks)))

    def _fail_all(self, error: Exception) -> None:
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(error)
        self._pending.clear()


class H3Client:
    """``https://host:port`` over QUIC, from any thread.

    The connection is made on the first request and kept; a request that
    finds it gone reconnects once and retries, which is what covers the idle
    timeout and a store that restarted. Concurrency is the loop's: N threads
    asking at once are N streams on one connection.
    """

    def __init__(self, url: str, timeout: float = 60.0) -> None:
        parts = urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname:
            raise ValueError(f'the store URL must be https://host[:port], not {url!r}')
        self.host = parts.hostname
        self.port = parts.port or 443
        self.authority = f'{self.host}:{self.port}'
        self.timeout = timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._protocol: _Protocol | None = None
        self._connect_lock: asyncio.Lock | None = None
        self._start = threading.Lock()

    def request(self, method: str, path: str, body: bytes = b'',
                headers: Iterable[tuple[bytes, bytes]] = ()) -> Response:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._request(method, path, body, tuple(headers)), loop)
        try:
            return future.result(timeout=self.timeout + 5)
        except TimeoutError as exc:
            future.cancel()
            raise H3Error(f'store did not answer {method} {path} in {self.timeout}s') from exc
        except OSError as exc:
            raise H3Error(f'store unreachable at {self.authority}: {exc}') from exc

    def close(self) -> None:
        loop = self._loop
        if loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._close(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        self._loop = None

    # -- on the loop ------------------------------------------------------

    async def _request(self, method: str, path: str, body: bytes,
                       headers: tuple[tuple[bytes, bytes], ...]) -> Response:
        for attempt in (1, 2):
            protocol = await self._connected()
            try:
                return await asyncio.wait_for(
                    protocol.request(method, self.authority, path, body, headers), self.timeout)
            except H3Error:
                self._protocol = None
                if attempt == 2:
                    raise
                logger.info('store connection dropped; reconnecting for %s %s', method, path)
        raise AssertionError('unreachable')

    async def _connected(self) -> _Protocol:
        assert self._connect_lock is not None
        async with self._connect_lock:
            if self._protocol is not None and not self._protocol.terminated:
                return self._protocol
            self._protocol = await self._connect()
            return self._protocol

    async def _connect(self) -> _Protocol:
        loop = asyncio.get_running_loop()
        configuration = QuicConfiguration(
            is_client=True, alpn_protocols=H3_ALPN, server_name=self.host,
            idle_timeout=IDLE_TIMEOUT)
        # The store mints its own certificate at start-up and nobody signed
        # it. Verifying would need the store to publish it somewhere the
        # client can trust more than the store itself, which on a private
        # network with one store is nothing. The tunnel is still encrypted.
        configuration.verify_mode = ssl.CERT_NONE
        address = await self._resolve(loop)
        family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.bind(('::' if family == socket.AF_INET6 else '0.0.0.0', 0))
        except OSError:
            sock.close()
            raise
        _, protocol = await loop.create_datagram_endpoint(
            lambda: _Protocol(QuicConnection(configuration=configuration)), sock=sock)
        assert isinstance(protocol, _Protocol)
        protocol.connect(address)
        try:
            await asyncio.wait_for(protocol.wait_connected(), self.timeout)
        except TimeoutError as exc:
            protocol.close()
            raise H3Error(f'no QUIC handshake with {self.authority} in {self.timeout}s') from exc
        return protocol

    async def _resolve(self, loop: asyncio.AbstractEventLoop) -> tuple:
        try:
            ipaddress.ip_address(self.host)
            return (self.host, self.port)
        except ValueError:
            pass
        infos = await loop.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)
        if not infos:
            raise H3Error(f'{self.host} does not resolve')
        return infos[0][4]

    async def _close(self) -> None:
        if self._protocol is not None:
            self._protocol.close()
            await self._protocol.wait_closed()
            self._protocol = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                self._connect_lock = asyncio.Lock()
                threading.Thread(target=loop.run_forever, name='h3-client', daemon=True).start()
                self._loop = loop
            return self._loop
