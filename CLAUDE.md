# CLAUDE.md — deal_with_it

Context for working on this codebase. Originally written 2026-09-03 from an
archaeological read of the 2022-era code; rewritten the same day once the
revival landed. If something here contradicts the code, the code wins — say so.

## What it is

A web app + JSON API that pastes "Deal With It" sunglasses onto every face in
a picture. Submitting an image enqueues a background job; the page polls until
the worker is done.

Face detection is `face_recognition` (a wrapper over `dlib`), compositing is
Pillow. The glasses angle comes from the vector between the outer left-eye
landmark and an inner right-eye landmark.

## Architecture

Three processes, because face detection is CPU-bound and takes seconds:

```
        POST /api/jobs                      RQ / Redis
browser ───────────────► web (FastAPI+FastHTML) ──────► worker (rq)
        ◄─────────────── 202 {job_id}                     │
        GET  /api/jobs/{id}  (htmx polls every 1s)         │ fetch, detect, paste
        ◄─────────────── {state, image}  ◄─────────────────┘
```

The web tier never touches an image. It validates the request shape, puts a
payload on the queue and reads job state back — nothing in a handler can block
longer than a Redis round trip. **The task is enqueued by name**
(`src/jobs.py:TASK = 'src.tasks.process_image'`) rather than imported, so the
web process and its container never load dlib. Keep that boundary.

```
src/app.py          Composition root. FastAPI outer app, FastHTML mounted at /
src/api.py          The JSON API router, mounted under /api
src/ui.py           The FastHTML interface: page, form handler, poll fragment
src/jobs.py         The queue seam: enqueue / result_for / is_broker_reachable
src/worker.py       `python -m src.worker` — the RQ worker entry point
src/tasks.py        process_image(): what the worker actually runs
src/images.py       Fetching (SSRF-guarded) and decoding into a numpy array
src/models.py       Pydantic v2 request/response schemas. Validates shape only
src/config.py       Settings, all DWI_-prefixed env vars
src/errors.py       DealWithItError: failures whose message is safe to show
src/processors/     BaseProcessor + DealWithItProcessor
src/static/         style.css and the showcase/glasses images
tests/              Hermetic: no network, no Redis server, no fixtures on disk
```

### Why FastAPI *and* FastHTML

Both are Starlette apps, so this is one ASGI stack, not two frameworks
fighting. FastAPI gives the API pydantic validation and OpenAPI docs at
`/api/docs`; FastHTML gives the UI htmx-driven interactivity with no
hand-written JavaScript.

**FastAPI must stay the outer app.** FastHTML installs a static-file catch-all
as its *first* route, matching any path ending in one of ~50 asset extensions.
With FastHTML on the outside, `/api/report.csv` never reaches FastAPI.
`tests/test_api.py::TestDocs::test_api_paths_are_not_swallowed_by_the_static_handler`
locks that in. That catch-all is also a bare `FileResponse` over the process
CWD, so `create_ui()` strips it and `src/app.py` mounts `StaticFiles` instead.

### htmx swaps in the UI

Three things happen when a job finishes, all as out-of-band swaps in one
response (`src/ui.py:finished`): the polling fragment empties, the image is
inserted at the top of `#gallery`, and a blank `#form` replaces the filled-in
one. Only on success -- a failure leaves the form alone so it can be
corrected.

**The gallery item is wrapped in an envelope div, and that nesting is
load-bearing.** htmx inserts the element carrying `hx-swap-oob` only when the
swap style is `outerHTML`; for anything else, including `afterbegin`, it
inserts that element's *children* (`isInlineSwap` in htmx 2.0.7 returns true
for `outerHTML` alone). Put the class on the outer div and the `.gallery-item`
wrapper vanishes silently, taking its styling and the
`:has(.gallery-item)` rule that unhides the section.

The gallery is DOM-only: nothing is written to disk or kept server-side, so a
reload loses it. That is deliberate, and the page says so.

