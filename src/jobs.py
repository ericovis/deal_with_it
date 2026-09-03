"""The queue seam between the web tier and the worker.

Everything RQ-shaped lives here, so the API and the UI only ever deal in job
ids and :class:`~src.models.JobResult`.
"""

import logging

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from src.config import get_settings
from src.models import JobResult, JobState

logger = logging.getLogger(__name__)

#: Bound lazily rather than at import: a ConnectionPool is not fork-safe, and
#: uvicorn/rq both fork after importing the app.
_queue: Queue | None = None

GENERIC_FAILURE = 'Something went wrong while processing this image.'

#: Resolved by the worker at execution time.
TASK = 'src.tasks.process_image'

#: RQ reports a job as "created" in the instant between being built and being
#: pushed. From the caller's point of view that is indistinguishable from queued.
_STATE_MAP = {JobStatus.CREATED: JobState.QUEUED}


def configure(connection: Redis | None = None, is_async: bool = True) -> Queue:
    """Bind this module to a Redis connection and return the queue.

    Called implicitly on first use. Tests call it explicitly with a fake
    connection (and usually ``is_async=False``, which runs jobs inline).
    """
    global _queue
    settings = get_settings()
    connection = connection or Redis.from_url(
        settings.redis_url,
        decode_responses=False,  # RQ stores pickles; decoding them corrupts jobs.
        socket_connect_timeout=5,
        socket_timeout=15,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )
    _queue = Queue(
        settings.queue_name,
        connection=connection,
        is_async=is_async,
        default_timeout=settings.job_timeout,
    )
    return _queue


def reset() -> None:
    """Forget the bound queue. Used between tests."""
    global _queue
    _queue = None


def get_queue() -> Queue:
    return _queue if _queue is not None else configure()


def enqueue(payload: dict) -> tuple[str, JobState]:
    """Hand an image payload to the workers.

    Returns the job id and the state it went in as -- normally ``queued``,
    but a synchronous queue (as used in tests) has already run it.
    """
    settings = get_settings()
    job = get_queue().enqueue(
        # Referenced by name, not imported: the web tier has no business
        # importing dlib, and this keeps that boundary honest.
        TASK,
        payload,
        job_timeout=settings.job_timeout,
        # Defaults would bite us both ways: results expire after 8 minutes
        # (before a slow client polls) while failures are kept for a year.
        result_ttl=settings.result_ttl,
        failure_ttl=settings.failure_ttl,
    )
    logger.info('queued job %s', job.id)
    # refresh=False: the status is already on the job we just created, and
    # there is no reason to pay a round trip to re-read it.
    status = job.get_status(refresh=False)
    return job.id, _STATE_MAP.get(status, JobState(status.value))


def result_for(job_id: str) -> JobResult | None:
    """Read a job's state, or None if the id is unknown or has expired."""
    queue = get_queue()
    try:
        job = Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError:
        return None

    status = job.get_status()
    if status == JobStatus.FINISHED:
        return _finished_result(job)
    if status == JobStatus.FAILED:
        # An exception escaped the task, which means a bug rather than bad
        # input -- the traceback is for our logs, not for the caller.
        result = job.latest_result()
        logger.error('job %s crashed: %s', job.id, result.exc_string if result else 'unknown')
        return JobResult(job_id=job.id, state=JobState.FAILED, error=GENERIC_FAILURE)

    # meta was already loaded by Job.fetch, so this costs no extra round trip.
    meta = job.meta or {}
    return JobResult(
        job_id=job.id,
        state=_STATE_MAP.get(status, JobState(status.value)),
        progress=meta.get('progress'),
        step=meta.get('step'),
    )


def _finished_result(job: Job) -> JobResult:
    """Unpack what :func:`src.tasks.process_image` returned.

    A finished job carrying an ``error`` is a rejected *input*, so it is
    reported as failed with that message -- the work completed, the image did
    not.
    """
    value = job.return_value() or {}
    if error := value.get('error'):
        return JobResult(job_id=job.id, state=JobState.FAILED, error=error)
    image = value.get('image')
    if not image:
        logger.error('job %s finished without an image or an error: %r', job.id, value)
        return JobResult(job_id=job.id, state=JobState.FAILED, error=GENERIC_FAILURE)
    return JobResult(job_id=job.id, state=JobState.FINISHED, image=image,
                     progress=100, step='Done')


def is_broker_reachable() -> bool:
    try:
        return bool(get_queue().connection.ping())
    except RedisError as exc:
        logger.warning('redis unreachable: %s', exc)
        return False
