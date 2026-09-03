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
src/ui.py           The FastHTML interface: both pages, fragments, routes
src/jobs.py         The queue seam: enqueue / describe / resubmit / health
src/worker.py       `python -m src.worker` — the RQ worker entry point
src/tasks.py        process_image(): what the worker actually runs
src/images.py       Fetching (SSRF-guarded) and decoding into a numpy array
src/models.py       Pydantic v2 request/response schemas. Validates shape only
src/config.py       Settings, all DWI_-prefixed env vars
src/errors.py       DealWithItError: failures whose message is safe to show
src/processors/     BaseProcessor + DealWithItProcessor
src/static/         style.css, the showcase photos, glasses.svg
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

Two pages: the app at `/` and the docs at `/docs`, sharing a header and
footer built by `shell`-ish helpers in `src/ui.py`.

**Everything on the app page is a card that replaces itself.** A submission
returns one `article` per picture, swapped `afterbegin` into `#queue`. While
a job is queued or started the article carries `hx-get="/jobs/{id}"`,
`hx-trigger="load delay:1s"` and `hx-swap="outerHTML"`; a terminal one does
not, so the poll chain ends by itself rather than leaving a timer running
against whatever replaced it. Queued/started render a `<progress>`, finished
renders the image, failed renders the message and a Retry button. There is no
`hx-swap-oob` gallery envelope any more -- that whole mechanism, and the
`:has(.gallery-item)` rule, went with the redesign.

Only two things are still swapped out of band: the blank `#form` that comes
back after a submission (so the URL, the file input and the hint reset at
once), and the `#url-hint` when a URL is rejected before it becomes a job.

**A polling card must not carry a data URI.** The thumbnail and the "before"
image come from `JobSource.thumb`, which is a remote URL for a URL job and a
`/static` path for a sample. An upload has neither, so its card shows a blank
thumbnail until it finishes -- putting the data URI there would re-send the
entire upload once a second, for as long as the job runs.

Progress comes from the worker writing checkpoints to `job.meta`
(`src/tasks.py:report_progress`), which `jobs.describe` reads back. They are
stage markers, not measurements. A queued job renders `<progress>` with no
value, which browsers animate as indeterminate. `report_progress` also lifts
the face count out of the "Drawing glasses on N faces" step, because the next
checkpoint overwrites `step` and the finished card still wants the number.

The session list is DOM-only: nothing is written to disk or kept server-side,
so a reload loses it. That is deliberate, and the page says so.

### The theme switch, and why it is a span

Light/dark is a checkbox that posts to `/theme`. The response is one hidden
`<span id="theme-state" data-theme="light|dark">`, swapped in place, plus the
cookie that makes the next load agree. `style.css` reads *only* that span:

```css
body { /* light */ }
@media (prefers-color-scheme: dark) {
    body:not(:has(#theme-state[data-theme="light"])) { /* dark */ }
}
body:has(#theme-state[data-theme="dark"]) { /* dark */ }
```

An absent `data-theme` means nobody has chosen, which is the only case where
the OS gets a say. Driving it from `:has(#theme:checked)` instead looks
simpler and is wrong: an unchecked box cannot be told apart from "no
preference", so a first-ever toggle *off* on a dark desktop has nothing to
override the media query with. The switch's own knob and label are drawn from
`--knob-left` and `--theme-label`, tokens defined in the theme blocks, so they
always show the theme actually in force. The one seam left: on a dark desktop
with no cookie the page is dark while the checkbox is unchecked, so the first
click is a no-op. Fixing that needs `Sec-CH-Prefers-Color-Scheme`.

### The glasses are vector

`glasses.svg` is the only copy of the artwork and the only file to edit. It
was traced from the original 3000x487 PNG with potrace; that PNG is gone.

The page loads the SVG directly. The worker rasterises it **per face, in
memory, at the width that face needs** (`_render_glasses` in
`src/processors/deal_with_it.py`) -- roughly 3ms at 520px, against ~4s of
detection, so it does not register. It renders at `SUPERSAMPLE` times the
final width because the rotate that follows resamples, and widths are rounded
up to `RENDER_STEP` so a photo full of similarly sized faces asks for one
render rather than one per face.

