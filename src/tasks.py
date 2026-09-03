"""The unit of work the RQ worker runs: picklable arguments in, a plain
dict out. Must not import anything web-related."""

import logging
import re

from rq import get_current_job

from src import images
from src.config import get_settings
from src.errors import DealWithItError
from src.models import ImageRequest
from src.processors.deal_with_it import DealWithItProcessor

logger = logging.getLogger(__name__)

FACE_COUNT_RE = re.compile(r'^Drawing glasses on (\d+) ')


def report_progress(percent: int, step: str) -> None:
    """Record a checkpoint on the job. A no-op outside a worker."""
    job = get_current_job()
    if job is None:
        return
    job.meta['progress'] = percent
    job.meta['step'] = step
    # Kept separately because the next checkpoint overwrites 'step'.
    if match := FACE_COUNT_RE.match(step):
        job.meta['faces'] = int(match.group(1))
    job.save_meta()


def record_faces(faces: list, detection: str) -> None:
    """Keep what was found on the job, in original-image coordinates: the
    detector's box and five points, and the 68 landmarks the glasses were
    placed from. A no-op outside a worker."""
    job = get_current_job()
    if job is None:
        return
    job.meta['landmarks'] = [face.as_record() for face in faces]
    job.meta['detection'] = detection
    job.save_meta()


def process_image(payload: dict) -> dict:
    """Fetch or decode the image, draw the glasses, return a data URI.

    Expected failures come back as an ``error`` string; anything else raises
    and lands in RQ's failed registry.
    """
    settings = get_settings()
    request = ImageRequest.model_validate(payload)
    try:
        report_progress(10, 'Fetching the image')
        array, source_format = images.load(
            url=str(request.url) if request.url else None,
            base64_data=request.base64,
            settings=settings,
        )
        processor = DealWithItProcessor(
            array,
            max_detection_size=settings.max_detection_size,
            on_progress=report_progress,
        )
        # A photo comes back as a photo: PNG-encoding a JPEG input is 7x the
        # bytes and, at phone resolution, longer than the detection itself.
        processor.img_format = 'JPEG' if source_format == 'JPEG' else 'PNG'
        processor.call()
        record_faces(processor.faces, processor.detection)
    except DealWithItError as exc:
        logger.info('rejected image: %s', exc)
        return {'image': None, 'error': str(exc)}

    report_progress(90, 'Encoding the result')
    return {'image': processor.base64_output, 'error': None}
