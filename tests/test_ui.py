"""The FastHTML interface, over a test client."""

import re

import pytest

from src import jobs
from src.models import JobResult, JobSource, JobState, SourceKind
from tests import support
from tests.conftest import make_png


def submit(client, hx, url='', files=None, sample=None):
    data = {'url': url}
    if sample is not None:
        data = {'sample': sample}
    return client.post('/submit', data=data, files=files, headers=hx)


def job_ids(body: str) -> list[str]:
    return re.findall(r'<article[^>]*\bid="job-([^"]+)"', body)


class TestPage:
    def test_serves_a_full_document(self, client):
        response = client.get('/')
        assert response.status_code == 200
        assert '<!doctype html>' in response.text.lower()

    def test_the_page_is_the_app_and_the_prose_moved_to_docs(self, client):
        """The single long page became two: submitting here, reading there."""
        body = client.get('/').text
        assert 'This session' in body
        assert 'href="/docs"' in body
        assert 'face_recognition' not in body, 'the prose lives on /docs now'
        assert 'Api Docs' not in body

    @pytest.mark.parametrize('needle', [
        'id="queue"', 'class="dropzone"', 'id="files"', 'id="url"', 'id="url-hint"',
        'id="health"', 'id="theme"', 'id="theme-state"', 'class="empty"',
    ])
    def test_the_furniture_is_all_there(self, client, needle):
        assert needle in client.get('/').text

    def test_offers_every_sample(self, client):
        from src.ui import SAMPLES

        body = client.get('/').text
        for name in SAMPLES:
            assert f'hx-vals=\'{{"sample": "{name}"}}\'' in body
        assert body.count('class="sample"') == len(SAMPLES)

    def test_the_queue_starts_empty(self, client):
        body = client.get('/').text
        assert '<div id="queue"></div>' in body, 'the :empty rule needs no whitespace inside'
        assert '<article' not in body

    def test_loads_our_stylesheet_and_not_pico(self, client):
        body = client.get('/').text
        assert '/static/css/style.css' in body
        assert 'pico' not in body.lower()

    def test_ships_htmx(self, client):
        assert 'htmx.org' in client.get('/').text

    def test_has_no_javascript_of_its_own(self, client):
        """The old page hand-rolled fetch() and spinner toggling in main.js."""
        body = client.get('/').text
        assert 'main.js' not in body
        assert all('src=' in tag for tag in re.findall(r'<script[^>]*>', body)), (
            'the app page carries no inline script at all'
        )

    def test_the_form_posts_to_submit_over_htmx(self, client):
        body = client.get('/').text
        assert 'hx-post="/submit"' in body
        assert 'hx-encoding="multipart/form-data"' in body
        assert 'hx-target="#queue"' in body
        assert 'hx-swap="afterbegin"' in body

    def test_the_file_input_takes_several_files(self, client):
        assert re.search(r'<input type="file"[^>]*multiple', client.get('/').text)

    def test_the_url_field_validates_as_it_is_typed(self, client):
        body = client.get('/').text
        assert 'hx-post="/validate"' in body
        assert 'hx-trigger="input changed delay:400ms"' in body

    def test_social_card_urls_are_relative_by_default(self, client):
        """The page used to hardcode a deployment host that stopped existing."""
        assert 'content="/static/img/deal_with_me.png"' in client.get('/').text

    def test_social_card_urls_are_absolute_when_a_public_url_is_configured(
            self, client, monkeypatch):
        monkeypatch.setenv('DWI_PUBLIC_URL', 'https://example.test/')
        jobs.get_settings.cache_clear()
        body = client.get('/').text
        assert 'content="https://example.test/static/img/deal_with_me.png"' in body

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


