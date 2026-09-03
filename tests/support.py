"""Stand-in job functions.

RQ resolves job functions by import path, so these have to live in a module a
worker could really import -- a closure or a function defined in a test body
cannot be enqueued.
"""

SUCCESS = 'tests.support.succeeds'
REJECTS = 'tests.support.rejects'
CRASHES = 'tests.support.crashes'
RETURNS_NOTHING = 'tests.support.returns_nothing'
REPORTS_PROGRESS = 'tests.support.reports_progress'
COUNTS_FACES = 'tests.support.counts_faces'
DAWDLES = 'tests.support.dawdles'

IMAGE = 'data:image/png;base64,c3VuZ2xhc3Nlcw=='
JPEG_IMAGE = 'data:image/jpeg;base64,c3VuZ2xhc3Nlcw=='
SUCCEEDS_AS_JPEG = 'tests.support.succeeds_as_jpeg'


def succeeds(payload: dict) -> dict:
    return {'image': IMAGE, 'error': None}


def succeeds_as_jpeg(payload: dict) -> dict:
    return {'image': JPEG_IMAGE, 'error': None}


def rejects(payload: dict) -> dict:
    """What process_image does with an input it cannot use."""
    return {'image': None, 'error': 'No faces were found in this image.'}


def crashes(payload: dict) -> dict:
    raise RuntimeError('the detector fell over')


def returns_nothing(payload: dict) -> dict:
    return {'image': None, 'error': None}


def reports_progress(payload: dict) -> dict:
    """Exercises the real progress plumbing from inside a worker."""
    from src.tasks import report_progress

    report_progress(42, 'Halfway up the stairs')
    return {'image': IMAGE, 'error': None}


def dawdles(payload: dict) -> dict:
    """Takes a few seconds, in small steps, so a timeout can interrupt it."""
    import time

    for _ in range(500):
        time.sleep(0.01)
    return {'image': IMAGE, 'error': None}


def counts_faces(payload: dict) -> dict:
    """Reports the one checkpoint that carries a number worth keeping."""
    from src.tasks import report_progress

    report_progress(75, 'Drawing glasses on 8 faces')
    report_progress(90, 'Encoding the result')
    return {'image': IMAGE, 'error': None}
