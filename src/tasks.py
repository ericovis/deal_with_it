"""The unit of work the RQ worker runs.

Kept deliberately dull: plain, picklable arguments in, a plain dict out. It
must not import anything web-related -- the worker container has no app.
"""

import logging

from src import images
from src.config import get_settings
from src.errors import DealWithItError
from src.models import ImageRequest
from src.processors.deal_with_it import DealWithItProcessor

logger = logging.getLogger(__name__)


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
        array = images.load(
            url=str(request.url) if request.url else None,
            base64_data=request.base64,
            settings=settings,
        )
        processor = DealWithItProcessor(array, max_detection_size=settings.max_detection_size)
        processor.call()
    except DealWithItError as exc:
        logger.info('rejected image: %s', exc)
        return {'image': None, 'error': str(exc)}

    return {'image': processor.base64_output, 'error': None}