class TestDocsPage:
    def test_renders(self, client):
        response = client.get('/docs')
        assert response.status_code == 200
        assert 'How it works' in response.text

    def test_walks_through_the_api_in_five_snippets(self, client):
        body = client.get('/docs').text
        assert body.count('class="snippet"') == 5
        assert 'href="/api/docs"' in body

    def test_the_only_script_is_the_clipboard_line(self, client):
        body = client.get('/docs').text
        assert body.count('hx-on:click=') == 5
        assert all('src=' in tag for tag in re.findall(r'<script[^>]*>', body))

    def test_keeps_the_attribution_for_the_group_photo(self, client):
        """It is public domain, not unattributed."""
        assert 'NASA' in client.get('/docs').text

    def test_both_pages_share_the_header(self, client):
        for path in ('/', '/docs'):
            body = client.get(path).text
            assert 'id="health"' in body
            assert 'id="theme"' in body


class TestSubmit:
    def test_a_url_starts_a_job_and_returns_a_card(self, client, hx):
        response = submit(client, hx, url='https://example.test/a.png')
        assert response.status_code == 200
        assert len(job_ids(response.text)) == 1
        assert 'example.test/a.png' in response.text, 'the card names what was submitted'

    def test_an_upload_starts_a_job(self, client, hx):
        response = submit(client, hx, files=[('image', ('me.png', make_png(), 'image/png'))])
        [job_id] = job_ids(response.text)
        assert jobs.payload_for(job_id)['base64'].startswith('data:image/png;base64,')
        assert 'me.png' in response.text

    def test_several_files_become_several_jobs(self, client, hx):
        response = submit(client, hx, files=[
            ('image', ('one.png', make_png(), 'image/png')),
            ('image', ('two.png', make_png(), 'image/png')),
        ])
        ids = job_ids(response.text)
        assert len(ids) == 2
        assert all(jobs.result_for(job_id) is not None for job_id in ids)
        assert 'one.png' in response.text and 'two.png' in response.text

    def test_a_submission_clears_the_form(self, client, hx):
        body = submit(client, hx, url='https://example.test/a.png').text
        assert 'hx-swap-oob="true"' in body
        assert 'id="form"' in body

    def test_an_upload_wins_over_a_blank_url_field(self, client, hx):
        """The browser posts both fields every time; only one is filled in."""
        response = submit(client, hx, url='',
                          files=[('image', ('me.png', make_png(), 'image/png'))])
        assert len(job_ids(response.text)) == 1

    def test_submitting_nothing_answers_in_the_hint(self, client, hx):
        response = submit(client, hx)
        assert 'No image or URL was provided!' in response.text
        assert 'id="url-hint"' in response.text
        assert '<article' not in response.text, 'no job, so no card'

    def test_a_blank_file_input_is_not_treated_as_a_file(self, client, hx):
        """An untouched file input arrives as an empty string, not None."""
        response = client.post('/submit', data={'url': '', 'image': ''}, headers=hx)
        assert 'No image or URL was provided!' in response.text

    def test_a_bad_url_answers_in_the_hint_so_it_can_be_corrected(self, client, hx):
        response = submit(client, hx, url='definitely not a url')
        assert 'Only http and https URLs are supported.' in response.text
        assert 'hx-swap-oob="true"' in response.text, 'the hint has to find its own slot'
        assert '<article' not in response.text

    @pytest.mark.parametrize('name,body,message', [
        ('a non-image', ('notes.txt', b'hello', 'text/plain'), 'does not look like an image'),
        ('an empty file', ('empty.png', b'', 'image/png'), 'empty'),
    ])
    def test_a_refused_file_comes_back_as_a_failed_card(self, client, hx, name, body, message):
        """One bad file among six should not hide the five that worked."""
        response = submit(client, hx, files=[('image', body)])
        assert message in response.text
        assert 'class="chip failed"' in response.text
        assert 'class="retry"' not in response.text, 'there is no job to retry'

    def test_an_oversized_upload_is_refused_before_it_reaches_the_queue(
            self, client, hx, monkeypatch):
        monkeypatch.setenv('DWI_MAX_IMAGE_BYTES', '1024')
        jobs.get_settings.cache_clear()
        response = submit(client, hx, files=[('image', ('big.png', b'x' * 2048, 'image/png'))])
        assert 'bigger than' in response.text
        assert not job_ids(response.text)


