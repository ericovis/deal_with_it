"""The queue seam between the web tier and the worker.

Everything RQ-shaped lives here, so the API and the UI only ever deal in job
ids and :class:`~src.models.JobResult`.
"""

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from rq.utils import str_to_date

from src import blobs
from src.config import get_settings
from src.models import JobImages, JobResult, JobSource, JobState, SourceKind

logger = logging.getLogger(__name__)

#: Bound lazily: a ConnectionPool is not fork-safe, and uvicorn and rq both
#: fork after importing the app.
_queue: Queue | None = None

GENERIC_FAILURE = 'Something went wrong while processing this image.'

ABANDONED = 'The worker restarted while this image was being processed.'

#: Where a finished result can be looked at by someone who was not here.
SHARE_PREFIX = '/s/'

#: How long past its own ``job_timeout`` a started job is given before the
#: page stops believing in it. Only has to cover the clock skew between the
#: worker that stamped ``started_at`` and whoever is reading it.
ABANDONED_GRACE = 30

#: Resolved by the worker at execution time. Referenced by name so the web
#: tier never imports the face stack.
TASK = 'src.tasks.process_image'

#: RQ reports "created" in the instant between building and pushing a job.
_STATE_MAP = {JobStatus.CREATED: JobState.QUEUED}

_TERMINAL = {JobStatus.FINISHED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED}


