"""The JSON API.

Mounted under ``/api`` by :mod:`src.app`, which is why the paths here are
relative. Handlers do no image work at all -- they hand a payload to the
queue and read job state back, so nothing here can block for longer than a
Redis round trip.
"""

import logging

from fastapi import APIRouter, HTTPException, Request, status

from src import jobs
from src.models import ImageRequest, JobCreated, JobResult

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    '/jobs',
    response_model=JobCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary='Queue an image for processing',
)
async def create_job(image: ImageRequest, request: Request) -> JobCreated:
    """Accept an image reference and return immediately with a job id.

    Poll ``status_url`` until ``state`` is ``finished`` or ``failed``.
    """
    job_id, state = jobs.enqueue(image.as_payload())
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
async def read_job(job_id: str) -> JobResult:
    result = jobs.result_for(job_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No such job. It may have expired.',
        )
    return result


@router.get('/health', summary='Liveness and broker connectivity')
async def health() -> dict:
    return {'status': 'ok', 'redis': jobs.is_broker_reachable()}
