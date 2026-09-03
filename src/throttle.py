"""Admission control: a body-size cap, a per-client rate limit and a cap on
how deep the queue may get. The last two are Redis-backed, so they hold
across web processes."""

import time

from redis.exceptions import RedisError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from src import jobs
from src.config import get_settings

RATE_KEY = 'dwi:rate:{client}:{window}'


def too_large_message(limit: int) -> str:
    return f'The request body must be under {limit} bytes.'


class BodyTooLarge(HTTPException):
    """Raised into a handler still reading a body that has grown past
    ``max_request_bytes``. An HTTPException so both apps' own exception
    middleware answers it, and FastAPI does not fold it into a 400."""

    def __init__(self, limit: int) -> None:
        # The rest of the body is never read, so the connection is done.
        super().__init__(413, too_large_message(limit), headers={'connection': 'close'})


def too_large_response(limit: int) -> JSONResponse:
    """For the middleware, which sits outside any exception handler."""
    return JSONResponse(status_code=413, content={'detail': too_large_message(limit)},
                        headers={'connection': 'close'})


class Throttled(Exception):
    """The submission was refused for now. ``retry_after`` is in seconds."""

    def __init__(self, message: str, retry_after: int, status_code: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)
        self.status_code = status_code


class TooManyRequests(Throttled):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            f'Too many submissions. Try again in {max(1, retry_after)} seconds.',
            retry_after, 429,
        )


class QueueFull(Throttled):
    def __init__(self) -> None:
        super().__init__('The queue is full right now. Try again in a moment.', 10, 503)


def check_rate(client: str) -> None:
    """Fixed-window counter: INCR a key per client per window, expire it with
    the window. Cheap, and off by one window at the boundary, which is fine
    for a limit meant to stop floods rather than meter usage."""
    settings = get_settings()
    if settings.rate_limit <= 0 or not client:
        return
    now = int(time.time())
    window = now - now % settings.rate_window
    key = RATE_KEY.format(client=client, window=window)
    connection = jobs.get_queue().connection
    try:
        with connection.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, settings.rate_window)
            count, _ = pipe.execute()
    except RedisError:
        # A broken broker fails the enqueue right after this anyway.
        return
    if count > settings.rate_limit:
        raise TooManyRequests(retry_after=window + settings.rate_window - now)


def check_queue_depth() -> None:
    settings = get_settings()
    if settings.max_queue_depth <= 0:
        return
    try:
        depth = jobs.get_queue().count
    except RedisError:
        return
    if depth >= settings.max_queue_depth:
        raise QueueFull()


def admit(client: str | None) -> None:
    """Raise :class:`Throttled` unless this client may submit right now."""
    check_queue_depth()
    check_rate(client or '')
