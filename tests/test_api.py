"""The JSON API, end to end over a test client."""

import pytest

from src import api, jobs
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
    def test_returns_links_once_finished(self, client):
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']

        response = client.get(f'/api/jobs/{job_id}')
        assert response.status_code == 200
        body = response.json()
        assert body['state'] == 'finished'
        # Absolute, so a caller does not have to know where we live. Built
        # from the request rather than a configured host, which is what makes
        # it right behind the reverse proxy.
        host = 'http://testserver'
        assert body['images'] == {
            'view': f'{host}/i/{job_id}/view.webp',
            'thumb': f'{host}/i/{job_id}/thumb.webp',
            'before': f'{host}/i/{job_id}/before.webp',
            'full': f'{host}/i/{job_id}/result.png',
            'card': f'{host}/i/{job_id}/card.jpg',
        }
        assert body['downloads'] == {'png': f'{host}/i/{job_id}/result.png',
                                     'webp': f'{host}/i/{job_id}/result.webp'}
        assert body['share_url'] == f'{host}/s/{job_id}', 'a link worth passing on'
        assert body['expires_at'], 'so a caller knows how long the links last'
        assert 'image' not in body, 'the base64 data URI is gone on purpose'

    def test_the_links_it_hands_out_actually_fetch(self, client):
        """A contract that returns URLs is only worth anything if they work."""
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']
        body = client.get(f'/api/jobs/{job_id}').json()
        for url in list(body['images'].values()) + list(body['downloads'].values()):
            assert client.get(url).status_code == 200, url

    def test_reports_a_rejected_image_with_its_reason(self, client, stub_task):
        stub_task(support.REJECTS)
        job_id = client.post('/api/jobs', json=URL_BODY).json()['job_id']

        body = client.get(f'/api/jobs/{job_id}').json()
        assert body['state'] == 'failed'
        assert body['error'] == 'No faces were found in this image.'
        assert body['images'] is None

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
        assert body['images'] is None


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


class TestBodySizeLimit:
    """The image limits only bite in the worker, by which point the web tier
    has already buffered the upload and put it in Redis."""

    def test_an_oversized_body_is_refused(self, client):
        settings = jobs.get_settings()
        payload = 'x' * (settings.max_request_bytes + 1)
        response = client.post('/api/jobs', content=payload,
                               headers={'content-type': 'application/json'})
        assert response.status_code == 413
        assert 'must be under' in response.json()['detail']

    def test_a_normal_body_is_unaffected(self, client, base64_image):
        assert client.post('/api/jobs', json={'base64': base64_image}).status_code == 202

    def test_the_limit_leaves_room_for_base64_and_the_envelope(self):
        settings = jobs.get_settings()
        assert settings.max_request_bytes > settings.max_image_bytes * 4 / 3

    def test_the_page_is_protected_too(self, client, hx):
        """The middleware wraps the mounted FastHTML app as well."""
        response = client.post('/submit', content='x' * (jobs.get_settings().max_request_bytes + 1),
                               headers={'content-type': 'application/x-www-form-urlencoded',
                                        **hx})
        assert response.status_code == 413


class TestChunkedBodies:
    """A body with no Content-Length skips the cheap check, so the bytes are
    counted as they arrive instead."""

    @staticmethod
    def chunks(total: int, size: int = 64 * 1024):
        sent = 0
        while sent < total:
            piece = min(size, total - sent)
            yield b'x' * piece
            sent += piece

    def test_an_oversized_chunked_body_is_refused(self, client):
        limit = jobs.get_settings().max_request_bytes
        response = client.post('/api/jobs', content=self.chunks(limit + 1),
                               headers={'content-type': 'application/json'})
        assert response.status_code == 413
        assert response.headers['connection'] == 'close'

    def test_the_page_is_protected_too(self, client, hx):
        limit = jobs.get_settings().max_request_bytes
        response = client.post('/submit', content=self.chunks(limit + 1),
                               headers={'content-type': 'application/x-www-form-urlencoded',
                                        **hx})
        assert response.status_code == 413

    def test_a_chunked_body_under_the_limit_is_fine(self, client, base64_image):
        body = f'{{"base64": "{base64_image}"}}'.encode()
        response = client.post('/api/jobs', content=iter([body[:100], body[100:]]),
                               headers={'content-type': 'application/json'})
        assert response.status_code == 202


class TestSecurityHeaders:
    @pytest.mark.parametrize('path', ['/api/health', '/', '/docs', '/static/css/style.css'])
    def test_every_response_carries_them(self, client, path):
        response = client.get(path)
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['referrer-policy'] == 'strict-origin-when-cross-origin'

    def test_even_a_refused_body(self, client):
        too_big = 'x' * (jobs.get_settings().max_request_bytes + 1)
        response = client.post('/api/jobs', content=too_big,
                               headers={'content-type': 'application/json'})
        assert response.status_code == 413
        assert response.headers['x-content-type-options'] == 'nosniff'


