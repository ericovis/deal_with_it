"""The JSON API, end to end over a test client."""

import pytest

from src import jobs
from tests import support

URL_BODY = {'url': 'https://example.test/a.png'}


class TestCreateJob:
    def test_accepts_a_url_and_returns_a_job(self, client):
        response = client.post('/api/jobs', json=URL_BODY)
        assert response.status_code == 202
        body = response.json()
        assert body['job_id']
        assert body['status_url'].endswith(f'/api/jobs/{body["job_id"]}')

    def test_accepts_a_data_uri(self, client, base64_image):
        response = client.post('/api/jobs', json={'base64': base64_image})
        assert response.status_code == 202

    def test_accepts_a_body_where_the_unused_field_is_null(self, client):
        """What a JSON client naturally sends, and what the old API rejected."""
        response = client.post('/api/jobs', json={'url': URL_BODY['url'], 'base64': None})
        assert response.status_code == 202

    def test_rejects_an_empty_body(self, client):
        response = client.post('/api/jobs', json={})
        assert response.status_code == 422
        assert 'An url or a base64 string must be passed.' in response.text

    def test_rejects_both_fields(self, client, base64_image):
        response = client.post('/api/jobs', json={'url': URL_BODY['url'],
                                                  'base64': base64_image})
        assert response.status_code == 422
        assert 'not both' in response.text

    def test_rejects_an_unknown_field(self, client):
        response = client.post('/api/jobs', json={'image': 'https://example.test/a.png'})
        assert response.status_code == 422

    def test_rejects_a_base64_string_without_a_header(self, client):
        response = client.post('/api/jobs', json={'base64': 'iVBORw0KGgo='})
        assert response.status_code == 422
        assert 'data URI header' in response.text

    def test_rejects_a_malformed_url(self, client):
        assert client.post('/api/jobs', json={'url': 'not-a-url'}).status_code == 422

    def test_does_not_fetch_the_url_while_handling_the_request(self, client, monkeypatch):
        """Submitting must never block on someone else's slow server."""
        import requests

        def explode(*args, **kwargs):
            raise AssertionError('the web tier must not make outbound requests')

        monkeypatch.setattr(requests, 'get', explode)
        monkeypatch.setattr(requests, 'head', explode)
        assert client.post('/api/jobs', json=URL_BODY).status_code == 202


class TestReadJob:
    def test_returns_the_image_once_finished(self, client):
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']

        response = client.get(f'/api/jobs/{job_id}')
        assert response.status_code == 200
        body = response.json()
        assert body == {'job_id': job_id, 'state': 'finished',
                        'image': support.IMAGE, 'error': None}

    def test_reports_a_rejected_image_with_its_reason(self, client, stub_task):
        stub_task(support.REJECTS)
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']

        body = client.get(f'/api/jobs/{job_id}').json()
        assert body['state'] == 'failed'
        assert body['error'] == 'No faces were found in this image.'
        assert body['image'] is None

    def test_hides_the_details_of_a_crash(self, client, stub_task):
        stub_task(support.CRASHES)
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']

        body = client.get(f'/api/jobs/{job_id}').json()
        assert body['state'] == 'failed'
        assert body['error'] == jobs.GENERIC_FAILURE
        assert 'Traceback' not in str(body)
        assert 'the detector fell over' not in str(body)

    def test_unknown_job_is_a_404(self, client):
        response = client.get('/api/jobs/nope')
        assert response.status_code == 404
        assert 'expired' in response.json()['detail']

    def test_a_pending_job_reports_its_state(self, client, async_queue):
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']
        body = client.get(f'/api/jobs/{job_id}').json()
        assert body['state'] == 'queued'
        assert body['image'] is None


class TestHealth:
    def test_reports_the_broker(self, client):
        assert client.get('/api/health').json() == {'status': 'ok', 'redis': True}


class TestDocs:
    def test_openapi_is_served_under_api(self, client):
        response = client.get('/api/openapi.json')
        assert response.status_code == 200
        assert set(response.json()['paths']) == {
            '/api/jobs', '/api/jobs/{job_id}', '/api/health'
        }

    def test_swagger_ui_is_served(self, client):
        assert client.get('/api/docs').status_code == 200

    @pytest.mark.parametrize('path', ['/api/report.csv', '/api/notes.txt', '/api/data.json'])
    def test_api_paths_are_not_swallowed_by_the_static_handler(self, client, path):
        """FastHTML's catch-all would serve these if it were the outer app.

        A 404 from FastAPI is the right answer; a 200 with a file would mean
        the mount order had regressed.
        """
        assert client.get(path).status_code == 404