class TestSamples:
    def test_a_sample_is_queued_as_base64_not_as_a_url(self, client, hx):
        """The worker's SSRF guard would refuse to fetch our own host."""
        response = submit(client, hx, sample='me')
        [job_id] = job_ids(response.text)
        payload = jobs.payload_for(job_id)
        assert payload['url'] is None
        assert payload['base64'].startswith('data:image/jpeg;base64,')

    def test_a_sample_card_shows_the_static_file_rather_than_the_data_uri(self, client, hx):
        """It is the same picture at a thousandth of the bytes."""
        body = submit(client, hx, sample='group').text
        assert 'src="/static/img/multiple_people.jpg"' in body

    def test_an_unknown_sample_is_refused(self, client, hx):
        response = submit(client, hx, sample='../../etc/passwd')
        assert 'No image or URL was provided!' in response.text
        assert not job_ids(response.text)
        assert 'id="rejected-' in response.text, 'a card, but not one pretending to be a job'

    def test_a_sample_does_not_wipe_a_half_typed_url(self, client, hx):
        assert 'hx-swap-oob' not in submit(client, hx, sample='me').text


class TestValidate:
    @pytest.mark.parametrize('url,expected', [
        ('', ''),
        ('   ', ''),
        ('ftp://example.test/a.png', 'Only http and https URLs are supported.'),
        ('not a url at all', 'Only http and https URLs are supported.'),
        ('http:///a.png', 'The URL has no host.'),
        ('http://localhost/a.png',
         "The host 'localhost' resolves to a non-public address, which is not allowed."),
        ('http://192.168.1.5/a.png',
         "The host '192.168.1.5' resolves to a non-public address, which is not allowed."),
        ('http://printer.local/a.png',
         "The host 'printer.local' resolves to a non-public address, which is not allowed."),
        ('https://example.test/a.png', 'Looks good. Press Enter or Deal with it.'),
    ])
    def test_says_what_is_wrong(self, client, hx, url, expected):
        body = client.post('/validate', data={'url': url}, headers=hx).text
        assert 'id="url-hint"' in body
        assert expected in body

    def test_a_good_url_and_a_bad_one_are_told_apart_by_class(self, client, hx):
        ok = client.post('/validate', data={'url': 'https://example.test/a.png'}, headers=hx)
        bad = client.post('/validate', data={'url': 'ftp://example.test/a'}, headers=hx)
        assert 'class="ok"' in ok.text
        assert 'class="error"' in bad.text

    def test_it_never_resolves_a_hostname(self, client, hx, monkeypatch):
        """A handler that waited on DNS would block the event loop."""
        import socket

        def explode(*args, **kwargs):
            raise AssertionError('the web tier must not resolve anything')

        monkeypatch.setattr(socket, 'getaddrinfo', explode)
        client.post('/validate', data={'url': 'https://example.test/a.png'}, headers=hx)


