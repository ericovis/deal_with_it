"""The unit of work the RQ worker runs.

Kept deliberately dull: plain, picklable arguments in, a plain dict out. It
must not import anything web-related -- the worker container has no app.
"""

import logging
import re

from rq import get_current_job

from src import images
from src.config import get_settings
from src.errors import DealWithItError
from src.models import ImageRequest
from src.processors.deal_with_it import DealWithItProcessor

logger = logging.getLogger(__name__)

#: The one checkpoint that carries a number worth keeping. See below.
FACE_COUNT_RE = re.compile(r'^Drawing glasses on (\d+) ')


def report_progress(percent: int, step: str) -> None:
    """Record how far along we are on the job itself.

    A no-op outside a worker, so the task stays callable on its own.
    """
    job = get_current_job()
    if job is None:
        return
    job.meta['progress'] = percent
    job.meta['step'] = step
    # The face count only exists while this one step is being reported: the
    # next checkpoint overwrites 'step', and the finished card still wants to
    # say how many faces it drew on. Lifted here rather than in the reader,
    # which would be parsing a string that no longer exists.
    if match := FACE_COUNT_RE.match(step):
        job.meta['faces'] = int(match.group(1))
    job.save_meta()


def process_image(payload: dict) -> dict:
    """Fetch or decode the image, draw the glasses, return a data URI.

    Expected failures (bad URL, undecodable image, no faces) come back as an
    ``error`` string rather than an exception: the queue would otherwise
    record them as crashed jobs, and we would have to dig a user-facing
    message out of a pickled traceback. Unexpected failures *do* raise, so
    they land in RQ's failed registry where they belong.
    """
    settings = get_settings()
    request = ImageRequest.model_validate(payload)
    try:
        report_progress(10, 'Fetching the image')
        array = images.load(
            url=str(request.url) if request.url else None,
            base64_data=request.base64,
            settings=settings,
        )
        processor = DealWithItProcessor(
            array,
            max_detection_size=settings.max_detection_size,
            on_progress=report_progress,
        )
        processor.call()
    except DealWithItError as exc:
        logger.info('rejected image: %s', exc)
        return {'image': None, 'error': str(exc)}

    report_progress(90, 'Encoding the result')
    return {'image': processor.base64_output, 'error': None}
