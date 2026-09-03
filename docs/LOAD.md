# Load, limits and abuse

Measured 2026-09-03 on a 4-core laptop (15 GB RAM) against a production-like
stack: Redis 7 in a container, one `uvicorn` process with a single worker,
and one `python -m src.worker` process with `DWI_WORKER_THREADS` as noted.
The load generator ran on the same machine, so the numbers are conservative.

```
uv run uvicorn src.app:app --port 5001 --workers 1
DWI_WORKER_THREADS=5 uv run python -m src.worker
uv run python tests/load/stress.py throughput --jobs 32 --concurrency 16
uv run python tests/load/stress.py big --pixels 36 --quality 75
uv run python tests/load/stress.py abuse
uv run python tests/load/stress.py slowloris --sockets 300
```

`tests/load/stress.py` is not collected by pytest. Set `DWI_RATE_LIMIT=0`
for the throughput runs or the generator throttles itself; leave the
defaults on for `abuse`.

## Worker threads

Sample mix (one face, eight faces, twenty-nine faces, a painting), 60 jobs,
16 concurrent clients, one worker process. YuNet detection plus dlib's
68-point landmarks per face, which is what ships:

| threads | finished jobs/min | end-to-end p50 | worker peak RSS |
|---------|-------------------|----------------|-----------------|
| 2       | 229               | 3.7 s          | 557 MB          |
| 5       | 307               | 2.8 s          | 1135 MB         |

YuNet alone, before the landmark pass was added, for the record:

| threads | finished jobs/min | end-to-end p50 | worker peak RSS |
|---------|-------------------|----------------|-----------------|
| 2       | 282               | 3.1 s          | 466 MB          |
| 5       | 375               | 2.3 s          | 1090 MB         |
| 8       | 369               | 2.3 s          | 1477 MB         |

OpenCV releases the GIL, so threads run detection in parallel, and its own
thread pool is pinned to one thread so it does not fight the worker's. Core
count plus one is the throughput sweet spot; beyond it threads contend for
cores and every extra one costs a full job's working set when busy. The
default is two: 280 jobs/min in under half a gigabyte is more than a small
deployment needs, and it leaves the cores to the web tier and Redis. End-to-end here
is dominated by queueing, since 16 clients feed 5 threads: a single job is
under a second.

For the record, two earlier configurations on the same mix: the forking RQ
worker with dlib did 20 jobs/min per process, because its work horse
resolved the task by name after the fork and re-imported the models on
every job; the threaded worker with dlib's own detector did 151 jobs/min
on five threads. YuNet detects in a third of the time; the landmark pass
that follows costs about 20 ms per face and loads a 100 MB model once.

The web tier is never the bottleneck: with 32 jobs in flight, `GET
/api/jobs/{id}` answered in 3 ms (p50) and `/api/health` in 2 to 4 ms. It
runs one event loop; the Redis-touching handlers use Starlette's threadpool,
which idles at zero threads and is capped at 40. Those threads only wait on
Redis, so the cap does not need to match the worker's.

## Large images

`multiple_people.jpg` upscaled, five threads, all jobs concurrent:

| input      | on the wire | jobs | end-to-end p50 | result | worker peak RSS |
|------------|-------------|------|----------------|--------|-----------------|
| 12 MP JPEG | 0.9 MB      | 10   | 2.9 s          | 1.6 MB | —               |
| 36 MP JPEG | 1.7 MB      | 5    | 6.0 s          | 3.1 MB | 2.9 GB          |

A job's working set is about 400 MB at 12 MP and 800 MB at the 36 MP
ceiling, so the worker's peak is roughly `threads × 800 MB` plus the models
under a flood of maximum-size images. Budget 4 GB for five threads, 2 GB
for the default two, or lower `DWI_MAX_IMAGE_PIXELS`.

Before results matched the input format, a 36 MP job returned a 22.5 MB PNG
data URI and the encode alone took 7.3 s, longer than detection. A JPEG at
quality 90 encodes in 0.16 s.

## What the abuse run checks

All of these pass against the live stack, with zero tracebacks in the worker
log:

- A declared body over `max_request_bytes` is a 413 before anything is read.
- A **chunked** body with no Content-Length is cut off at the same limit
  (413 in 30 ms) instead of being buffered whole. This was a bypass.
- A 400 MP PNG a few KB on disk is refused with the pixel-limit message.
  Pillow's own bomb ceiling used to fire first and crash the job.
- Every SSRF probe is refused with "non-public address": loopback, `0.0.0.0`,
  `[::1]`, IPv4-mapped IPv6, decimal and hex IP literals, `127.1`, the
  cloud metadata address, NAT64-embedded loopback, RFC 1918, and compose
  service names. A resolver error that was not `gaierror` used to crash the
  job. The address that is checked is the address that is dialled
  (`images.PinnedAdapter`), which closes the DNS-rebinding window.
- A server that drips bytes cannot hold a worker thread past
  `DWI_FETCH_DEADLINE` (30 s), redirects included.
- 40 URL submissions from one address in a minute: 20 accepted, 20 refused
  with 429 and `Retry-After`. The queue cap answers 503 the same way.
- 300 half-open connections (slowloris) left `/api/health` at 6 ms. uvicorn
  tolerates idle sockets, but it has no header timeout, so put a reverse
  proxy with one in front for a public deployment.

## Limits and defaults

| setting                | default | what it bounds                              |
|------------------------|---------|---------------------------------------------|
| `DWI_WORKER_THREADS`   | 2       | concurrent jobs in the worker process       |
| `DWI_MAX_IMAGE_BYTES`  | 10 MB   | any upload or download                      |
| `DWI_MAX_IMAGE_PIXELS` | 40 MP   | decoded size, checked from the header       |
| `DWI_RATE_LIMIT`       | 30/min  | submissions per client address (0 disables) |
| `DWI_MAX_QUEUE_DEPTH`  | 50      | waiting jobs before submissions get 503     |
| `DWI_JOB_TIMEOUT`      | 120 s   | one job's runtime                           |
| `DWI_FETCH_DEADLINE`   | 30 s    | one URL download, redirects included        |
| `DWI_RESULT_TTL`       | 900 s   | how long a result waits to be collected     |
| Redis `maxmemory`      | 1 GB    | set in compose, `noeviction`                |

At the defaults the worst case in Redis is 50 queued payloads of 13 MB plus
results at 900 s: well over 1 GB if every submission is a 10 MB image. With
`noeviction`, Redis then refuses writes and submissions fail with a 500
rather than losing queued jobs silently. Lower `DWI_RESULT_TTL` or raise
`maxmemory` to taste.

The rate limit keys on `request.client.host`. Behind a proxy, run uvicorn
with `--proxy-headers --forwarded-allow-ips <proxy>` (the `web` image
target already does) or every client shares one budget.

A crash inside native code takes the whole worker process down, threads
and all.
Run it under a restart policy; queued jobs survive in Redis, and the ones
that were running land in the failed registry once their heartbeat expires,
where the page offers a Retry.
