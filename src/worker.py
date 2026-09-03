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
from rq.timeouts import TimerDeathPenalty
from rq.worker import WorkerStatus

from src import jobs
from src.config import get_settings

logger = logging.getLogger(__name__)


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
        ThreadWorker([queue_name], connection=connection, name=f'{prefix}.{index}')
        for index in range(count)
    ]


def serve(workers: list[ThreadWorker], stop: threading.Event, grace: float) -> None:
    """Run every worker in its own thread until ``stop`` is set, then let
    running jobs finish for up to ``grace`` seconds."""
    threads = [
        threading.Thread(target=worker.work, kwargs={'with_scheduler': False},
                         name=worker.name, daemon=True)
        for worker in workers
    ]
    for thread in threads:
        thread.start()
    while not stop.wait(1):
        if not any(thread.is_alive() for thread in threads):
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
    logger.info('starting %s worker threads on %r', len(workers), queue.name)

    # `kill -USR1 <pid>` prints every thread's stack to stderr.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    serve(workers, stop, grace=settings.job_timeout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
