# CLAUDE.md — deal_with_it

Context for working on this codebase. If something here contradicts the
code, the code wins — say so.

## What it is

A web app + JSON API that pastes "Deal With It" sunglasses onto every face in
a picture. Submitting an image enqueues a background job; the page polls until
the worker is done.

Face detection is YuNet, a 230 KB ONNX model (`src/models/`, MIT) run
through OpenCV's DNN module, with two fallback passes (upscaled, then
contrast-equalised, at a stricter score) when the plain one finds nothing.
Each face then goes to dlib's 68-point shape predictor, inside a square
built from YuNet's points (`predictor_box`) rather than YuNet's own box,
which is the wrong shape for a model trained on dlib's detector. The
glasses are placed from those landmarks with the original rule: rotated by
the line between the outer eye corners, anchored on the left eye's outer
corner, sized from that span rather than the detector's box. The nose tip's
offset along the eye line gives the turn (yaw), which tapers the frame. See
`_place` in `src/processors/deal_with_it.py`. Both point sets are stored on
the job (`meta['landmarks']`) and returned by the API as `faces`.

## Architecture

```
        POST /api/jobs                      RQ / Redis
browser ───────────────► web (FastAPI+FastHTML) ──────► worker (rq)
        ◄─────────────── 202 {job_id}                     │
        GET  /api/jobs/{id}  (htmx polls every 1s)         │ fetch, detect, paste
        ◄─────────────── {state, image}  ◄─────────────────┘
```

The web tier never touches an image: it validates the request shape, puts a
payload on the queue and reads job state back. The task is enqueued **by
name** (`src/jobs.py:TASK`) so the web process never imports OpenCV.

```
src/app.py          Composition root: FastAPI outer app, FastHTML mounted at /
src/api.py          The JSON API router, mounted under /api
src/ui.py           The FastHTML interface: both pages, fragments, routes
src/jobs.py         The queue seam: enqueue / describe / resubmit / health
src/throttle.py     Per-client rate limit and queue backpressure
src/worker.py       `python -m src.worker` — the RQ worker entry point
src/tasks.py        process_image(): what the worker runs
src/images.py       Fetching (SSRF-guarded) and decoding into a numpy array
src/models.py       Pydantic v2 request/response schemas. Shape only
src/config.py       Settings, all DWI_-prefixed env vars
src/errors.py       DealWithItError: failures whose message is safe to show
src/processors/     BaseProcessor + DealWithItProcessor
src/static/         style.css, the sample photos, glasses.svg
tests/              Hermetic: no network, no Redis server, no fixtures on disk
```

Invariants worth knowing before editing:

- **FastAPI is the outer app.** FastHTML's static catch-all would otherwise
  swallow `/api/anything.csv`. `create_ui()` strips that route and `src/app.py`
  mounts `StaticFiles` instead.
- **Handlers must not block the event loop.** Redis calls happen in sync
  `def` handlers (threadpool) or via `run_in_threadpool`. Polls read only the
  `status` and `meta` hash fields; the full job (with its image payload) is
  fetched once, when it is terminal.
- **The dropzone posts one file per request** (`UPLOAD_ONE_BY_ONE` in
  `src/ui.py`): a plain `hx-post` on a multiple file input sends the whole
  selection in one body, so nothing appears until the last picture has
  uploaded. It is one of the two lines of script in the interface — htmx has
  no attribute that splits a file input. A file post therefore answers with
  the card alone: the script empties the input itself, and an out-of-band
  form swap would tear out the element the rest of the batch posts from.
  `/submit` still takes a list, because the submit button can post one.
- **A polling card must not carry a data URI.** `JobSource.thumb` is a
  remote URL or a `/static` path; an upload has neither until it finishes.
- **Progress is stage markers** written to `job.meta` by the worker. The face
  count is lifted out of the "Drawing glasses on N faces" step because the
  next checkpoint overwrites `step`.
- **A queued job's wait is `LPOS` on the queue list** — the index *is* the
  number of jobs ahead, and `_peek` reads it in the same pipeline as the
  status and meta, so a poll still costs one round trip. It comes back as
  `ahead` on `JobResult` and is None once a worker has the job, which is
  exactly when `progress`/`step` start saying something.
- **The theme is a hidden `<span id="theme-state" data-theme=...>`** swapped
  by `POST /theme`. `style.css` reads only that span; an absent attribute lets
  `prefers-color-scheme` decide. Do not drive it from `:checked`.
- **`glasses.svg` is the only copy of the artwork.** The worker rasterises it
  per face, in memory, at the width that face needs.
