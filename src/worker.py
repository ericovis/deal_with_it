"""Worker entry point: ``python -m src.worker``.

One process, ``DWI_WORKER_THREADS`` threads. OpenCV releases the GIL, so
threads give the throughput of as many processes for a fraction of the
memory: the interpreter and the model are loaded once. The trade is
isolation -- a crash in native code takes every thread down, and the
container restarts.
"""

import faulthandler
import logging
import os
import signal
import socket
import sys
import threading
import time

from rq import SimpleWorker
from rq.cron import CronScheduler
from rq.timeouts import TimerDeathPenalty
from rq.worker import WorkerStatus

from src import jobs, tasks
from src.config import get_settings

logger = logging.getLogger(__name__)


#: How long a thread blocks in one BLMOVE before coming up for air, and how
#: often it then reaps. RQ's defaults are 405 and 600 seconds, which between
#: them leave a job whose worker died mid-flight sitting at ``started`` --
#: holding its payload in a Redis with no room to spare -- for up to a
#: quarter of an hour. Waking a thread once a minute costs one command.
REAP_INTERVAL = 60


class ThreadWorker(SimpleWorker):
    """A SimpleWorker that may run off the main thread.

    ``work()`` normally installs signal handlers and enforces ``job_timeout``
    with SIGALRM; both are main-thread-only. The timer penalty raises the
    timeout into the thread instead. It cannot interrupt a native call, so a
    job stuck inside native code ends when that call returns.
    """

    death_penalty_class = TimerDeathPenalty

    def _install_signal_handlers(self) -> None:
        pass


def build_workers(count: int, queue_name: str, connection) -> list[ThreadWorker]:
    prefix = f'{socket.gethostname()}.{os.getpid()}'
    return [
        ThreadWorker(
            [queue_name], connection=connection, name=f'{prefix}.{index}',
            # `dequeue_timeout` is `worker_ttl - 15`, and maintenance only
            # runs between dequeues, so the two have to move together.
            worker_ttl=REAP_INTERVAL + 15, maintenance_interval=REAP_INTERVAL,
        )
        for index in range(count)
    ]


class ThreadScheduler(CronScheduler):
    """A CronScheduler that may run off the main thread.

    Same reason as :class:`ThreadWorker`: ``start()`` installs signal
    handlers, and those are main-thread-only.
    """

    def _install_signal_handlers(self) -> None:
        pass


def build_scheduler(queue_name: str, connection, interval: int) -> ThreadScheduler:
    """The clock behind the blob sweep.

    Nothing on a filesystem expires by itself, so something has to go and
    look. It is enqueued as an ordinary job rather than done inline here, so
    it is visible in the queue, bounded by ``job_timeout``, and logged like
    every other job.
    """
    scheduler = ThreadScheduler(connection=connection)
    scheduler.register(tasks.sweep_blobs, queue_name, interval=interval)
    return scheduler


def serve(workers: list[ThreadWorker], stop: threading.Event, grace: float,
          scheduler: CronScheduler | None = None) -> None:
    """Run every worker in its own thread until ``stop`` is set, then let
    running jobs finish for up to ``grace`` seconds."""
    threads = [
        threading.Thread(target=worker.work, kwargs={'with_scheduler': False},
                         name=worker.name, daemon=True)
        for worker in workers
    ]
    # Watched separately from the workers: it fails differently. A dead
    # worker means nothing is processed, which is loud. A dead scheduler
    # means jobs keep succeeding while nothing sweeps the store, which on a
    # host with no swap surfaces as a full disk much later.
    cron = (threading.Thread(target=scheduler.start, name='cron', daemon=True)
            if scheduler is not None else None)
    for thread in ([*threads, cron] if cron else threads):
        thread.start()
    while not stop.wait(1):
        if cron is not None and not cron.is_alive():
            logger.error('the blob sweep scheduler has exited; the store will grow')
            cron = None
        if threads and not any(thread.is_alive() for thread in threads):
            logger.error('every worker thread has exited')
            break

    for worker in workers:
        worker._stop_requested = True
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(
            worker.get_state() == WorkerStatus.BUSY for worker in workers):
        time.sleep(0.2)
    for worker in workers:
        worker.register_death()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    settings = get_settings()
    queue = jobs.get_queue()
    workers = build_workers(settings.worker_threads, queue.name, queue.connection)
    scheduler = build_scheduler(queue.name, queue.connection, settings.blob_sweep_interval)
    logger.info('starting %s worker threads on %r, sweeping the blob store every %ss',
                len(workers), queue.name, settings.blob_sweep_interval)

    # `kill -USR1 <pid>` prints every thread's stack to stderr.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    serve(workers, stop, grace=settings.job_timeout, scheduler=scheduler)
    return 0


if __name__ == '__main__':
    sys.exit(main())