The geometry is unchanged from the raster version: `_get_glasses` still sizes
the *rotated bounding box* to the face width and `_get_final_position` still
anchors on `lens_offset`. Only where the pixels come from changed.

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
   needs only libstdc++/libm/libgcc/libc, and `resvg_py.abi3.so` only
   libgcc/libpthread/libm/libc -- all present in `python:3.12-slim`. dlib is
   built without BLAS, LAPACK, CUDA or image codecs; Pillow does the image
   I/O. Don't add an apt layer "just in case".
4. **`resvg-py` is what rasterises the glasses**, and it was chosen for that
   `ldd` output. It is a static Rust build. The obvious alternative,
   `cairosvg`, needs `libcairo.so.2`, which `python:3.12-slim` does not have
   (checked) -- it would cost the apt layer point 3 is about. `skia-python`
   wants libEGL. If resvg ever has to go, the fallbacks are an apt layer for
   cairo or going back to a committed PNG.

Replacement candidates when the time comes: mediapipe, InsightFace, or an
ONNX-exported detector. `image_to_numpy` was already dropped — it only did
`ImageOps.exif_transpose`, verified identical across all 8 EXIF orientations.

## Testing

`uv run pytest` — 256 tests, ~16s (the real detector accounts for nearly all
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
  `apollo_11_crew.jpg` (3), `multiple_people.jpg` (8), `solvay_1927.jpg` (29),
  `blue_marble.jpg` (none). The sample tiles state those counts in words, so
  `test_every_sample_has_the_faces_its_caption_claims` pins every one --
  change a picture and the page would otherwise start lying about it.

### The browser suite

`tests/e2e` drives a real Chromium. It exists because this interface leans on
CSS doing what scripts usually do -- `:has()` switches Before/After, opens the
full-screen view and flips the palette -- and a test client sees the markup
that implies all of it and none of the behaviour. Both htmx bugs fixed during
the redesign were found by writing these, not by the 256 tests above.

```
uv sync --group e2e && uv run playwright install chromium
uv run pytest tests/e2e          # 24 tests, ~40s
uv run pytest -m "not e2e"       # everything else
```

Without the group installed `tests/e2e` is not collected at all, so plain
`uv run pytest` still passes. CI runs it as its own job.

The stack is real but self-contained: uvicorn in a thread, an RQ worker in
another, fakeredis, and the genuine detector. Two things the worker thread
needs, both in `tests/e2e/conftest.py`: **RQ enforces `job_timeout` with
SIGALRM, and only the main thread may install a signal handler**, so the
handlers *and* `death_penalty_class` are stubbed out. Miss the second and
every job dies with "signal only works in main thread" before running a line,
which looks exactly like a broken app.

Every test also asserts the console stayed quiet. A page that ships no
JavaScript of its own should log nothing, so anything there is htmx failing
or markup we got wrong.

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
  every person, which cost seconds per extra face. One batched call now.
- Detection also runs on a copy downscaled to `DWI_MAX_DETECTION_SIZE` (1600px
  longest side). On `multiple_people.jpg` upscaled to a phone-sized 3840x3072,
  that is 17.2s against 4.3s, with the same 8 faces found. Compositing stays
  full-resolution, and the glasses centroid moves under 1% of image width.
- **The interface was one long page** -- form, showcase, API docs and licence
  stacked together, with a single result slot. It is now the app at `/` and
  the docs at `/docs`, and each submission gets its own card.
- **Stale everything** → the packaging and deployment leftovers of three
  earlier hosting arrangements, the wrong framework named in the page copy,
  `Dockerfile-base`, the Docker Hub push workflow and the dead CodeClimate
  badges are all gone. uv and the compose stack are the whole story now.

## Known rough edges

- The API is unauthenticated and unthrottled. The SSRF guard stops it being a
  network proxy, but nothing stops someone burning worker CPU -- and the
  dropzone takes several files at once, which makes that easier than it was.
- `POST /validate` judges a URL from the string alone. It cannot resolve a
  name (a handler must not wait on DNS), so `images.shape_error` catches only
  the obvious cases; `_assert_public_address` in the worker is the real guard.
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
- `style.css` was rewritten for the redesign and its responsive rules have
  been reasoned about but not tested in a real browser at width.
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
