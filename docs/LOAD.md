# Load, limits and abuse

Measured 2026-09-03 on a 4-core laptop (15 GB RAM) against a production-like
stack: Redis 7 in a container, one `uvicorn` process with a single worker,
and `python -m src.worker` with `DWI_WORKER_PROCESSES` set as noted. The
load generator ran on the same machine, so the numbers are conservative.

```
uv run uvicorn src.app:app --port 5001 --workers 1
DWI_WORKER_PROCESSES=2 uv run python -m src.worker
uv run python tests/load/stress.py throughput --jobs 40 --concurrency 8
uv run python tests/load/stress.py big --pixels 36 --quality 75
uv run python tests/load/stress.py abuse
uv run python tests/load/stress.py slowloris --sockets 300
```

`tests/load/stress.py` is not collected by pytest. Set `DWI_RATE_LIMIT=0`
for the throughput runs or the generator throttles itself; leave the
defaults on for `abuse`.

## Worker parallelism

Sample mix (one face, eight faces, twenty-nine faces, a painting), 24 jobs,
8 concurrent clients. Detection is single-threaded, so processes scale it:

| workers | finished jobs/min | end-to-end p50 |
|---------|-------------------|----------------|
| 1       | 20.1              | 23.8 s         |
| 2       | 39.8              | 11.7 s         |
| 3       | 52.8              |  8.7 s         |
| 4       | 64.4              |  6.8 s         |

Linear to two, then the web process and the generator start competing for
cores. Set `DWI_WORKER_PROCESSES` to the core count of the worker host;
`docker-compose.yml` runs two per container, and `--scale worker=N` adds
containers.

The web tier is never the bottleneck: with 40 jobs in flight, `GET
/api/jobs/{id}` answered in 3 ms (p50) and `/api/health` in 2 ms. Submit
latency for a sample-sized upload is 36 ms at p50.

## Large images

`multiple_people.jpg` upscaled, two workers, all concurrent:

| input      | on the wire | end-to-end p50 | result  |
|------------|-------------|----------------|---------|
| 12 MP JPEG | 0.9 MB      |  7.8 s         | 1.6 MB  |
| 36 MP JPEG | 1.7 MB      | 10.0 s         | 3.1 MB  |

Before results matched the input format, the same 36 MP job took 15 s on
*four* workers and returned a 22.5 MB PNG data URI: at that size the PNG
encode alone was 7.3 s, longer than detection, and Redis peaked at 455 MB
holding ten of them. A JPEG at quality 90 encodes in 0.16 s.

Peak resident memory for one job, measured in-process: 234 MB at 1.3 MP,
394 MB at 12 MP, 795 MB at the 36 MP ceiling. Budget ~1 GB per worker
process for a worst-case image; the forked work horse frees it after each
job.

## What the abuse run checks

All of these pass against the live stack:

- A declared body over `max_request_bytes` is a 413 before anything is read.
- A **chunked** body with no Content-Length is cut off at the same limit
  (413 in 30 ms) instead of being buffered whole. This was a bypass.
- A 400 MP PNG a few KB on disk is refused with the pixel-limit message.
  Pillow's own bomb ceiling used to fire first and crash the job.
- Every SSRF probe is refused with "non-public address": loopback, `0.0.0.0`,
  `[::1]`, IPv4-mapped IPv6, decimal and hex IP literals, `127.1`, the
  cloud metadata address, NAT64-embedded loopback, RFC 1918, and compose
  service names. A resolver error that was not `gaierror` used to crash the
  job. The address that is checked is now the address that is dialled
  (`images.PinnedAdapter`), which closes the DNS-rebinding window.
- 40 URL submissions from one address in a minute: 20 accepted, 20 refused
  with 429 and `Retry-After`. The queue cap answers 503 the same way.
- 300 half-open connections (slowloris) left `/api/health` at 6 ms. uvicorn
  tolerates idle sockets, but it has no header timeout, so put a reverse
  proxy with one in front for a public deployment.

## Limits and defaults

| setting                | default | what it bounds                              |
|------------------------|---------|---------------------------------------------|
| `DWI_MAX_IMAGE_BYTES`  | 10 MB   | any upload or download                      |
| `DWI_MAX_IMAGE_PIXELS` | 40 MP   | decoded size, checked from the header       |
| `DWI_RATE_LIMIT`       | 30/min  | submissions per client address (0 disables) |
| `DWI_MAX_QUEUE_DEPTH`  | 50      | waiting jobs before submissions get 503     |
| `DWI_JOB_TIMEOUT`      | 120 s   | one job's runtime                           |
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
