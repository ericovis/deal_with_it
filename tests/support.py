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
RECORDS_FACES = 'tests.support.records_faces'

SUCCEEDS_AS_JPEG = 'tests.support.succeeds_as_jpeg'

#: Recognisable stand-in bytes. Nothing here decodes them; the tests that
#: care about real pixels use the real processor.
PIXELS = b'sunglasses'


def written(img_format: str = 'PNG') -> dict:
    """What `src.tasks.process_image` returns, without the face stack.

    These stubs run inside a real worker, so they write real files into
    whatever DWI_BLOB_DIR the test set -- a manifest pointing at nothing
    would not exercise the paths that matter.
    """
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from rq import get_current_job

    from src import blobs
    from src.config import get_settings

    job = get_current_job()
    job_id = job.id if job is not None else f'detached-{uuid4().hex}'
    native = 'jpg' if img_format == 'JPEG' else 'png'
    images = {
        'full': blobs.put(job_id, f'result.{native}', PIXELS),
        'view': blobs.put(job_id, 'view.webp', PIXELS),
        'thumb': blobs.put(job_id, 'thumb.webp', PIXELS),
        'before': blobs.put(job_id, 'before.webp', PIXELS),
    }
    expires = datetime.now(UTC) + timedelta(seconds=get_settings().blob_ttl)
    return {
        'images': images,
        'downloads': {native: images['full'],
                      'webp': blobs.put(job_id, 'result.webp', PIXELS)},
        'expires_at': expires.isoformat(),
        'error': None,
    }


def succeeds(payload: dict) -> dict:
    return written()


def succeeds_as_jpeg(payload: dict) -> dict:
    return written('JPEG')


def rejects(payload: dict) -> dict:
    """What process_image does with an input it cannot use."""
    return {'images': None, 'error': 'No faces were found in this image.'}


def crashes(payload: dict) -> dict:
    raise RuntimeError('the detector fell over')


def returns_nothing(payload: dict) -> dict:
    return {'images': None, 'error': None}


def reports_progress(payload: dict) -> dict:
    """Exercises the real progress plumbing from inside a worker."""
    from src.tasks import report_progress

    report_progress(42, 'Halfway up the stairs')
    return written()


def dawdles(payload: dict) -> dict:
    """Takes a few seconds, in small steps, so a timeout can interrupt it."""
    import time

    for _ in range(500):
        time.sleep(0.01)
    return written()


class _FakeFace:
    def as_record(self):
        return {'box': [10, 90, 90, 10], 'score': 0.9,
                'points': {'left_eye': (30.0, 40.0), 'right_eye': (70.0, 40.0),
                           'nose': (50.0, 60.0), 'mouth_left': (35.0, 75.0),
                           'mouth_right': (65.0, 75.0)},
                'landmarks': [[float(i), float(i)] for i in range(68)]}


def records_faces(payload: dict) -> dict:
    """What the real task does after drawing: keeps the faces on the job."""
    from src.tasks import record_faces

    record_faces([_FakeFace()], 'upscaled')
    return written()


def counts_faces(payload: dict) -> dict:
    """Reports the one checkpoint that carries a number worth keeping."""
    from src.tasks import report_progress

    report_progress(75, 'Drawing glasses on 8 faces')
    report_progress(90, 'Writing the pictures')
    return written()
