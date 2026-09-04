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
        ◄─────────────── {state, images}  ◄────────────────┘   write derivatives
                                    │                          │
        GET /i/{job}/view.webp ─────┴──► Caddy, off the shared blob directory
```

The web tier never *decodes* an image: it validates the request shape, writes
the submitted bytes to the blob store, puts a path on the queue and reads job
state back. The task is enqueued **by name** (`src/jobs.py:TASK`) so the web
process never imports OpenCV.

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
src/blobs.py        The blob store. Web-safe: no imaging library, ever
src/derivatives.py  Pixels to files. Worker-only; all the Pillow lives here
src/assets.py       Content-hashed /static URLs and their cache headers
src/processors/     BaseProcessor + DealWithItProcessor
src/static/         style.css, the sample photos, glasses.svg, the app
                    icons and the web manifest
tests/              Hermetic: no network, no Redis server, no committed
                    fixtures; blobs go to a per-test tmp_path
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
  uploaded. It is one of the three lines of script in the interface — htmx has
  no attribute that splits a file input. A file post therefore answers with
  the card alone: the script empties the input itself, and an out-of-band
  form swap would tear out the element the rest of the batch posts from.
  `/submit` still takes a list, because the submit button can post one.
- **Nothing multi-megabyte goes through Redis, and no card carries a
  picture.** A submission is written to the blob store and the queue carries a
  path; the worker writes sized derivatives there and the result is a map of
  URLs. A finished card fetches ~320 KB where it once fetched 15.8 MB as URLs,
  or 42 MB inlined. `JobResult.image` is gone — that is API 2.0.
- **`src/blobs.py` is web-safe and `src/derivatives.py` is not.** blobs imports
  no imaging library, which is what lets the web tier mint paths and store
  opaque bytes without gaining the ability to decode an attacker's image; a
  subprocess test pins it. All the Pillow lives in derivatives, which only the
  worker imports. The web tier never *decodes* an image — writing bytes to a
  file is strictly less than the base64 encode it replaced.
- **A job owns one directory**, which is what makes it independently
  deletable and is the whole basis of the sweep. Writes are temp-file plus
  `os.replace` inside it, so a reader sees a whole file or nothing; the temp
  name is dot-prefixed and both servers refuse to serve one. A retry
  hard-links rather than sharing a directory.
- **`view.webp` is one size for the card *and* the lightbox, on purpose.**
  `_result_view` reuses the same `<img>` for both and the lightbox is a CSS
  `:has(:checked)` state, so no `srcset`/`sizes` pair can describe it —
  `sizes` cannot see a sibling checkbox. Download is the escape hatch for
  full resolution. Do not "fix" this with srcset.
- **A sample is displayed from `tiles/` and submitted from the original.**
  `SAMPLE_FACES` pins a face count per sample and those counts move with the
  width, so pointing submission at a tile would quietly make every caption on
  the page wrong. A test asserts the submitted bytes equal the committed file.
- **The share page reads `meta.json` off disk, not Redis.** Redis forgets a
  job at `result_ttl`; the pictures live to `blob_ttl`. A Redis-backed page
  would go dark while its own images were still up.
- **A started job past its deadline is reported failed** (`_abandoned` in
  `src/jobs.py`). A worker killed mid-job — a deploy, a crash in native code,
  the OOM killer, all of which this deployment restarts from — leaves its job
  at `started` with nobody coming back to it, and the card polls its last
  checkpoint ("Encoding the result") forever. RQ does reap those, but only in
  a maintenance pass it runs every ten minutes *and* only while some worker is
  alive to run one, so the reader decides instead, at `job_timeout +
  ABANDONED_GRACE`. `ThreadWorker` also reaps every `REAP_INTERVAL`
  (`src/worker.py`), which is what gives an abandoned job's payload a TTL
  rather than leaving it in a `noeviction` Redis for good.
- **Progress is stage markers** written to `job.meta` by the worker. The face
  count is lifted out of the "Drawing glasses on N faces" step because the
  next checkpoint overwrites `step`.
- **A queued job's wait is `LPOS` on the queue list** — the index *is* the
  number of jobs ahead, and `_peek` reads it in the same pipeline as the
  status and meta, so a poll still costs one round trip. It comes back as
  `ahead` on `JobResult` and is None once a worker has the job, which is
  exactly when `progress`/`step` start saying something.
- **A `/static` URL carries its file's content hash.** `asset()` in
  `src/assets.py` stamps `?v=<hash>`, and anything with a `v` is served
  `immutable` for a year; a bare URL gets five minutes and its ETag. The
  hash is cached per (path, mtime, size) because hashing everything a page
  touches is 8ms and a stat is 0.07ms. The reverse proxy serves these files
  itself in production and repeats the same two rules, so a change here is
  a change in two places. `og:image` stays unversioned: it is a URL other
  people's scrapers keep.
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
- **The phone layout has no server side.** Below 600px the page has two
  states and the CSS reads which one from the list itself: with nothing in
  `#queue` the input pane is the page, and `body:has(#queue article)` moves
  it into a fixed bar (`nav.addbar` in `src/ui.py`). The URL and Samples
  panels over that bar are three radios (`#show-url`, `#show-samples`,
  `#show-none`); the swipe-to-remove gesture is a scroll-snap strip of
  `.card-body` and `.card-remove`; the copy that differs between a desktop
  and a phone ships twice, as `.desktop-only` / `.mobile-only`. No endpoint,
  no cookie and no script knows about any of it.
- **`#form` is never `display: none`.** The camera and library file inputs
  live in it, and a label cannot open a picker for an input with no box, so
  results mode hides the form's children instead.
- **The dropzone card is a `<div>`, not the label.** On a phone the card also
  holds a "Take a photo" label, and a label cannot nest in a label, so the
  label is `.dropzone-face` inside it and `.dropzone-face::before` stretches
  its hit area back over the whole card -- which is what keeps a click or a
  drop on the padding working. `.pick-row` is `position: relative` so its two
  buttons sit above that overlay, and `order` puts the limits line after them.
- **The docs contents list is a `<details>`.** Above 860px it is forced open
  with `::details-content { content-visibility: visible }` and the summary is
  made inert, so the sidebar is the label and links it always was.
- **`head_tags` takes the theme.** With no cookie it emits both `theme-color`
  values behind `prefers-color-scheme`; with one it emits a single
  unconditional tag, so the browser chrome follows the chosen theme. The
  colours are the band's, because a translucent status bar sits on the band.
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
