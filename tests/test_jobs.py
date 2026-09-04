"""The queue seam. Runs against fakeredis, so no server is needed."""

from datetime import UTC, datetime, timedelta

import pytest
from rq.job import Job, JobStatus

from src import jobs
from src.models import JobState
from tests import support

PAYLOAD = {'url': 'https://example.test/a.png', 'base64': None}
UPLOADED = 'data:image/png;base64,c3VuZ2xhc3Nlcw=='


class TestEnqueue:
    def test_returns_a_job_id(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, state = jobs.enqueue(PAYLOAD)
        assert isinstance(job_id, str) and job_id
        assert state.is_terminal, 'a synchronous queue runs the job immediately'

    def test_passes_the_payload_through_unchanged(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, state = jobs.enqueue(PAYLOAD)
        assert state == JobState.QUEUED
        job = Job.fetch(job_id, connection=async_queue.connection)
        assert job.args == (PAYLOAD,)

    def test_applies_the_configured_ttls(self, async_queue, stub_task):
        """RQ's defaults expire results in 8 minutes and keep failures a year."""
        stub_task(support.SUCCESS)
        settings = jobs.get_settings()
        job = Job.fetch(jobs.enqueue(PAYLOAD)[0], connection=async_queue.connection)
        assert job.result_ttl == settings.result_ttl
        assert job.failure_ttl == settings.failure_ttl
        assert job.timeout == settings.job_timeout

    def test_enqueues_the_task_by_name(self, async_queue, monkeypatch):
        """The web tier must not import the face stack to queue work."""
        job = Job.fetch(jobs.enqueue(PAYLOAD)[0], connection=async_queue.connection)
        assert job.func_name == 'src.tasks.process_image'


class TestResultFor:
    def test_unknown_job_is_none(self, sync_queue):
        assert jobs.result_for('does-not-exist') is None

    def test_finished_job_points_at_its_pictures(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        result = jobs.result_for(job_id)
        assert result.state == JobState.FINISHED
        assert result.images.view == f'/i/{job_id}/view.webp'
        assert result.images.thumb == f'/i/{job_id}/thumb.webp'
        assert result.images.full == f'/i/{job_id}/result.png'
        assert result.expires_at is not None
        assert result.error is None

    def test_a_finished_job_offers_its_downloads_by_format(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        downloads = jobs.result_for(job_id).downloads
        assert downloads['png'] == f'/i/{job_id}/result.png'
        assert downloads['webp'] == f'/i/{job_id}/result.webp'

    def test_rejected_input_is_reported_as_a_failure_with_its_message(self, sync_queue, stub_task):
        """The job succeeded; the image did not. The caller cares about the message."""
        stub_task(support.REJECTS)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.FAILED
        assert result.error == 'No faces were found in this image.'
        assert result.images is None

    def test_a_crash_is_reported_generically(self, sync_queue, stub_task, caplog):
        """A traceback is for our logs, never for the response body."""
        stub_task(support.CRASHES)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.FAILED
        assert result.error == jobs.GENERIC_FAILURE
        assert 'the detector fell over' not in (result.error or '')
        assert 'the detector fell over' in caplog.text

    def test_a_task_returning_nothing_is_a_failure_not_an_empty_success(
            self, sync_queue, stub_task):
        stub_task(support.RETURNS_NOTHING)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.FAILED
        assert result.error == jobs.GENERIC_FAILURE

    def test_a_pending_job_reports_queued(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.QUEUED
        assert result.images is None

    def test_queued_becomes_finished_once_a_worker_runs(self, async_queue, stub_task, drain):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        assert jobs.result_for(job_id).state == JobState.QUEUED

        drain()

        result = jobs.result_for(job_id)
        assert result.state == JobState.FINISHED
        assert result.images.view == f'/i/{job_id}/view.webp'


class TestConnection:
    def test_broker_reachability_is_reported(self, sync_queue):
        assert jobs.is_broker_reachable() is True

    def test_an_unreachable_broker_is_not_an_exception(self, monkeypatch):
        """/api/health has to answer even when Redis is down."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        monkeypatch.setenv('DWI_REDIS_URL', 'redis://127.0.0.1:1/0')
        jobs.reset()
        queue = jobs.get_queue()
        monkeypatch.setattr(
            queue.connection, 'ping',
            lambda *a, **kw: (_ for _ in ()).throw(RedisConnectionError('refused')),
        )
        assert jobs.is_broker_reachable() is False
        jobs.reset()

    def test_the_queue_is_built_once_and_reused(self, sync_queue):
        assert jobs.get_queue() is jobs.get_queue()

    def test_reset_forgets_the_queue(self, sync_queue):
        jobs.reset()
        assert jobs._queue is None

    def test_rq_needs_undecoded_responses(self, sync_queue):
        """decode_responses=True corrupts every pickled job RQ stores."""
        kwargs = sync_queue.connection.connection_pool.connection_kwargs
        assert not kwargs.get('decode_responses', False)


@pytest.mark.parametrize('task,expected_state', [
    (support.SUCCESS, JobState.FINISHED),
    (support.REJECTS, JobState.FAILED),
    (support.CRASHES, JobState.FAILED),
])
def test_states_are_terminal_after_the_worker_is_done(
        async_queue, stub_task, drain, task, expected_state):
    stub_task(task)
    job_id, _ = jobs.enqueue(PAYLOAD)
    drain()
    result = jobs.result_for(job_id)
    assert result.state == expected_state
    assert result.state.is_terminal


class TestMetadata:
    """What the UI hangs on a job so a card can name itself."""

    def test_meta_survives_the_round_trip(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD, meta={'kind': 'upload', 'label': 'holiday.jpg'})
        _, source = jobs.describe(job_id)
        assert source.kind == 'upload'
        assert source.label == 'holiday.jpg'

    def test_the_worker_and_the_web_tier_share_one_meta_dict(
            self, async_queue, stub_task, drain):
        """save_meta rewrites the whole dict, so progress must not evict the
        label the submitting request put there."""
        stub_task(support.REPORTS_PROGRESS)
        job_id, _ = jobs.enqueue(PAYLOAD, meta={'kind': 'url', 'label': 'example.test/a.png'})
        drain()
        result, source = jobs.describe(job_id)
        assert source.label == 'example.test/a.png'
        assert result.progress == 100

    def test_a_job_with_no_meta_is_described_from_its_payload(self, sync_queue, stub_task):
        """The JSON API records nothing, and its jobs still render."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        _, source = jobs.describe(job_id)
        assert source.kind == 'url'
        assert source.label == 'example.test/a.png'
        assert source.thumb == PAYLOAD['url']

    def test_an_upload_with_no_meta_gets_a_stand_in_name(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue({'url': None, 'base64': UPLOADED})
        _, source = jobs.describe(job_id)
        assert source.kind == 'upload'
        assert source.label == 'Uploaded image'
        assert source.thumb is None, (
            'an upload arrives with nothing to show; the card takes the '
            'thumbnail off the finished result instead'
        )

    @pytest.mark.parametrize('url,expected', [
        ('https://example.test/a.png', 'example.test/a.png'),
        ('https://example.test/', 'example.test'),
        ('https://example.test', 'example.test'),
    ])
    def test_a_url_is_labelled_by_host_and_path(self, url, expected):
        assert jobs.label_for({'url': url, 'base64': None}) == expected

    def test_describe_is_none_for_an_unknown_id(self, sync_queue):
        assert jobs.describe('long-gone') is None

    def test_the_face_count_reaches_the_card(self, async_queue, stub_task, drain):
        stub_task(support.COUNTS_FACES)
        job_id, _ = jobs.enqueue(PAYLOAD)
        drain()
        _, source = jobs.describe(job_id)
        assert source.faces == 8


class TestPayloadAndRetry:
    def test_payload_for_returns_what_was_submitted(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        assert jobs.payload_for(job_id) == PAYLOAD

    def test_payload_for_is_none_for_an_unknown_id(self, sync_queue):
        assert jobs.payload_for('long-gone') is None

    def test_resubmit_queues_the_same_payload_as_a_new_job(self, sync_queue, stub_task):
        stub_task(support.REJECTS)
        job_id, _ = jobs.enqueue(PAYLOAD, meta={'kind': 'sample', 'label': 'socks_the_cat.jpg'})

        stub_task(support.SUCCESS)
        new_id, _ = jobs.resubmit(job_id)

        assert new_id != job_id
        assert jobs.payload_for(new_id) == PAYLOAD
        assert jobs.result_for(new_id).state == JobState.FINISHED

    def test_resubmit_keeps_the_label_but_not_the_old_progress(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(
            PAYLOAD, meta={'kind': 'sample', 'label': 'socks_the_cat.jpg', 'faces': 3})
        new_id, _ = jobs.resubmit(job_id)
        _, source = jobs.describe(new_id)
        assert source.label == 'socks_the_cat.jpg'
        assert source.kind == 'sample'

    def test_resubmit_is_none_for_an_unknown_id(self, sync_queue):
        assert jobs.resubmit('long-gone') is None


class TestQueuePosition:
    """How long a queued job still has to wait, in jobs."""

    def test_the_first_job_in_is_next_up(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue({'url': 'https://example.com/a.png', 'base64': None})
        assert jobs.result_for(job_id).ahead == 0

    def test_each_job_counts_the_ones_in_front_of_it(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        ids = [jobs.enqueue({'url': f'https://example.com/{n}.png', 'base64': None})[0]
               for n in range(3)]
        assert [jobs.result_for(job_id).ahead for job_id in ids] == [0, 1, 2]

    def test_the_queue_shortens_as_jobs_are_taken(self, async_queue, stub_task, drain):
        stub_task(support.SUCCESS)
        first, second = (jobs.enqueue({'url': f'https://example.com/{n}.png',
                                       'base64': None})[0] for n in range(2))
        assert jobs.result_for(second).ahead == 1
        drain(max_jobs=1)
        assert jobs.result_for(first).state == JobState.FINISHED
        assert jobs.result_for(second).ahead == 0

    def test_a_job_nobody_is_waiting_behind_reports_nothing(self, sync_queue, stub_task):
        """A finished job is out of the list, so there is no position to give."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue({'url': 'https://example.com/a.png', 'base64': None})
        assert jobs.result_for(job_id).ahead is None

    def test_the_position_rides_along_with_describe(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        jobs.enqueue({'url': 'https://example.com/a.png', 'base64': None})
        job_id, _ = jobs.enqueue({'url': 'https://example.com/b.png', 'base64': None},
                                 meta={'kind': 'url', 'label': 'b.png'})
        result, source = jobs.describe(job_id)
        assert (result.ahead, source.label) == (1, 'b.png')


class TestLightPolling:
    """A pending job is polled once a second per card, and its hash holds the
    whole image. Polls must read only status and meta."""

    BIG = {'url': None, 'base64': 'data:image/png;base64,' + 'A' * 100_000}

    @pytest.fixture
    def no_full_fetch(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError('Job.fetch reads the whole payload')

        monkeypatch.setattr(jobs.Job, 'fetch', refuse)

    def test_a_queued_job_is_described_without_its_payload(
            self, async_queue, stub_task, no_full_fetch):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(self.BIG, meta={'kind': 'upload', 'label': 'a.png'})
        result, source = jobs.describe(job_id)
        assert result.state == JobState.QUEUED
        assert source.label == 'a.png'
        assert source.thumb is None, 'nothing to show until the worker writes one'

    def test_a_queued_job_is_read_without_its_payload(
            self, async_queue, stub_task, no_full_fetch):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(self.BIG)
        assert jobs.result_for(job_id).state == JobState.QUEUED

    def test_progress_still_comes_through(self, async_queue, stub_task, no_full_fetch):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(self.BIG)
        connection = async_queue.connection
        # Write progress the way the worker does, without a worker.
        job = jobs.Job(job_id, connection=connection)
        job.refresh = lambda: None
        job.meta = {'progress': 42, 'step': 'Halfway'}
        job.save_meta()
        result = jobs.result_for(job_id)
        assert (result.progress, result.step) == (42, 'Halfway')

    def test_a_finished_job_still_loads_everything(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(self.BIG, meta={'kind': 'upload', 'label': 'a.png'})
        result, source = jobs.describe(job_id)
        assert result.state == JobState.FINISHED
        assert result.images.view == f'/i/{job_id}/view.webp'

    def test_an_unknown_id_is_none(self, async_queue, no_full_fetch):
        assert jobs.describe('nope') is None
        assert jobs.result_for('nope') is None


class TestAbandonedJobs:
    """A worker can die mid-job -- a deploy, a restart, a crash in native
    code, the OOM killer -- and the job it was running stays ``started`` in
    Redis with nobody coming back to it. RQ only reaps those in a
    maintenance pass it runs every ten minutes, and only while some worker
    is alive to run one, so the reader has to be able to tell on its own.
    """

    @staticmethod
    def _started(connection, job_id: str, seconds_ago: float) -> None:
        """Put a job in the state a killed worker leaves behind."""
        started = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        connection.hset(Job.key_for(job_id), mapping={
            'status': JobStatus.STARTED.value,
            'started_at': started.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        })

    def test_a_job_still_running_inside_its_timeout_is_left_alone(
            self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        self._started(async_queue.connection, job_id, seconds_ago=5)
        assert jobs.result_for(job_id).state == JobState.STARTED

    def test_a_job_past_its_timeout_is_reported_as_failed(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        settings = jobs.get_settings()
        job_id, _ = jobs.enqueue(PAYLOAD)
        self._started(async_queue.connection, job_id,
                      seconds_ago=settings.job_timeout + jobs.ABANDONED_GRACE + 1)
        result = jobs.result_for(job_id)
        assert result.state == JobState.FAILED
        assert result.error == jobs.ABANDONED

    def test_the_grace_is_respected(self, async_queue, stub_task):
        """A job exactly at its timeout may still be writing its result."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        self._started(async_queue.connection, job_id,
                      seconds_ago=jobs.get_settings().job_timeout + 1)
        assert jobs.result_for(job_id).state == JobState.STARTED

    def test_describe_reports_it_too_and_keeps_the_label(self, async_queue, stub_task):
        """The card that has been polling needs to stop, and to say why."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD, meta={'kind': 'url', 'label': 'a.png'})
        self._started(async_queue.connection, job_id, seconds_ago=1000)
        result, source = jobs.describe(job_id)
        assert (result.state, result.error) == (JobState.FAILED, jobs.ABANDONED)
        assert source.label == 'a.png'

    def test_an_abandoned_job_can_be_retried(self, async_queue, stub_task):
        """Its payload is still on the job, which is the whole point."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        self._started(async_queue.connection, job_id, seconds_ago=1000)
        new_id, state = jobs.resubmit(job_id)
        assert new_id != job_id and state == JobState.QUEUED

    def test_deciding_costs_no_extra_round_trip(self, async_queue, stub_task):
        """The verdict rides in the pipeline a poll already makes."""
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue({'url': None,
                                  'base64': 'data:image/png;base64,' + 'A' * 100_000})
        self._started(async_queue.connection, job_id, seconds_ago=1000)

        def refuse(*args, **kwargs):
            raise AssertionError('Job.fetch reads the whole payload')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(jobs.Job, 'fetch', refuse)
            assert jobs.result_for(job_id).error == jobs.ABANDONED

    def test_a_finished_job_is_never_mistaken_for_an_abandoned_one(
            self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        connection = sync_queue.connection
        # A job that ran, long ago, and whose result is still readable.
        old = datetime.now(UTC) - timedelta(seconds=10_000)
        connection.hset(Job.key_for(job_id), 'started_at',
                        old.strftime('%Y-%m-%dT%H:%M:%S.%fZ'))
        assert jobs.result_for(job_id).state == JobState.FINISHED
