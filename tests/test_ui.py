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


class TestProgress:
    """A progress bar fed by checkpoints the worker records on the job."""

    def start(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        return response.text.split('hx-get="/jobs/')[1].split('"')[0]

    def test_a_queued_job_shows_an_indeterminate_bar(self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert '<progress max="100">' in body, 'no value attribute means indeterminate'
        assert 'In the queue...' in body
        assert '%' not in body, 'there is no honest percentage for a queued job'

    def test_a_running_job_shows_its_step_and_percentage(self, client, hx, monkeypatch):
        from src.models import JobResult

        monkeypatch.setattr(jobs, 'result_for', lambda job_id: JobResult(
            job_id=job_id, state='started', progress=35, step='Looking for faces'))
        body = client.get('/jobs/whatever', headers=hx).text
        assert 'value="35"' in body
        assert 'Looking for faces... 35%' in body

    def test_the_worker_records_progress_on_the_job(self, async_queue, stub_task, drain):
        """End to end: the task calls report_progress, the queue reads it back."""
        stub_task(support.REPORTS_PROGRESS)
        job_id, _ = jobs.enqueue({'url': 'https://example.test/a.png', 'base64': None})
        drain()

        from rq.job import Job
        meta = Job.fetch(job_id, connection=async_queue.connection).meta
        assert meta['progress'] == 42
        assert meta['step'] == 'Halfway up the stairs'

    def test_progress_reporting_is_a_no_op_outside_a_worker(self):
        """process_image stays callable on its own, with no job to write to."""
        from src.tasks import report_progress

        report_progress(50, 'no job here')

    def test_a_finished_job_reports_a_hundred_percent(self, client):
        job_id = self.start(client, {'hx-request': 'true'})
        body = client.get(f'/api/jobs/{job_id}').json()
        assert body['progress'] == 100
        assert body['step'] == 'Done'


class TestGallery:
    """Finished images accumulate in the page and nowhere else."""

    def start(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        return response.text.split('hx-get="/jobs/')[1].split('"')[0]

    def test_the_gallery_starts_empty(self, client):
        body = client.get('/').text
        assert 'id="gallery"' in body
        assert 'gallery-item' not in body

    def test_a_finished_image_is_appended_rather_than_replacing(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'hx-swap-oob="afterbegin:#gallery"' in body, 'newest first, nothing replaced'
        assert 'class="gallery-item"' in body

    def test_the_gallery_item_sits_inside_a_transport_envelope(self, client, hx):
        """Do not flatten this nesting.

        htmx only inserts the element carrying hx-swap-oob when the swap
        style is outerHTML; for every other style -- afterbegin included --
        it inserts that element's children instead. Put the class on the
        outer div and the wrapper is silently dropped, along with the
        :has(.gallery-item) rule that unhides the section.
        """
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text

        envelope = body[body.index('hx-swap-oob="afterbegin:#gallery"'):]
        assert 'class="gallery-item"' not in envelope[:envelope.index('>')], (
            'the envelope itself must not be the gallery item'
        )
        assert 'class="gallery-item"' in envelope

    def test_each_item_can_be_opened_and_downloaded(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert f'href="{support.IMAGE}" target="_blank"' in body
        assert f'download="deal-with-it-{job_id[:8]}.png"' in body

    def test_the_polling_slot_is_emptied_so_the_image_is_not_shown_twice(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert f'<div id="job-{job_id}"></div>' in body

    def test_nothing_is_written_to_disk(self, client, hx, tmp_path, monkeypatch):
        """The gallery is DOM-only: no file should appear anywhere."""
        monkeypatch.chdir(tmp_path)
        job_id = self.start(client, hx)
        client.get(f'/jobs/{job_id}', headers=hx)
        assert list(tmp_path.iterdir()) == []

    def test_a_failed_job_adds_nothing_to_the_gallery(self, client, hx, stub_task):
        stub_task(support.REJECTS)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'gallery-item' not in body


class TestFormReset:
    def start(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        return response.text.split('hx-get="/jobs/')[1].split('"')[0]

    def test_a_finished_job_swaps_in_an_empty_form(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'hx-swap-oob="true"' in body
        assert 'id="form"' in body
        assert 'value=' not in body.split('id="form"')[1], 'the fields come back blank'

    def test_a_failed_job_leaves_the_form_alone(self, client, hx, stub_task):
        """So a bad URL can be corrected instead of retyped."""
        stub_task(support.REJECTS)
        job_id = self.start(client, hx)
        assert 'hx-swap-oob' not in client.get(f'/jobs/{job_id}', headers=hx).text

    def test_the_form_on_the_page_is_not_marked_for_swapping(self, client):
        assert 'hx-swap-oob' not in client.get('/').text
