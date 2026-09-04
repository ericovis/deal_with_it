"""The worker's entry point: expected failures must come back as data."""

import os
import time

import numpy as np
import pytest
from PIL import Image

from src import blobs, images, jobs, tasks
from src.images import ImageSourceError
from src.processors.deal_with_it import NoFacesFound
from tests import support


@pytest.fixture
def stub_load(monkeypatch):
    """Replace image loading so these tests need neither network nor pixels."""

    def install(result, source_format='PNG'):
        def fake_load(url=None, base64_data=None, blob=None, settings=None):
            if isinstance(result, Exception):
                raise result
            return result, source_format

        monkeypatch.setattr(images, 'load', fake_load)

    return install


@pytest.fixture
def stub_processor(monkeypatch):
    seen = {}

    def install(outcome):
        class FakeProcessor:
            img_format = 'PNG'
            faces: list = []
            detection = 'plain'

            def __init__(self, array, **kwargs):
                self.output = None
                seen['processor'] = self

            def call(self):
                if isinstance(outcome, Exception):
                    raise outcome
                self.output = outcome

        monkeypatch.setattr(tasks, 'DealWithItProcessor', FakeProcessor)
        return seen

    return install


class TestOutputFormat:
    @pytest.mark.parametrize('source_format, expected', [
        ('JPEG', 'JPEG'), ('PNG', 'PNG'), ('WEBP', 'PNG'), ('GIF', 'PNG'), ('', 'PNG'),
    ])
    def test_a_jpeg_comes_back_as_a_jpeg_and_everything_else_as_png(
            self, stub_load, stub_processor, source_format, expected):
        stub_load(np.zeros((2, 2, 3), dtype=np.uint8), source_format)
        seen = stub_processor(Image.new('RGB', (2, 2), 'red'))
        tasks.process_image({'url': 'https://example.test/a', 'base64': None})
        assert seen['processor'].img_format == expected


def test_writes_the_pictures_and_returns_where_they_went(stub_load, stub_processor, blob_root):
    """Nothing multi-megabyte goes back through Redis: the return value is a
    handful of references to files on a disk both tiers can see."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(Image.new('RGB', (8, 8), 'red'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})

    assert result['error'] is None
    assert set(result['images']) == {'full', 'view', 'thumb', 'before', 'card'}
    for reference in result['images'].values():
        assert blobs.path(reference).is_file(), reference
    assert result['downloads']['png'] == result['images']['full']
    assert 'webp' in result['downloads']
    assert result['expires_at'], 'the card counts down to this'


def test_a_small_picture_is_not_blown_up_to_fill_a_derivative(
        stub_load, stub_processor, blob_root):
    """Fitting never upscales: an 8px picture is already the right size for
    anything below it, and stretching it would cost bytes for nothing."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(Image.new('RGB', (8, 8), 'red'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})
    with Image.open(blobs.path(result['images']['view'])) as view:
        assert view.size == (8, 8)


def test_reports_a_bad_source_as_an_error_not_a_crash(stub_load):
    """A rejected URL is the user's problem, not a failed job."""
    stub_load(ImageSourceError('The URL is not an image.'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})
    assert result == {'images': None, 'error': 'The URL is not an image.'}


def test_reports_a_faceless_image_as_an_error(stub_load, stub_processor):
    """The old API returned a 500 here; before that, a 400. Now it is a message."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(NoFacesFound('No faces were found in this image.'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})
    assert result == {'images': None, 'error': 'No faces were found in this image.'}


def test_unexpected_errors_propagate(stub_load, stub_processor):
    """Bugs must reach RQ's failed registry rather than masquerade as input errors."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(RuntimeError('detector segfaulted'))

    with pytest.raises(RuntimeError, match='detector segfaulted'):
        tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})


def test_refuses_a_payload_that_names_no_source():
    with pytest.raises(ValueError, match='must be passed'):
        tasks.process_image({'url': None, 'blob': None})


class TestFaceCount:
    """The number in one checkpoint is worth keeping past that checkpoint."""

    @pytest.fixture
    def meta(self, monkeypatch):
        recorded = {}

        class FakeJob:
            meta = recorded

            def save_meta(self):
                pass

        monkeypatch.setattr(tasks, 'get_current_job', FakeJob)
        return recorded

    def test_the_drawing_step_leaves_the_count_behind(self, meta):
        tasks.report_progress(75, 'Drawing glasses on 8 faces')
        tasks.report_progress(90, 'Encoding the result')
        assert meta['faces'] == 8
        assert meta['step'] == 'Encoding the result', 'the step itself is gone by then'

    def test_one_face_is_singular(self, meta):
        tasks.report_progress(75, 'Drawing glasses on 1 face')
        assert meta['faces'] == 1

    @pytest.mark.parametrize('step', [
        'Fetching the image', 'Looking for faces', 'Encoding the result', 'Done',
    ])
    def test_no_other_step_claims_a_count(self, meta, step):
        tasks.report_progress(10, step)
        assert 'faces' not in meta


class TestRecordedFaces:
    def test_what_was_found_lands_on_the_job(self, async_queue, drain, stub_task):
        stub_task(support.RECORDS_FACES)
        job_id, _ = jobs.enqueue({'url': 'https://example.test/a.png', 'base64': None})
        drain()
        result = jobs.result_for(job_id)
        assert result.detection == 'upscaled'
        assert len(result.faces) == 1
        assert result.faces[0].box == (10, 90, 90, 10)
        assert len(result.faces[0].landmarks) == 68
        assert result.faces[0].points['nose'] == (50.0, 60.0)

    def test_outside_a_worker_it_is_a_no_op(self):
        tasks.record_faces([], 'plain')


class TestSweepingTheStore:
    """Nothing on a filesystem expires by itself, so a job goes and looks."""

    def test_it_drops_what_is_past_the_ttl(self, blob_root, monkeypatch):
        monkeypatch.setenv('DWI_BLOB_TTL', '900')
        tasks.get_settings.cache_clear()
        blobs.put('old-job-0001', 'view.webp', b'x')
        blobs.put('new-job-0001', 'view.webp', b'x')
        stale = time.time() - 7200
        os.utime(blob_root / 'old-job-0001', (stale, stale))

        assert tasks.sweep_blobs() == {'swept': 1, 'error': None}
        assert not (blob_root / 'old-job-0001').exists()
        assert (blob_root / 'new-job-0001').exists()

    def test_it_runs_as_an_ordinary_job(self, async_queue, drain, blob_root, monkeypatch):
        """The scheduler enqueues it; it is not a side effect of the
        supervisor loop, so it shows up in the queue like anything else."""
        monkeypatch.setenv('DWI_BLOB_TTL', '900')
        tasks.get_settings.cache_clear()
        blobs.put('old-job-0002', 'view.webp', b'x')
        stale = time.time() - 7200
        os.utime(blob_root / 'old-job-0002', (stale, stale))

        job_id, _ = jobs.enqueue({}, meta={})
        jobs.get_queue().enqueue('src.tasks.sweep_blobs')
        drain()
        assert not (blob_root / 'old-job-0002').exists()

    def test_it_will_not_eat_a_job_that_may_still_be_queued(self, blob_root, monkeypatch):
        monkeypatch.setenv('DWI_BLOB_MAX_BYTES', '1')
        tasks.get_settings.cache_clear()
        blobs.put('fresh-job-001', 'source.jpg', b'x' * 500)

        assert tasks.sweep_blobs() == {'swept': 0, 'error': None}
        assert (blob_root / 'fresh-job-001').exists()
