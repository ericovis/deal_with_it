"""The threaded worker, against fakeredis."""

import threading
import time

import pytest
from rq import Worker
from rq.job import Job, JobStatus

from src import jobs, worker
from src.config import get_settings
from tests import support

PAYLOAD = {'url': 'https://example.test/a.png', 'base64': None}


def _wait(check, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


class TestThreadWorker:
    def test_runs_jobs_off_the_main_thread(self, async_queue, stub_task):
        stub_task(support.SUCCESS)
        job_id, _ = jobs.enqueue(PAYLOAD)
        one = worker.build_workers(1, async_queue.name, async_queue.connection)[0]
        thread = threading.Thread(target=one.work, kwargs={'burst': True}, daemon=True)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert jobs.result_for(job_id).state == 'finished'

    def test_enforces_the_job_timeout_without_signals(self, async_queue, stub_task, monkeypatch):
        stub_task(support.DAWDLES)
        monkeypatch.setenv('DWI_JOB_TIMEOUT', '1')
        get_settings.cache_clear()
        job_id, _ = jobs.enqueue(PAYLOAD)
        one = worker.build_workers(1, async_queue.name, async_queue.connection)[0]
        thread = threading.Thread(target=one.work, kwargs={'burst': True}, daemon=True)
        started = time.monotonic()
        thread.start()
        thread.join(timeout=10)
        job = Job.fetch(job_id, connection=async_queue.connection)
        assert job.get_status() == JobStatus.FAILED
        assert 'timeout' in (job.latest_result().exc_string or '').lower()
        assert time.monotonic() - started < 4, 'the job should have been cut short'

    def test_workers_get_distinct_names(self, async_queue):
        names = {w.name for w in worker.build_workers(5, async_queue.name, async_queue.connection)}
        assert len(names) == 5

    def test_progress_lands_on_the_right_job(self, async_queue, stub_task):
        """get_current_job() is thread-local, so concurrent jobs must not
        write their checkpoints onto each other."""
        stub_task(support.REPORTS_PROGRESS)
        ids = [jobs.enqueue(PAYLOAD)[0] for _ in range(6)]
        stop = threading.Event()
        workers = worker.build_workers(3, async_queue.name, async_queue.connection)
        supervisor = threading.Thread(target=worker.serve, args=(workers, stop, 5), daemon=True)
        supervisor.start()
        assert _wait(lambda: all(jobs.result_for(i).state == 'finished' for i in ids))
        stop.set()
        supervisor.join(timeout=10)
        for job_id in ids:
            job = Job.fetch(job_id, connection=async_queue.connection)
            assert job.meta == {'progress': 42, 'step': 'Halfway up the stairs'}


class TestServe:
    def test_stops_when_asked_and_registers_deaths(self, async_queue):
        stop = threading.Event()
        workers = worker.build_workers(2, async_queue.name, async_queue.connection)
        supervisor = threading.Thread(target=worker.serve, args=(workers, stop, 1), daemon=True)
        supervisor.start()
        assert _wait(lambda: all(w.get_state() for w in workers))
        stop.set()
        supervisor.join(timeout=10)
        assert not supervisor.is_alive()
        assert Worker.all(connection=async_queue.connection) == []


@pytest.fixture(autouse=True)
def _quiet_rq(caplog):
    caplog.set_level('CRITICAL', logger='rq.worker')