class TestHandlersDoNotBlockTheLoop:
    """Every handler talks to Redis. FastAPI runs a sync ``def`` in its
    threadpool; an ``async def`` would run the round trip on the loop."""

    @pytest.mark.parametrize('handler', [api.create_job, api.read_job, api.health])
    def test_api_handlers_are_sync(self, handler):
        import inspect

        assert not inspect.iscoroutinefunction(handler)


class TestJobMetadata:
    def test_an_api_job_is_labelled_for_the_page(self, client, hx, async_queue):
        """A job created through the API still renders a sensible card."""
        job_id = client.post('/api/jobs', json={'url': 'https://example.test/a.png'}).json()['job_id']
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'example.test/a.png' in body
        assert 'src="https://example.test/a.png" alt="" class="thumb"' in body


class TestTheOpenAPISchema:
    """The schema is the documentation, for a person reading /api/docs and for
    an agent reading the JSON. Neither is served by `additionalProp1`."""

    def schema(self, client):
        return client.get('/api/openapi.json').json()

    def test_downloads_shows_real_formats_not_additionalprop(self, client):
        """A bare dict[str, str] renders as additionalProp1/2/3 in Swagger,
        which tells a reader nothing about what the keys are."""
        field = self.schema(client)['components']['schemas']['JobResult']['properties']
        [example] = field['downloads']['examples']
        assert set(example) == {'jpg', 'webp'}
        assert all(url.startswith('http') for url in example.values())

    def test_a_finished_job_has_a_whole_worked_example(self, client):
        example = self.schema(client)['components']['schemas']['JobResult']['example']
        assert example['state'] == 'finished'
        assert set(example['images']) == {'view', 'thumb', 'before', 'full', 'card'}
        assert example['expires_at'] and example['share_url']
        assert example['faces'][0]['landmarks'], 'the shape of a face, not just its box'

    def test_every_endpoint_is_described_and_grouped(self, client):
        schema = self.schema(client)
        assert {tag['name'] for tag in schema['tags']} == {'jobs', 'health'}
        for path, operations in schema['paths'].items():
            for verb, operation in operations.items():
                assert operation.get('summary'), f'{verb} {path} has no summary'
                assert operation.get('tags'), f'{verb} {path} is in no group'

    def test_the_failure_codes_say_what_they_mean(self, client):
        responses = self.schema(client)['paths']['/api/jobs']['post']['responses']
        assert 'Retry-After' in responses['429']['description']
        assert 'Retry-After' in responses['503']['description']

    def test_the_description_explains_the_two_step_flow(self, client):
        """An agent reads this before anything else."""
        description = self.schema(client)['info']['description']
        assert 'POST /api/jobs' in description
        assert 'GET /api/jobs/{job_id}' in description
        assert 'expires_at' in description, 'nothing here is durable storage'


class TestLLMsTxt:
    """https://llmstxt.org: what an agent reads before trying to use this."""

    def test_it_is_served_at_the_root_as_plain_text(self, client):
        response = client.get('/llms.txt')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/plain')

    def test_it_opens_the_way_the_convention_asks(self, client):
        lines = client.get('/llms.txt').text.splitlines()
        assert lines[0] == '# Deal With It!'
        assert any(line.startswith('> ') for line in lines[:6]), 'a summary blockquote'

    def test_it_carries_the_limits_from_the_settings(self, client, monkeypatch):
        """Generated, not committed, so a changed limit cannot leave it
        lying."""
        monkeypatch.setenv('DWI_RATE_LIMIT', '7')
        monkeypatch.setenv('DWI_BLOB_TTL', '1800')
        jobs.get_settings.cache_clear()
        body = client.get('/llms.txt').text
        assert '7 submissions' in body
        assert 'deleted after\n30 minutes' in body or '30 minutes' in body

    def test_it_points_at_the_machine_readable_contract(self, client):
        body = client.get('/llms.txt').text
        assert '/api/openapi.json' in body
        assert '/api/docs' in body

    def test_it_says_what_each_picture_is_for(self, client):
        body = client.get('/llms.txt').text
        for field in ('images.view', 'images.thumb', 'images.full', 'downloads',
                      'expires_at', 'share_url'):
            assert field in body, f'{field} is undocumented'

    def test_it_warns_that_nothing_is_private_or_durable(self, client):
        body = client.get('/llms.txt').text.lower()
        assert 'no authentication' in body
        assert 'durable storage' in body
