"""Worker entry point: ``python -m src.worker``.

Uses the forking worker rather than SimpleWorker on purpose -- if dlib
segfaults on a pathological image, only the work horse dies and the worker
picks up the next job.
"""

import logging
import sys

from rq import Queue, Worker
from rq.worker_pool import WorkerPool

from src import jobs
from src.config import get_settings


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    settings = get_settings()
    queue = jobs.get_queue()
    connection = queue.connection

    if settings.worker_processes > 1:
        logging.info('starting %s workers on %r', settings.worker_processes, queue.name)
        WorkerPool(
            [Queue(queue.name, connection=connection)],
            connection=connection,
            num_workers=settings.worker_processes,
        ).start(logging_level='INFO')
    else:
        logging.info('starting worker on %r', queue.name)
        Worker([queue.name], connection=connection).work(logging_level='INFO')
    return 0


if __name__ == '__main__':
    sys.exit(main())