Progress comes from the worker writing checkpoints to `job.meta`
(`src/tasks.py:report_progress`), which `jobs.result_for` reads back. They are
stage markers, not measurements. A queued job renders `<progress>` with no
value, which browsers animate as indeterminate.

### Why RQ

Considered and rejected: Celery (far more machinery than a one-queue app
needs), arq (asyncio-first, and this work is CPU-bound sync code), Dramatiq
(fine, but no advantage here). RQ is Redis-only, ~30 lines of integration, and
its worker **forks per job** — which matters when the job calls into a native
library that could segfault: only the work horse dies. `src/worker.py` uses the
forking `Worker` deliberately; `SimpleWorker` appears only in tests.

## Running it

```
docker compose up                      # http://localhost:5000, docs at /api/docs
docker compose run --rm web pytest
docker compose up --scale worker=3     # more detection throughput
```

`docker` is aliased to `podman` in the user's zsh, and `docker compose`
delegates to `podman-compose` 1.6.0. Verified working under it: service-name
DNS, port mapping, and `depends_on: condition: service_healthy`. Image names
are fully qualified (`docker.io/library/...`) because Podman will not guess a
registry.

`docker compose restart <service>` **does not work** under podman-compose when
the service has a healthcheck dependency -- it stops the dependency and then
refuses to start the dependents ("container state improper"). Use
`docker compose up -d` again, which recreates what it needs.

The `web` service reloads on edit; the `worker` does not, because RQ has no
reloader. Restart it after touching anything under `src/tasks.py` or
`src/processors/`.

Locally, without containers:

```
uv sync --all-extras                   # dlib-bin is a wheel; no compiler needed
uv run pytest
docker compose up -d redis && uv run rq worker deal_with_it &
uv run uvicorn src.app:app --reload --port 5000
```

## The dependency situation

