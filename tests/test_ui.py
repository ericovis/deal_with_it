"""The FastHTML interface, over a test client."""

import pytest

from src import jobs
from tests import support
from tests.conftest import make_png


def submit(client, hx, url='', file=None):
    files = {'image': file} if file else None
    return client.post('/submit', data={'url': url}, files=files, headers=hx)


class TestPage:
    def test_serves_a_full_document(self, client):
        response = client.get('/')
        assert response.status_code == 200
        assert '<!doctype html>' in response.text.lower()

    @pytest.mark.parametrize('needle', [
        'Deal With It!', 'How it works', 'Try it', 'Api Docs', 'License',
    ])
    def test_the_sections_survived_the_rewrite(self, client, needle):
        assert needle in client.get('/').text

    def test_loads_our_stylesheet_and_not_pico(self, client):
        body = client.get('/').text
        assert '/static/css/style.css' in body
        assert 'pico' not in body.lower()

    def test_ships_htmx(self, client):
        assert 'htmx.org' in client.get('/').text

    def test_has_no_javascript_of_its_own(self, client):
        """The old page hand-rolled fetch() and spinner toggling in main.js."""
        assert 'main.js' not in client.get('/').text

    def test_the_form_posts_to_submit_over_htmx(self, client):
        body = client.get('/').text
        assert 'hx-post="/submit"' in body
        assert 'hx-encoding="multipart/form-data"' in body
        assert 'hx-target="#result"' in body

    def test_points_at_the_api_docs_rather_than_a_dead_host(self, client):
        body = client.get('/').text
        assert '/api/docs' in body
        assert 'herokuapp.com' not in body

    def test_static_assets_are_served(self, client):
        for path in ('/static/css/style.css', '/static/img/glasses.png'):
            assert client.get(path).status_code == 200

    @pytest.mark.parametrize('path', [
        '/tests/fixtures/img.base64.txt', '/README.txt', '/src/static/css/style.css',
    ])
    def test_repository_files_are_not_served_from_the_working_directory(self, client, path):
        """FastHTML's default static route is a bare FileResponse over the CWD.

        It matches any path ending in one of ~50 asset extensions, so leaving
        it in place would publish chunks of the repo. It is stripped in favour
        of a StaticFiles mount rooted at src/static.
        """
        assert client.get(path).status_code == 404


class TestSubmit:
    def test_a_url_starts_a_job_and_returns_a_poller(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        assert response.status_code == 200
        assert 'hx-get="/jobs/' in response.text
        assert 'hx-trigger="load delay:1s"' in response.text
        assert 'hx-swap="outerHTML"' in response.text

    def test_an_upload_starts_a_job(self, client, hx):
        response = submit(client, hx, file=('me.png', make_png(), 'image/png'))
        assert response.status_code == 200
        assert 'hx-get="/jobs/' in response.text

    def test_an_upload_wins_over_a_blank_url_field(self, client, hx):
        """The browser posts both fields every time; only one is filled in."""
        response = submit(client, hx, url='', file=('me.png', make_png(), 'image/png'))
        assert 'hx-get="/jobs/' in response.text

    def test_submitting_nothing_explains_itself(self, client, hx):
        response = submit(client, hx)
        assert 'No image or URL was provided!' in response.text
        assert 'hx-get' not in response.text, 'nothing to poll for'

    def test_a_blank_file_input_is_not_treated_as_a_file(self, client, hx):
        """An untouched file input arrives as an empty string, not None."""
        response = client.post('/submit', data={'url': '', 'image': ''}, headers=hx)
        assert 'No image or URL was provided!' in response.text

    def test_a_non_image_upload_is_refused(self, client, hx):
        response = submit(client, hx, file=('notes.txt', b'hello', 'text/plain'))
        assert 'does not look like an image' in response.text

    def test_an_empty_upload_is_refused(self, client, hx):
        response = submit(client, hx, file=('empty.png', b'', 'image/png'))
        assert 'empty' in response.text

    def test_an_oversized_upload_is_refused_before_it_reaches_the_queue(
            self, client, hx, monkeypatch):
        monkeypatch.setenv('DWI_MAX_IMAGE_BYTES', '1024')
        jobs.get_settings.cache_clear()
        response = submit(client, hx, file=('big.png', b'x' * 2048, 'image/png'))
        assert 'bigger than' in response.text

    def test_a_malformed_url_is_reported_as_a_sentence(self, client, hx):
        """ValidationError is a ValueError, so str() on it leaks a report."""
        response = submit(client, hx, url='definitely not a url')
        assert response.status_code == 200
        assert 'Input should be a valid URL' in response.text
        assert 'validation error for ImageRequest' not in response.text
        assert 'hx-get' not in response.text


class TestPolling:
    def start(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        return response.text.split('hx-get="/jobs/')[1].split('"')[0]

    def test_a_finished_job_swaps_in_the_image_and_stops_polling(self, client, hx):
        job_id = self.start(client, hx)
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert f'src="{support.IMAGE}"' in response.text
        assert 'hx-get' not in response.text, 'the poll chain must end here'

    def test_a_pending_job_keeps_polling(self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert 'hx-get="/jobs/' in response.text
        assert 'In the queue' in response.text

    def test_a_pending_job_becomes_an_image_after_the_worker_runs(
            self, client, hx, async_queue, stub_task, drain):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        drain()
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert f'src="{support.IMAGE}"' in response.text

    def test_a_rejected_image_shows_the_reason_and_stops_polling(
            self, client, hx, stub_task):
        stub_task(support.REJECTS)
        job_id = self.start(client, hx)
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert 'No faces were found in this image.' in response.text
        assert 'hx-get' not in response.text

    def test_a_crash_shows_a_generic_message(self, client, hx, stub_task):
        stub_task(support.CRASHES)
        job_id = self.start(client, hx)
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert jobs.GENERIC_FAILURE in response.text
        assert 'the detector fell over' not in response.text

    def test_an_expired_job_says_so(self, client, hx):
        response = client.get('/jobs/long-gone', headers=hx)
        assert 'expired' in response.text
        assert 'hx-get' not in response.text

    def test_fragments_are_not_wrapped_in_the_page_shell(self, client, hx):
        job_id = self.start(client, hx)
        response = client.get(f'/jobs/{job_id}', headers=hx)
        assert '<html' not in response.text
        assert '<header' not in response.text