- **The detector is thread-local** (`_detector` in
  `src/processors/deal_with_it.py`): `setInputSize` mutates it. The shape
  predictor is shared; it is thread-safe. OpenCV's own thread pool is pinned
  to one thread so it does not fight the worker's.
- **The samples are base64 through the queue**, not `/static` URLs: the
  worker's SSRF guard refuses our own (non-public) host. Same reason the API
  example on `/docs` points at Wikimedia unless `DWI_PUBLIC_URL` is set.
- **Sample captions are pinned** by `SAMPLE_FACES` in
  `tests/test_processor.py`. YuNet finds every human face in the samples at
  `SCORE_THRESHOLD` and never a cat, but it does find dogs (three poker
  players, Nelson), and the captions say so.
- The session list is DOM-only. Reloading loses it, on purpose.

## Running it

```
docker compose up                      # http://localhost:5000, docs at /api/docs
docker compose run --rm web pytest
docker compose up --scale worker=3     # more detection throughput
```

`docker` is aliased to `podman`; `docker compose` is `podman-compose`. Image
names are fully qualified because Podman will not guess a registry.
`docker compose restart <service>` does not work under podman-compose when the
service has a healthcheck dependency; use `docker compose up -d` again.

The `web` service reloads on edit; the `worker` does not. Restart it after
touching `src/tasks.py`, `src/images.py` or `src/processors/`.

Locally, without containers:

```
uv sync --all-extras
uv run pytest
docker compose up -d redis && uv run python -m src.worker &
uv run uvicorn src.app:app --reload --port 5000
```

The worker is one process with `DWI_WORKER_THREADS` threads (default 2).
Detection releases the GIL, so threads scale like processes at a fraction
of the memory. `src/worker.py:ThreadWorker` is a `SimpleWorker` with the
signal handlers removed and the timeout enforced by a timer, because both
are main-thread-only. A crash in native code takes the whole process down;
let the container restart. Measured numbers are in `docs/LOAD.md`.

## Dependencies

- `opencv-python-headless` for the detector. Headless on purpose: the GUI
  build drags in X libraries the slim image does not have. The model file
  is vendored in `src/models/` from opencv_zoo (MIT).
- `dlib-bin` (a prebuilt wheel of dlib) for the shape predictor, whose
  ~100 MB model comes from `face_recognition_models`. That package imports
  `pkg_resources`, hence `setuptools>=80,<82`: setuptools 82 removed it.
- `resvg-py` rasterises the glasses. Statically linked, so no apt layer.
  `cairosvg` would need libcairo; don't swap it in.

## Testing

`uv run pytest` — hermetic:

- `stub_dns` (autouse) replaces `socket.getaddrinfo`; `responses` intercepts
  HTTP; `fakeredis` stands in for the broker.
- `sync_queue` runs jobs inline. `async_queue` + `drain` (a `SimpleWorker`
  burst) covers queued→finished. The forking `Worker` silently does nothing
  against fakeredis — never use it in a test.
- `stub_task` repoints `jobs.TASK` at a fake in `tests/support.py`. Job
  functions must be importable, so they cannot be defined in a test body.
- Real detection runs against the sample images. `SAMPLE_FACES` in
  `tests/test_processor.py` pins each count; counts are resolution-dependent,
  so they are for the files as committed.

The browser suite (`tests/e2e`) drives real Chromium against uvicorn, an RQ
worker thread, fakeredis and the real detector:

```
uv sync --group e2e && uv run playwright install chromium
uv run pytest tests/e2e
uv run pytest -m "not e2e"
```

The worker thread runs the real `ThreadWorker` in bursts. Every e2e test
also asserts the console stayed quiet.

Load and abuse testing lives in `tests/load` and runs against a real stack;
see `docs/LOAD.md`.

## Known limits

- The API is unauthenticated. The rate limit and queue cap in
  `src/throttle.py` bound abuse per client; they are not access control.
- Redis holds payloads and results as base64 with no persistence. Give it a
  `maxmemory` in production; `docker-compose.yml` sets one.
- The detector runs on CPU only. It finds dogs sometimes; the score does
  not separate them from people, so there is no threshold that would.
- No structured logging, metrics or tracing. CI runs lint + tests only.

## Conventions

- 4-space indent, single quotes, double quotes in route decorators. Ruff
  (`line-length 100`, `E,F,W,I,UP,B,SIM,C4`) is the arbiter: `uv run ruff check`.
- Type hints on function signatures. Comments explain *why*, briefly, where
  the obvious alternative is a trap.
- Processors subclass `BaseProcessor`, take a numpy array, expose `call()` and
  `base64_output`.
- Failures a user should read subclass `DealWithItError`; anything else is a
  bug and gets a generic message plus a logged traceback.
- Tests are grouped in `Test*` classes by behaviour, named as sentences.
