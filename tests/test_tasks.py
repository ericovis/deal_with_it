"""The worker's entry point: expected failures must come back as data."""

import numpy as np
import pytest

from src import images, tasks
from src.images import ImageSourceError
from src.processors.deal_with_it import NoFacesFound


@pytest.fixture
def stub_load(monkeypatch):
    """Replace image loading so these tests need neither network nor pixels."""

    def install(result):
        def fake_load(url=None, base64_data=None, settings=None):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(images, 'load', fake_load)

    return install


@pytest.fixture
def stub_processor(monkeypatch):
    def install(outcome):
        class FakeProcessor:
            def __init__(self, array, **kwargs):
                self.base64_output = None

            def call(self):
                if isinstance(outcome, Exception):
                    raise outcome
                self.base64_output = outcome

        monkeypatch.setattr(tasks, 'DealWithItProcessor', FakeProcessor)

    return install


def test_returns_the_processed_image(stub_load, stub_processor):
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor('data:image/png;base64,AAAA')

    assert tasks.process_image({'url': 'https://example.test/a.png', 'base64': None}) == {
        'image': 'data:image/png;base64,AAAA',
        'error': None,
    }


def test_reports_a_bad_source_as_an_error_not_a_crash(stub_load):
    """A rejected URL is the user's problem, not a failed job."""
    stub_load(ImageSourceError('The URL is not an image.'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})
    assert result == {'image': None, 'error': 'The URL is not an image.'}


def test_reports_a_faceless_image_as_an_error(stub_load, stub_processor):
    """The old API returned a 500 here; before that, a 400. Now it is a message."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(NoFacesFound('No faces were found in this image.'))

    result = tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})
    assert result == {'image': None, 'error': 'No faces were found in this image.'}


def test_unexpected_errors_propagate(stub_load, stub_processor):
    """Bugs must reach RQ's failed registry rather than masquerade as input errors."""
    stub_load(np.zeros((4, 4, 3), dtype=np.uint8))
    stub_processor(RuntimeError('detector segfaulted'))

    with pytest.raises(RuntimeError, match='detector segfaulted'):
        tasks.process_image({'url': 'https://example.test/a.png', 'base64': None})


def test_revalidates_the_payload_it_was_given():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        tasks.process_image({'url': None, 'base64': None})
