import pytest
from pydantic import ValidationError

from src.models import ImageRequest, JobState


def messages(exc: pytest.ExceptionInfo) -> str:
    return ' '.join(error['msg'] for error in exc.value.errors())


class TestImageRequest:
    def test_accepts_a_url(self):
        request = ImageRequest(url='https://example.test/a.png')
        assert request.base64 is None

    def test_accepts_a_data_uri(self, base64_image):
        request = ImageRequest(base64=base64_image)
        assert request.url is None

    def test_requires_a_source(self):
        with pytest.raises(ValidationError) as exc:
            ImageRequest()
        assert 'An url or a base64 string must be passed.' in messages(exc)

    def test_refuses_both_sources(self, base64_image):
        with pytest.raises(ValidationError) as exc:
            ImageRequest(url='https://example.test/a.png', base64=base64_image)
        assert 'not both' in messages(exc)

    def test_accepts_a_payload_where_the_unused_source_is_null(self, base64_image):
        """The bug that broke the demo form for years.

        The old validator tested for key *presence*, so a browser sending
        {"url": null, "base64": "..."} -- which is what the page always sent
        -- was rejected as having passed both.
        """
        request = ImageRequest.model_validate({'url': None, 'base64': base64_image})
        assert request.base64 == base64_image

    def test_refuses_a_null_only_payload(self):
        with pytest.raises(ValidationError) as exc:
            ImageRequest.model_validate({'url': None, 'base64': None})
        assert 'An url or a base64 string must be passed.' in messages(exc)

    def test_refuses_a_base64_string_without_a_header(self):
        with pytest.raises(ValidationError) as exc:
            ImageRequest(base64='iVBORw0KGgoAAAANSUhEUg==')
        assert 'data URI header' in messages(exc)

    def test_refuses_a_malformed_url(self):
        with pytest.raises(ValidationError):
            ImageRequest(url='string')

    def test_refuses_unknown_fields(self):
        with pytest.raises(ValidationError) as exc:
            ImageRequest.model_validate({'image': 'https://example.test/a.png'})
        assert 'Extra inputs are not permitted' in messages(exc)

    def test_does_no_network_io_while_validating(self, monkeypatch):
        """Validation must stay pure -- it runs on the web tier's event loop."""
        import requests

        def explode(*args, **kwargs):
            raise AssertionError('validation must not touch the network')

        monkeypatch.setattr(requests, 'get', explode)
        monkeypatch.setattr(requests, 'head', explode)
        ImageRequest(url='https://example.test/a.png')

    def test_payload_is_json_serialisable(self):
        payload = ImageRequest(url='https://example.test/a.png').as_payload()
        assert payload == {'url': 'https://example.test/a.png', 'base64': None}
        assert all(isinstance(value, (str, type(None))) for value in payload.values())

    def test_payload_round_trips_through_validation(self, base64_image):
        """The worker re-validates what the queue handed it."""
        payload = ImageRequest(base64=base64_image).as_payload()
        assert ImageRequest.model_validate(payload).base64 == base64_image


class TestJobState:
    @pytest.mark.parametrize('state,terminal', [
        (JobState.QUEUED, False),
        (JobState.STARTED, False),
        (JobState.DEFERRED, False),
        (JobState.SCHEDULED, False),
        (JobState.FINISHED, True),
        (JobState.FAILED, True),
        (JobState.STOPPED, True),
        (JobState.CANCELED, True),
    ])
    def test_terminal_states(self, state, terminal):
        assert state.is_terminal is terminal

    def test_serialises_as_a_plain_string(self):
        assert f'{JobState.FINISHED}' == 'finished'