def configure(connection: Redis | None = None, is_async: bool = True) -> Queue:
    """Bind this module to a Redis connection and return the queue.

    Called implicitly on first use. Tests call it explicitly with a fake
    connection (and usually ``is_async=False``, which runs jobs inline).
    """
    global _queue
    settings = get_settings()
    connection = connection or Redis.from_url(
        settings.redis_url,
        decode_responses=False,  # RQ stores pickles.
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


def new_id() -> str:
    """An id minted before the job exists, so its pictures have a directory
    to go in before anything is queued. RQ takes it verbatim."""
    return str(uuid4())


def enqueue(payload: dict, meta: dict | None = None,
            job_id: str | None = None) -> tuple[str, JobState]:
    """Hand an image payload to the workers.

    ``meta`` rides along on the job and comes back through :func:`describe`;
    the UI uses it to label a card. The worker writes progress into the same
    dict.

    Returns the job id and the state it went in as -- normally ``queued``,
    but a synchronous queue (as used in tests) has already run it.
    """
    settings = get_settings()
    job = get_queue().enqueue(
        TASK,
        payload,
        job_id=job_id,
        meta=dict(meta or {}),
        job_timeout=settings.job_timeout,
        result_ttl=settings.result_ttl,
        failure_ttl=settings.failure_ttl,
    )
    logger.info('queued job %s', job.id)
    status = job.get_status(refresh=False)
    return job.id, _STATE_MAP.get(status, JobState(status.value))


def _fetch(job_id: str) -> Job | None:
    try:
        return Job.fetch(job_id, connection=get_queue().connection)
    except NoSuchJobError:
        return None


def _peek(job_id: str) -> tuple[JobStatus, dict, int | None, datetime | None] | None:
    """Status, meta, queue position and start time, without the payload.

    ``Job.fetch`` reads the whole hash, and for an upload that includes the
    image itself -- megabytes per poll, once a second per card. The few
    things a pending job is polled for are tiny, and a pipeline buys all of
    them for one round trip.

    ``LPOS`` is the index of the id in the queue list, so it *is* the number
    of jobs ahead of this one. It is None for anything not in the list: a job
    already picked up, and one that never was.
    """
    queue = get_queue()
    with queue.connection.pipeline() as pipe:
        pipe.hmget(Job.key_for(job_id), ['status', 'meta', 'started_at'])
        pipe.lpos(queue.key, job_id)
        (raw_status, raw_meta, raw_started), ahead = pipe.execute()
    if raw_status is None:
        return None
    meta = queue.serializer.loads(raw_meta) if raw_meta else {}
    return JobStatus(raw_status.decode()), meta, ahead, _started_at(raw_started)


def _started_at(raw: bytes | None) -> datetime | None:
    """When the worker picked this job up, if it has been picked up."""
    if not raw:
        return None
    try:
        return str_to_date(raw)
    except ValueError:  # pragma: no cover - a hash we did not write
        return None


def _abandoned(status: JobStatus, started_at: datetime | None) -> bool:
    """True for a job nobody is coming back to.

    A job runs for at most ``job_timeout``; RQ's death penalty is what
    enforces that. Still ``started`` well past it means the worker that had
    it is gone -- killed mid-job by a deploy, a restart, a crash in native
    code or the OOM killer, all of which this deployment expects and
    restarts from. RQ does notice, in ``run_maintenance_tasks``, but that
    runs every ten minutes *and* only while some worker is alive to run it,
    so a card would poll its last checkpoint ("Encoding the result") for
    that long, or forever while the worker is down. This is the reader
    deciding instead, which needs nobody's cooperation.
    """
    if status != JobStatus.STARTED or started_at is None:
        return False
    age = (datetime.now(UTC) - started_at).total_seconds()
    return age > get_settings().job_timeout + ABANDONED_GRACE


def _pending(job_id: str, status: JobStatus, meta: dict,
             ahead: int | None = None) -> JobResult:
    return JobResult(
        job_id=job_id,
        state=_STATE_MAP.get(status, JobState(status.value)),
        progress=meta.get('progress'),
        step=meta.get('step'),
        ahead=ahead,
    )


def result_for(job_id: str) -> JobResult | None:
    """Read a job's state, or None if the id is unknown or has expired."""
    peeked = _peek(job_id)
    if peeked is None:
        return None
    status, meta, ahead, started_at = peeked
    if _abandoned(status, started_at):
        return _abandoned_result(job_id)
    if status not in _TERMINAL:
        return _pending(job_id, status, meta, ahead)
    job = _fetch(job_id)
    return None if job is None else _state_of(job)


def describe(job_id: str) -> tuple[JobResult, JobSource] | None:
    """State *and* provenance. The payload is only read once the job is done."""
    peeked = _peek(job_id)
    if peeked is None:
        return None
    status, meta, ahead, started_at = peeked
    if _abandoned(status, started_at):
        return _abandoned_result(job_id), _source_of(meta, None)
    if status not in _TERMINAL:
        return _pending(job_id, status, meta, ahead), _source_of(meta, None)
    job = _fetch(job_id)
    if job is None:
        return None
    return _state_of(job), _source_of(job.meta or {}, job.args[0] if job.args else {})


def _abandoned_result(job_id: str) -> JobResult:
    """A job whose worker went away. Retryable: the payload is still there."""
    logger.warning('job %s was abandoned by its worker', job_id)
    return JobResult(job_id=job_id, state=JobState.FAILED, error=ABANDONED)


def payload_for(job_id: str) -> dict | None:
    """The image payload a job was created with."""
    job = _fetch(job_id)
    return job.args[0] if job is not None and job.args else None


def resubmit(job_id: str) -> tuple[str, JobState] | None:
    """Queue a failed job's payload again, as a new job.

    A fresh job rather than RQ's requeue: the failures worth retrying are the
    ones the task *returned*, which are finished jobs to the queue.

    An upload's picture is hard-linked into the new job's directory rather
    than shared from the old one, so both stay independently deletable --
    which is what the sweep assumes. If it has already been swept there is
    nothing to retry.
    """
    job = _fetch(job_id)
    if job is None or not job.args:
        return None
    payload = dict(job.args[0])
    fresh = new_id()
    if reference := payload.get('blob'):
        if not blobs.exists(reference):
            logger.info('cannot retry %s: its picture has expired', job_id)
            return None
        payload['blob'] = blobs.link(reference, fresh, blobs.split(reference)[1])
    return enqueue(payload, job_id=fresh, meta={
        key: value for key, value in (job.meta or {}).items()
        if key in ('kind', 'label', 'thumb')
    })


def label_for(payload: dict) -> str:
    """A name for an image nobody gave a name to."""
    if url := payload.get('url'):
        parts = urlparse(url)
        return f'{parts.netloc}{parts.path}'.rstrip('/') or url
    return 'Uploaded image'


def _source_of(meta: dict, payload: dict | None) -> JobSource:
    """Where the image came from, preferring what the UI recorded.

    Jobs submitted through the JSON API carry no meta, so everything falls
    back to the payload -- which a pending job does not have loaded.
    """
    payload = payload or {}
    url = payload.get('url')
    kind = meta.get('kind') or (SourceKind.URL if url else SourceKind.UPLOAD)
    return JobSource(
        kind=kind,
        label=meta.get('label') or (label_for(payload) if payload else ''),
        thumb=meta.get('thumb') or url,
        faces=meta.get('faces'),
    )


def _state_of(job: Job) -> JobResult:
    status = job.get_status()
    if status == JobStatus.FINISHED:
        return _finished_result(job)
    if status == JobStatus.FAILED:
        # An exception escaped the task: a bug, so the traceback is for our
        # logs and the caller gets a generic message.
        result = job.latest_result()
        logger.error('job %s crashed: %s', job.id, result.exc_string if result else 'unknown')
        return JobResult(job_id=job.id, state=JobState.FAILED, error=GENERIC_FAILURE)
    return _pending(job.id, status, job.meta or {})


def _finished_result(job: Job) -> JobResult:
    """A finished job carrying an ``error`` is a rejected *input*: reported as
    failed with that message."""
    value = job.return_value() or {}
    if error := value.get('error'):
        return JobResult(job_id=job.id, state=JobState.FAILED, error=error)
    written = value.get('images')
    if not written:
        logger.error('job %s finished without pictures or an error: %r', job.id, value)
        return JobResult(job_id=job.id, state=JobState.FAILED, error=GENERIC_FAILURE)
    meta = job.meta or {}
    return JobResult(
        job_id=job.id, state=JobState.FINISHED,
        images=JobImages(**{role: blobs.url(ref) for role, ref in written.items()}),
        downloads={fmt: blobs.url(ref) for fmt, ref in (value.get('downloads') or {}).items()},
        share_url=f'{SHARE_PREFIX}{job.id}',
        expires_at=value.get('expires_at'),
        progress=100, step='Done',
        faces=meta.get('landmarks') or None, detection=meta.get('detection'),
    )


def shared_record(job_id: str) -> dict | None:
    """What a share page renders from: the job's own file, not Redis.

    Redis forgets a job at ``result_ttl``; the pictures live until
    ``blob_ttl``. Reading from disk is what makes a shared link last exactly
    as long as what it shows.
    """
    record = blobs.read_json(job_id)
    return record if record and record.get('images') else None


def is_broker_reachable() -> bool:
    try:
        return bool(get_queue().connection.ping())
    except RedisError as exc:
        logger.warning('redis unreachable: %s', exc)
        return False
