"""The JSON API, mounted under ``/api`` by :mod:`src.app`.

Handlers are plain ``def`` on purpose: they talk to Redis, and FastAPI runs
sync handlers in a threadpool, so a slow round trip or a large payload does
not stall the event loop.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status

from src import blobs, images, jobs, throttle
from src.errors import DealWithItError
from src.models import ImageRequest, JobCreated, JobResult, SourceKind

logger = logging.getLogger(__name__)

router = APIRouter()


def client_of(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    '/jobs',
    response_model=JobCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Queue an image for processing',
    responses={
        429: {'description': 'Too many submissions from this client'},
        503: {'description': 'The queue is full'},
    },
)
def create_job(image: ImageRequest, request: Request) -> JobCreated:
    """Accept an image reference and return immediately with a job id.

    Poll ``status_url`` until ``state`` is ``finished`` or ``failed``.
    """
    try:
        throttle.admit(client_of(request))
    except throttle.Throttled as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers={'Retry-After': str(exc.retry_after)},
        ) from exc
    payload = image.as_payload()
    job_id = jobs.new_id()
    if payload['base64'] is not None:
        # base64-decoding a string, not decoding an image: the bytes go
        # straight to disk and the queue carries a path. `base64` stays in
        # the published request contract; it just stops riding through Redis.
        try:
            data = images.decode_data_uri(payload['base64'])
        except DealWithItError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=str(exc)) from exc
        media_type = payload['base64'][len('data:'):payload['base64'].index(';')]
        queued = {'url': None,
                  'blob': blobs.put(job_id, f'source.{blobs.extension_for(media_type)}', data)}
    else:
        queued = {'url': payload['url'], 'blob': None}

    job_id, state = jobs.enqueue(queued, job_id=job_id, meta={
        'kind': str(SourceKind.URL if payload['url'] else SourceKind.UPLOAD),
        'label': jobs.label_for(payload),
        'thumb': payload['url'],
    })
    return JobCreated(
        job_id=job_id,
        state=state,
        status_url=str(request.url_for('read_job', job_id=job_id)),
    )


@router.get(
    '/jobs/{job_id}',
    response_model=JobResult,
    name='read_job',
    summary='Read the state, and eventually the result, of a job',
    responses={404: {'description': 'Unknown or expired job id'}},
)
def read_job(job_id: str) -> JobResult:
    result = jobs.result_for(job_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No such job. It may have expired.',
        )
    return result


@router.get('/health', summary='Liveness and broker connectivity')
def health() -> dict:
    return {'status': 'ok', 'redis': jobs.is_broker_reachable()}
