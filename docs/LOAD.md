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

## Getting a result to the browser

Measured 2026-09-04 on the 4-core laptop, two worker threads pinned to two
cores. What changed is where the pictures live, not how long a job takes:
a card used to inline the submitted image *and* the result, twice each — the
thumbnail, the Before half, the After half and the Download link — into the
one-second poll that swapped it in.

| upload           | job   | poll response | on the wire, all told |
|------------------|-------|---------------|-----------------------|
| 3.2 MB 36 MP JPEG| 2.6 s | 16.6 MB → 3.2 KB | 16.6 → 6.2 MB      |
| 7.9 MB 11 MP PNG | 5.3 s | 41.8 MB → 3.2 KB | 41.8 → 15.7 MB     |

Eight such cards: **194 MB before, 73 MB after**, and the 73 MB is binary
rather than base64 (a third of the saving on its own), fetched once per
picture instead of twice, in parallel, cacheable, and off the poll chain —
so a slow picture no longer holds up the card that carries it. In a browser,
queueing those eight put 371 poll responses on the wire totalling 292 KB,
the largest 2.7 KB.

This is the other half of "the results take forever": the job was done in
under three seconds and the browser still had tens of megabytes to pull,
through a proxy compressing base64 for nothing, off the cores detection
needs.

### Measured against the live site

2026-09-04, over the internet from a home connection to the VPS. One 7.9 MB
PNG through the page, nothing else in the queue:

```
upload    7.89 MB                       14.9 s
job       finished (worker log agrees)  25.0 s
card poll 41.76 MB, gzip, chunked      263.8 s
```

**The card took four and a half minutes after the worker had finished.** That
is the whole reported bug: the poll that carries the finished card *is* the
41.76 MB response, so until it lands htmx has nothing to swap in and the card
keeps showing the last state it did get — "Encoding the result · 90%", the
last checkpoint the worker writes. Meanwhile the worker is idle, which is
what `htop` shows. Queue several and it is worse, on one connection.

The link, not the CPU, is the limit: a 0.50 MB static PNG over the same link
runs at 0.18 MB/s, and refetching the same card with `Accept-Encoding:
identity` took 55.8 s against 64.5 s gzipped — so Caddy compressing base64
costs about 14%, real but secondary to simply sending 41.76 MB.

Serving the two pictures as URLs fixes the symptom outright, not just the
byte count: the card arrives in 3.2 KB and flips to Done immediately, and the
pictures stream in afterwards as ordinary images, in parallel and cached,
without the card's *state* waiting on them.

### Sized derivatives, and the store behind them

Done. The worker writes what each place needs instead of one full-resolution
file: a 160 px thumbnail, a 1600 px viewing copy used by both the card and the
full-screen view, the same for the submitted image, a 1200 px JPEG for link
previews, and the full-resolution file for download.

| upload | full-res result | what a finished card fetches |
|--------|-----------------|------------------------------|
| 3.2 MB 36 MP JPEG | 3.09 MB | **318 KB** (thumb 3.9 + view 157 + before 157) |
| 7.9 MB 11 MP PNG | 7.71 MB | **320 KB** |

Against 15.8 MB as URLs, or 42 MB inlined. The derivatives cost about 0.9 s of
worker time per job, against removing the base64 encode and megabytes of Redis
traffic.

The static side went the same way: 200 px WebP centre crops for the sample
grid (exactly what `object-fit: cover` was already showing) and 760 px figures
for the docs.

| page | before | after |
|------|--------|-------|
| `/` | 4.0 MB | **175 KB** |
| `/docs` | ~1.7 MB | **190 KB** |

Images no longer travel through Redis at all. A submission is written to a
directory both tiers mount and the queue carries a path, so `DWI_MAX_QUEUE_DEPTH`
is no longer bounded by the Redis ceiling — it is bounded by how long a queue
is worth watching.

**Disk is now the thing that fills.** Roughly 12 MB per finished job (the
full-resolution result, its WebP and JPEG siblings, and the derivatives;
the submitted source is deleted on success). At the 170 jobs/min above that is
~2 GB a minute, so on this host `DWI_BLOB_MAX_BYTES` (1 GB), not
`DWI_BLOB_TTL` (1 hour), is what binds under load — the TTL is what binds
under ordinary traffic. The sweep runs as an ordinary job on the queue every
30 s, oldest first, and will not evict anything younger than two job timeouts
in case it is still queued.

## On two cores

Measured 2026-09-04 against the live deployment: a 2 vCPU, 3.8 GB VPS with
no swap, running Redis, uvicorn, the worker (`DWI_WORKER_THREADS=2`) and
Caddy as rootless containers on the same two cores. The load generator ran
off-host over the internet, so every submit latency below includes the
upload; the worker numbers come from its own log and do not.

