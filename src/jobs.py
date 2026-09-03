"""The queue seam between the web tier and the worker.

Everything RQ-shaped lives here, so the API and the UI only ever deal in job
ids and :class:`~src.models.JobResult`.
"""

import logging
from urllib.parse import urlparse

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus

from src.config import get_settings
from src.models import JobResult, JobSource, JobState, SourceKind

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


def enqueue(payload: dict, meta: dict | None = None) -> tuple[str, JobState]:
    """Hand an image payload to the workers.

    ``meta`` rides along on the job and comes back through :func:`describe`.
    The UI uses it to label a card with the file name the person recognises,
    which the payload itself does not carry -- a base64 upload is anonymous
    once it is a data URI. The worker writes progress into the same dict, and
    ``save_meta`` rewrites it whole, so both survive.

    Returns the job id and the state it went in as -- normally ``queued``,
    but a synchronous queue (as used in tests) has already run it.
    """
    settings = get_settings()
    job = get_queue().enqueue(
        # Referenced by name, not imported: the web tier has no business
        # importing dlib, and this keeps that boundary honest.
        TASK,
        payload,
        meta=dict(meta or {}),
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


def _fetch(job_id: str) -> Job | None:
    try:
        return Job.fetch(job_id, connection=get_queue().connection)
    except NoSuchJobError:
        return None


def result_for(job_id: str) -> JobResult | None:
    """Read a job's state, or None if the id is unknown or has expired."""
    job = _fetch(job_id)
    return None if job is None else _state_of(job)


def describe(job_id: str) -> tuple[JobResult, JobSource] | None:
    """State *and* provenance, from a single fetch.

    The UI needs both on every poll, and ``Job.fetch`` already deserialises
    the whole job -- asking twice would double the round trips for nothing.
    """
    job = _fetch(job_id)
    return None if job is None else (_state_of(job), _source_of(job))


def payload_for(job_id: str) -> dict | None:
    """The image payload a job was created with."""
    job = _fetch(job_id)
    return job.args[0] if job is not None and job.args else None


def resubmit(job_id: str) -> tuple[str, JobState] | None:
    """Queue a failed job's payload again, as a new job.

    A retry is a fresh job rather than RQ's own requeue: the failures worth
    retrying are the ones the task *returned* (an unreachable host, say), and
    those are finished jobs as far as the queue is concerned.
    """
    job = _fetch(job_id)
    if job is None or not job.args:
        return None
    return enqueue(job.args[0], meta={
        key: value for key, value in (job.meta or {}).items()
        if key in ('kind', 'label', 'thumb')
    })


def label_for(payload: dict) -> str:
    """A name for an image nobody gave a name to."""
    if url := payload.get('url'):
        parts = urlparse(url)
        return f'{parts.netloc}{parts.path}'.rstrip('/') or url
    return 'Uploaded image'


def _source_of(job: Job) -> JobSource:
    """Where the image came from, preferring what the UI recorded.

    Jobs submitted through the JSON API carry no meta at all, so everything
    falls back to what can be read off the payload.
    """
    meta = job.meta or {}
    payload = job.args[0] if job.args else {}
    url = payload.get('url')
    kind = meta.get('kind') or (SourceKind.URL if url else SourceKind.UPLOAD)
    return JobSource(
        kind=kind,
        label=meta.get('label') or label_for(payload),
        thumb=meta.get('thumb') or url,
        original=url or payload.get('base64'),
        faces=meta.get('faces'),
    )


def _state_of(job: Job) -> JobResult:
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
