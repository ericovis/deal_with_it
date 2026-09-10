"""The blob store as a service: ``python -m src.store``.

Where the two tiers cannot mount one directory -- a deployment that gives
every service its own disk -- this owns the directory and the others talk to
it. It is the filesystem half of :mod:`src.blobs` behind a handful of routes,
and :mod:`src.blobs` itself is the client, so the web tier and the worker
call exactly what they always called.

It speaks HTTP/3: hypercorn serves the app over QUIC on the UDP side of
``DWI_STORE_BIND``, under a certificate minted at start-up, and plain
HTTP/1.1 on the TCP side of the same port for a health check that cannot
speak QUIC. Nothing else is meant to reach either: like Redis, the store has
no password because the network it sits on has no strangers.
"""

import asyncio
import datetime as dt
import logging
import mimetypes
import tempfile
import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from src import blobs
from src.config import get_settings

logger = logging.getLogger(__name__)


def _bad(exc: Exception) -> Response:
    return PlainTextResponse(str(exc), status_code=400)


async def put(request: Request) -> Response:
    job_id, name = request.path_params['job_id'], request.path_params['name']
    try:
        reference = blobs.put.local(job_id, name, await request.body())
    except blobs.UnsafeReference as exc:
        return _bad(exc)
    return PlainTextResponse(reference, status_code=201)


async def get(request: Request) -> Response:
    reference = f"{request.path_params['job_id']}/{request.path_params['name']}"
    try:
        path = blobs.path(reference)
    except blobs.UnsafeReference as exc:
        return _bad(exc)
    if not path.is_file():
        return Response('', status_code=404)
    if request.method == 'HEAD':
        # Not FileResponse: its HEAD carries the file's content-length with
        # no body, which hypercorn's HTTP/3 layer treats as a broken stream
        # and closes the whole connection over.
        return Response('', status_code=200)
    media_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    return FileResponse(path, media_type=media_type)


async def delete(request: Request) -> Response:
    reference = f"{request.path_params['job_id']}/{request.path_params['name']}"
    blobs.delete.local(reference)
    return Response('', status_code=204)


async def forget(request: Request) -> Response:
    try:
        blobs.forget.local(request.path_params['job_id'])
    except blobs.UnsafeReference as exc:
        return _bad(exc)
    return Response('', status_code=204)


async def link(request: Request) -> Response:
    body = await request.json()
    try:
        reference = blobs.link.local(body['source'], body['job_id'], body['name'])
    except (blobs.UnsafeReference, KeyError, TypeError) as exc:
        return _bad(exc)
    except FileNotFoundError:
        return Response('', status_code=404)
    return PlainTextResponse(reference, status_code=201)


async def usage(request: Request) -> Response:
    return JSONResponse([entry.__dict__ for entry in blobs.usage.local()])


async def reap(request: Request) -> Response:
    body = await request.json()
    try:
        swept = blobs.reap.local(ttl=float(body['ttl']), max_bytes=int(body['max_bytes']),
                                 min_age=float(body['min_age']))
    except (KeyError, TypeError, ValueError) as exc:
        return _bad(exc)
    return JSONResponse(swept)


async def health(request: Request) -> Response:
    return JSONResponse({'ok': True, 'root': str(blobs.root())})


app = Starlette(routes=[
    Route('/health', health),
    Route('/usage', usage),
    Route('/reap', reap, methods=['POST']),
    Route('/link', link, methods=['POST']),
    Route('/{job_id}', forget, methods=['DELETE']),
    Route('/{job_id}/{name}', get, methods=['GET', 'HEAD']),
    Route('/{job_id}/{name}', put, methods=['PUT']),
    Route('/{job_id}/{name}', delete, methods=['DELETE']),
])


def self_signed(directory: Path, hostname: str = 'store') -> tuple[Path, Path]:
    """A certificate for QUIC, which will not run without one.

    Minted here rather than shipped: the client does not verify it (see
    :mod:`src.h3`), so its only job is to let TLS 1.3 key the tunnel. A new
    one per start-up means there is nothing to rotate, expire or leak.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, hostname)])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / 'store.crt', directory / 'store.key'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return cert_path, key_path


def serve(bind: str, shutdown: threading.Event | None = None) -> None:
    """Run the store on ``bind`` (``host:port``) until ``shutdown`` is set.

    QUIC on UDP and plain HTTP on TCP, same port. Hypercorn's ``bind`` is
    TLS-over-TCP, which nobody here needs: the health check wants plain
    HTTP and everything else wants QUIC.
    """
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    blobs.serve_locally()
    with tempfile.TemporaryDirectory(prefix='dwi-store-') as directory:
        certfile, keyfile = self_signed(Path(directory))
        config = Config()
        config.bind = []
        config.insecure_bind = [bind]
        config.quic_bind = [bind]
        config.certfile, config.keyfile = str(certfile), str(keyfile)
        config.accesslog = None
        config.errorlog = '-'

        async def stopped() -> None:
            # A threading.Event so a test can stop the server from outside
            # its loop; polled, because that is the only thread-safe wait.
            while not shutdown.is_set():
                await asyncio.sleep(0.1)

        trigger = stopped if shutdown is not None else None
        asyncio.run(hypercorn_serve(app, config, shutdown_trigger=trigger, mode='asgi'))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s: %(message)s')
    bind = get_settings().store_bind
    logger.info('blob store serving %s over QUIC and plain HTTP on %s', blobs.root(), bind)
    serve(bind)


if __name__ == '__main__':
    main()