`dlib` and `face_recognition` are staying for now (user's call), but they are
the fragile part of the tree and there are three load-bearing details:

1. **`setuptools>=80,<82` is not cosmetic.** `face_recognition_models` calls
   `pkg_resources` at import time; setuptools 82 removed it. Without the upper
   bound, `import face_recognition` fails with a *misleading* "please install
   face_recognition_models" — which is already installed. Bisected: 81 works,
   82 does not.
2. **`dlib-bin==20.0.1` instead of `dlib`.** Same code, prebuilt manylinux
   wheel: installs in ~1s instead of a ~5min compile, and was verified to
   produce bit-identical landmarks. It writes the same files as `dlib`, and
   `face-recognition` hard-depends on `dlib`, so `[tool.uv]
   override-dependencies` neutralises that or both get installed and one
   clobbers the other. To go back to source builds: swap to `dlib==20.0.1`,
   drop the override, and note modern dlib pulls cmake in as a PEP 517 build
   dep (no system cmake needed).
3. **No apt packages are required.** Verified with `ldd`: the dlib extension
   needs only libstdc++/libm/libgcc/libc, all present in `python:3.12-slim`.
   It is built without BLAS, LAPACK, CUDA or image codecs; Pillow does the
   image I/O. Don't add an apt layer "just in case".

Replacement candidates when the time comes: mediapipe, InsightFace, or an
ONNX-exported detector. `image_to_numpy` was already dropped — it only did
`ImageOps.exif_transpose`, verified identical across all 8 EXIF orientations.

## Testing

`uv run pytest` — 163 tests, ~30s (the real detector accounts for nearly all
of it). Everything is hermetic:

- `stub_dns` (autouse) replaces `socket.getaddrinfo`, so no test resolves a
  real name and the SSRF guard has predictable addresses to reject.
- `responses` intercepts HTTP; `fakeredis` stands in for the broker.
- `sync_queue` runs jobs inline (`is_async=False`). RQ records a failing job
  as failed rather than re-raising, exactly as a real worker does, so failure
  paths are genuinely exercised.
- `async_queue` + `drain` (a `SimpleWorker` burst) covers the
  queued→finished transition. The forking `Worker` **silently does nothing**
  against fakeredis — never use it in a test.
- `stub_task` repoints `jobs.TASK` at a fake in `tests/support.py`. Job
  functions must be importable, so they cannot be defined in a test body.
- Real detection runs against the repo's own images: `me.jpg` (1 face),
  `multiple_people.jpg` (5 faces), `glasses.png` (none).

## What the revival changed

Every numbered pitfall from the original audit, for the record:

- **Event-loop blocking** → the work moved to an RQ worker; handlers only
  talk to Redis.
- **Network I/O in pydantic validators** → gone. `src/models.py` validates
  shape only, and `tests/test_models.py` asserts it never touches the network.
- **The demo form was broken for years** — the old validator checked key
  *presence*, so the page's `{"url": null, "base64": "..."}` was rejected as
  "both". Now value-based, with a regression test.
- **Error display was broken** — the JS read `data.message`, a leftover from
  the Flask API that returned `detail`. No JS left to get it wrong.
- **No-faces → 500** → a `failed` state with the message
  "No faces were found in this image."
- **`functools.cache` on a method** leaked every processor and its decoded
  image. Now memoised per instance, with a weakref test to prove it.
- **SSRF** → `src/images.py` resolves the host, refuses non-global addresses,
  re-validates every redirect hop, caps the body while streaming, and enforces
  a timeout. Off-by-default escape hatch: `DWI_ALLOW_PRIVATE_ADDRESSES`.
- **No size limits** → `DWI_MAX_IMAGE_BYTES` (10 MB) and `DWI_MAX_IMAGE_PIXELS`
  (40 MP, checked from the header before decoding).
- **Per-face `face_landmarks` calls** re-ran detection on the whole image for
  every person: 11.4s vs 0.02s on the group photo. One batched call now.
- Detection also runs on a copy downscaled to `DWI_MAX_DETECTION_SIZE` (1600px
  longest side): 12.5s → 2.6s on the group photo, same 5 faces, glasses
  centroid drifting 0.8% of image width. Compositing stays full-resolution.
- **Stale everything** → Heroku, Poetry, Pipenv, gunicorn, the Flask mention in
  the page copy, `Dockerfile-base`, the Docker Hub push workflow and the dead
  CodeClimate badges are all gone.

## Known rough edges

- The API is unauthenticated and unthrottled. The SSRF guard stops it being a
  network proxy, but nothing stops someone burning worker CPU.
- Redis runs with no `maxmemory` and no persistence. Results are base64 data
  URIs stored at ~1.33x their size; a flood of 10 MB submissions is a memory
  problem. Serving results from an object store (or the filesystem) and
  returning a URL would fix both that and the giant JSON payloads.
- The SSRF guard validates the DNS answer it sees, then makes a second
  resolution when connecting — a rebinding attacker has a window. Closing it
  means pinning the socket to the validated IP, which breaks TLS hostname
  verification.
- `face_recognition_models` puts ~100 MB of model data in the worker image,
  which is why the Dockerfile has separate `web` (473 MB) and `worker`
  (1.13 GB) targets. `docker compose` uses the `dev` target for both.
- The HOG detector is CPU-only and mediocre on small or side-on faces.
- `style.css` is hand-written from 2020 and has had no responsive testing
  since the rewrite.
- No structured logging, no metrics, no tracing.
- CI runs lint + tests only. Building or pushing images is deliberately not
  wired up yet.

## Conventions

- 4-space indent, single quotes, double quotes in route decorators. Ruff
  (`line-length 100`, `E,F,W,I,UP,B,SIM,C4`) is the arbiter: `uv run ruff check`.
- Type hints on function signatures. Comments explain *why*, especially where
  the obvious-looking alternative is a trap — several here were bugs once.
- Processors subclass `BaseProcessor`, take a numpy array, expose `call()` and
  `base64_output`.
- Failures a user should read subclass `DealWithItError`; anything else is a
  bug and gets a generic message plus a logged traceback.
- Tests are grouped in `Test*` classes by behaviour, named as sentences.