| run                       | throughput | end-to-end p50 | poll p50 / p95 |
|---------------------------|------------|----------------|----------------|
| 24 samples, concurrency 8 | 175/min    | 2.3 s          | 0.17 / 0.96 s  |
| 2 x 36 MP, concurrency 2  | —          | 18.5 s         | 0.16 / 16.5 s  |

The sample burst put 8.8 seconds of work through the worker (mean 0.37 s a
job, p90 0.58 s) in about seven seconds of wall time, and `vmstat` showed
both cores pinned throughout — 87-90% user, 4-6% idle. The worker's own
8.8 thread-seconds are roughly 60% of the fourteen core-seconds that burst
had to spend: on a host this size **the web tier is not free**. Caddy's
TLS and compression and uvicorn's enqueue path take the rest, and they take
it from detection.

Which is why two threads is the ceiling here rather than the three that
"cores plus one" would suggest. There is no idle CPU left for a third to
use, and the cost of trying is paid in poll latency, by the person
watching a card.

Two things worth fixing rather than measuring around:

- **Compression on a result is pure waste.** A finished job is base64
  JPEG or PNG. Fetched through Caddy's `encode zstd gzip`, a 0.37 MB
  result was 0.37 MB on the wire — nothing saved — and 0.2 s slower than
  the same fetch with `Accept-Encoding: identity`. Exclude the job
  endpoints from compression and that CPU goes back to detection.
  *Since fixed at the source:* the pictures are no longer base64 in a poll
  response at all, but binary on `/jobs/{id}/result` and `/jobs/{id}/source`,
  which Caddy skips compressing on content type alone. See below.
- **Nothing must be allowed to grow without a ceiling** on a host with no
  swap. The worker peaked at 850 MB with two 36 MP jobs in flight, against
  677 MB idle, which is fine; Redis holding queued payloads and results is
  the one that is not, and its `maxmemory` is worth checking rather than
  assuming (see below).

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
`maxmemory` to taste. Keep the queue cap and the ceiling in step with each
other: `DWI_MAX_QUEUE_DEPTH` payloads must fit inside `maxmemory`, or the
backpressure that was meant to answer 503 arrives as a 500 instead.

Confirm the ceiling is real rather than configured — `redis-cli config get
maxmemory` on the running container. It was 0 in production for a while:
the argument list said otherwise, but podman's quadlet generator drops an
empty `""` argument and everything after it, so `--save "" --maxmemory 1gb`
reached Redis as `--save`.

The rate limit keys on `request.client.host`, which behind a proxy means
whatever the proxy says. **Getting that wrong is a bypass, not a nuisance**,
and it was wrong here until 2026-09-04:

- uvicorn ran with `--forwarded-allow-ips "*"`, whose always-trust path takes
  the **first** entry of `X-Forwarded-For`.
- Both Caddy and Cloudflare *append* to a client-supplied header rather than
  replacing it. `X-Forwarded-For: 1.2.3.4` arrived as `1.2.3.4,<real>`, and
  the first entry is the client's own invention.

So anyone could pick their own bucket. Measured: forty submissions each
claiming a different address were all accepted, against a limit of thirty a
minute. The fix is to make the proxy work the client out and hand the app a
single value it did not get from the client — Caddy `trusted_proxies` with
Cloudflare's ranges plus `header_up X-Forwarded-For {client_ip}`, and uvicorn
trusting only the container subnet. The same forty now get 29 accepted and 11
refused, sharing one bucket as they should, and an ordinary submission is
unaffected.

A stale Cloudflare range list fails the safe way: visitors get bucketed as
Cloudflare, which over-limits rather than under-limits.

A crash inside native code takes the whole worker process down, threads
and all.
Run it under a restart policy; queued jobs survive in Redis and the restarted
worker picks them up. The ones that were *running* do not come back: they sit
at `started`, and the card polling them shows its last checkpoint — almost
always "Encoding the result", the last one written — until something says
otherwise. RQ's own reaping is not that something on its own: it happens in
`run_maintenance_tasks`, which runs every ten minutes and only while some
worker is alive to run it. Measured, killing a worker mid-job left the card
polling for **595 seconds**; with the worker down it never recovered at all.

Two things fix it, and both are needed:

- `_abandoned` in `src/jobs.py` reports a `started` job past `job_timeout +
  ABANDONED_GRACE` as failed, from the read side, so the page is honest with
  no worker running. The card now fails, with a Retry, 151 s after the kill.
- `ThreadWorker` reaps every `REAP_INTERVAL` (60 s) instead of RQ's 600, and
  blocks for 60 s per dequeue instead of 405 so it comes up for air to do it.
  This is what moves the job to the failed registry and gives its payload a
  TTL; abandoned jobs' hashes have no expiry of their own, so on a
  `noeviction` Redis they otherwise accumulate until writes start failing.