class TestCards:
    def start(self, client, hx):
        return job_ids(submit(client, hx, url='https://example.test/a.png').text)[0]

    def test_a_finished_job_shows_the_image_and_stops_polling(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert f'src="{support.IMAGE}"' in body
        assert 'class="chip done"' in body
        assert 'hx-get="/jobs/' not in body, 'the poll chain must end here'

    def test_a_finished_card_compares_before_and_after_without_script(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'class="before"' in body and 'class="after"' in body
        assert f'value="after" checked id="view-{job_id}-a"' in body
        assert 'class="segmented"' in body

    def test_each_result_can_be_downloaded(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert f'download="deal-with-it-{job_id[:8]}.png"' in body

    def test_the_full_size_view_is_an_overlay_not_a_link(self, client, hx):
        """A data: URI cannot be navigated to -- browsers block it -- so the
        old target="_blank" link did nothing at all."""
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'target="_blank"' not in body
        assert f'id="full-{job_id}"' in body
        assert body.count(f'for="full-{job_id}"') == 3, 'open, backdrop and close'
        assert 'class="lightbox-close"' in body

    def test_the_overlay_reuses_the_one_copy_of_the_image(self, client, hx):
        """Duplicating the markup would send the whole data URI twice."""
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert body.count(f'src="{support.IMAGE}"') == 1

    def test_a_queued_job_keeps_polling(self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert f'hx-get="/jobs/{job_id}"' in body
        assert 'hx-trigger="load delay:1s"' in body
        assert 'In the queue' in body

    def test_a_queued_job_becomes_a_result_after_the_worker_runs(
            self, client, hx, async_queue, stub_task, drain):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        drain()
        assert f'src="{support.IMAGE}"' in client.get(f'/jobs/{job_id}', headers=hx).text

    def test_a_rejected_image_shows_the_reason_and_offers_a_retry(self, client, hx, stub_task):
        stub_task(support.REJECTS)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'No faces were found in this image.' in body
        assert 'class="chip failed"' in body
        assert f'hx-post="/jobs/{job_id}/retry"' in body
        assert 'hx-get="/jobs/' not in body

    def test_a_crash_shows_a_generic_message(self, client, hx, stub_task):
        stub_task(support.CRASHES)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert jobs.GENERIC_FAILURE in body
        assert 'the detector fell over' not in body

    def test_an_expired_job_says_so_and_offers_nothing(self, client, hx):
        body = client.get('/jobs/long-gone', headers=hx).text
        assert 'expired' in body
        assert 'hx-get="/jobs/' not in body
        assert 'class="retry"' not in body, 'there is no payload left to resubmit'

    def test_every_card_can_be_removed(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert 'hx-target="closest article"' in body
        assert 'hx-swap="delete"' in body

    def test_fragments_are_not_wrapped_in_the_page_shell(self, client, hx):
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert '<html' not in body
        assert '<header' not in body

    def test_nothing_is_written_to_disk(self, client, hx, tmp_path, monkeypatch):
        """The session list is DOM-only: no file should appear anywhere."""
        monkeypatch.chdir(tmp_path)
        job_id = self.start(client, hx)
        client.get(f'/jobs/{job_id}', headers=hx)
        assert list(tmp_path.iterdir()) == []


class TestPollingPayloads:
    """What a card carries while it is still refetching itself every second."""

    def test_an_upload_does_not_resend_itself_on_every_poll(
            self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        body = submit(client, hx,
                      files=[('image', ('me.png', make_png(), 'image/png'))]).text
        card = body.split('<article')[1]
        assert 'data:image/png;base64,' not in card, (
            'a data URI thumbnail would be the whole upload, once a second'
        )
        assert '<div class="thumb"></div>' in card

    def test_a_url_job_shows_its_thumbnail_straight_away(
            self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        body = submit(client, hx, url='https://example.test/a.png').text
        assert 'src="https://example.test/a.png" alt="" class="thumb"' in body


class TestProgress:
    """A progress bar fed by checkpoints the worker records on the job."""

    def start(self, client, hx):
        return job_ids(submit(client, hx, url='https://example.test/a.png').text)[0]

    def test_a_queued_job_shows_an_indeterminate_bar(self, client, hx, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id = self.start(client, hx)
        body = client.get(f'/jobs/{job_id}', headers=hx).text
        assert '<progress max="100">' in body, 'no value attribute means indeterminate'
        assert 'In the queue' in body
        assert '%' not in body, 'there is no honest percentage for a queued job'

    def test_a_running_job_shows_its_step_and_percentage(self, client, hx, monkeypatch):
        monkeypatch.setattr(jobs, 'describe', lambda job_id: (
            JobResult(job_id=job_id, state='started', progress=35, step='Looking for faces'),
            JobSource(kind=SourceKind.URL, label='example.test/a.png'),
        ))
        body = client.get('/jobs/whatever', headers=hx).text
        assert 'value="35"' in body
        assert 'Looking for faces · 35%' in body
        assert 'class="chip working"' in body

    def test_a_finished_card_says_how_many_faces_it_drew_on(self, client, hx, monkeypatch):
        monkeypatch.setattr(jobs, 'describe', lambda job_id: (
            JobResult(job_id=job_id, state=JobState.FINISHED, image=support.IMAGE,
                      progress=100, step='Done'),
            JobSource(kind=SourceKind.SAMPLE, label='multiple_people.jpg',
                      thumb='/static/img/multiple_people.jpg', faces=8),
        ))
        assert 'Sample picture · 8 faces' in client.get('/jobs/whatever', headers=hx).text

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


class TestRetry:
    def start_failing(self, client, hx, stub_task):
        stub_task(support.REJECTS)
        return job_ids(submit(client, hx, sample='earth').text)[0]

    def test_a_retry_queues_the_same_picture_again(self, client, hx, stub_task):
        job_id = self.start_failing(client, hx, stub_task)
        original = jobs.payload_for(job_id)

        stub_task(support.SUCCESS)
        response = client.post(f'/jobs/{job_id}/retry', headers=hx)
        [new_id] = job_ids(response.text)

        assert new_id != job_id, 'a retry is a new job, not a resurrected one'
        assert jobs.payload_for(new_id) == original
        assert 'blue_marble.jpg' in response.text, 'and it remembers what it was called'

    def test_retrying_an_expired_job_says_so(self, client, hx):
        body = client.post('/jobs/long-gone/retry', headers=hx).text
        assert 'expired' in body
        assert 'class="retry"' not in body


class TestClearing:
    def test_remove_and_clear_swap_against_an_empty_body(self, client):
        response = client.get('/empty')
        assert response.status_code == 200
        assert response.text == ''

    def test_the_page_wires_clear_to_it(self, client):
        body = client.get('/').text
        assert 'hx-get="/empty" hx-swap="innerHTML" hx-target="#queue"' in body


class TestHealth:
    def test_the_pill_reports_a_reachable_broker(self, client):
        assert 'workers ready' in client.get('/health').text

    def test_the_pill_reports_an_unreachable_one(self, client, monkeypatch):
        monkeypatch.setattr(jobs, 'is_broker_reachable', lambda: False)
        body = client.get('/health').text
        assert 'queue unreachable' in body
        assert 'class="pill down"' in body

    def test_the_pill_refreshes_itself(self, client):
        assert 'hx-trigger="every 30s"' in client.get('/').text

    def test_the_page_renders_the_real_state_rather_than_a_placeholder(self, client,
                                                                       monkeypatch):
        monkeypatch.setattr(jobs, 'is_broker_reachable', lambda: False)
        assert 'queue unreachable' in client.get('/').text


class TestTheme:
    def test_the_switch_records_a_choice_in_a_cookie(self, client):
        response = client.post('/theme', data={'theme': 'dark'})
        assert response.cookies['dwi_theme'] == 'dark'
        assert 'data-theme="dark"' in response.text

    def test_an_unchecked_switch_means_light(self, client):
        response = client.post('/theme', data={})
        assert response.cookies['dwi_theme'] == 'light'
        assert 'data-theme="light"' in response.text

    def test_the_next_page_load_agrees(self, client):
        client.post('/theme', data={'theme': 'dark'})
        body = client.get('/').text
        assert 'data-theme="dark"' in body
        assert re.search(r'<input type="checkbox"[^>]*checked', body)

    def test_no_cookie_leaves_the_operating_system_to_decide(self, client):
        """An absent attribute is what lets prefers-color-scheme through."""
        body = client.get('/').text
        assert '<span hidden id="theme-state"></span>' in body
        assert 'data-theme=' not in body

    def test_a_junk_cookie_is_ignored(self, client):
        client.cookies.set('dwi_theme', 'chartreuse')
        assert 'data-theme=' not in client.get('/').text
        client.cookies.clear()
