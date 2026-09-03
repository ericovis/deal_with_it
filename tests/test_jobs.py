"""The queue seam. Runs against fakeredis, so no server is needed."""

import pytest
from rq.job import Job

from src import jobs
from src.models import JobState
from tests import support

PAYLOAD = {'url': 'https://example.test/a.png', 'base64': None}


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

    def test_finished_job_carries_the_image(self, sync_queue, stub_task):
        stub_task(support.SUCCESS)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.FINISHED
        assert result.image == support.IMAGE
        assert result.error is None

    def test_rejected_input_is_reported_as_a_failure_with_its_message(self, sync_queue, stub_task):
        """The job succeeded; the image did not. The caller cares about the message."""
        stub_task(support.REJECTS)
        result = jobs.result_for(jobs.enqueue(PAYLOAD)[0])
        assert result.state == JobState.FAILED
        assert result.error == 'No faces were found in this image.'
        assert result.image is None

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
        assert result.image is None

    def test_queued_becomes_finished_once_a_worker_runs(self, async_queue, stub_task, drain):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        assert jobs.result_for(job_id).state == JobState.QUEUED

        drain()

        result = jobs.result_for(job_id)
        assert result.state == JobState.FINISHED
        assert result.image == support.IMAGE


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
