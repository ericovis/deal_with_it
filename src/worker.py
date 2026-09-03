"""Worker entry point: ``python -m src.worker``.

The forking worker, so a segfault in dlib kills only the work horse.
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
