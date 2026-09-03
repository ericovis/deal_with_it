# CLAUDE.md — deal_with_it

Context for working on this codebase. Written 2026-09-03 while reviving the project; the code
itself was last touched 2022-04-16.

## What it is

A small web app + JSON API that pastes "Deal With It" sunglasses onto every face in a picture.
`POST /api` takes either an image `url` or a data-URI `base64` string and returns the processed
image back as a data-URI. `GET /` serves a one-page HTML demo/docs site (Jinja2 template + vanilla
JS + a static CSS file). FastAPI's `/docs` (Swagger) is enabled; `/redoc` is disabled.

Face detection is `face_recognition` (which wraps `dlib`); compositing is Pillow. The rotation
angle of the glasses comes from the vector between the outer left-eye landmark and an inner
right-eye landmark.

## Layout

```
src/app.py                     FastAPI app: 2 routes, static mount, template config
src/models.py                  ImageModel (input) + ApiResponseModel (output) — pydantic v1
src/processors/base.py          BaseProcessor: holds output PIL image, renders base64 data-URI
src/processors/deal_with_it.py  DealWithItProcessor: detection, angle, glasses resize/paste
src/templates/index.html        the whole demo/docs page
src/static/{css,js,img}         style.css, main.js, glasses.png + showcase images
tests/                          pytest; conftest gives a TestClient, a base64 fixture, a URL
Dockerfile                      2 stages (dev, heroku) on a prebuilt dlib base image
Dockerfile-base                 builds ericovis/python3-10-dlib (python:3.10 + dlib + poetry)
docker-compose.yml              single `web` service, uvicorn --reload on :5000
heroku.yml                      Heroku container deploy pointing at the `heroku` stage
.github/workflows/              tests.yml (pytest + CodeClimate coverage), base-docker-image.yml
```

Request flow: `POST /api` → `ImageModel(**body)` — validation *and* the actual image download /
decode happen here, inside the model's `__init__` → `DealWithItProcessor(image, STATIC_DIR)` →
`.call()` mutates `self.output` → `.base64_output` encodes it → response.

## Running it

Everything assumed Docker Compose (the dlib build is why):

```
docker-compose up                      # http://localhost:5000
docker-compose run web pytest
```

Local machine as of this writing has **podman 5.4.2, no docker, no poetry, Python 3.13**, so none
of the above runs as-is — expect to sort out the container story before you can execute anything.
The base image `ericovis/python3-10-dlib:latest` may or may not still exist on Docker Hub;
verify before assuming a build works.

## History worth knowing

- **2018** — born as an AWS Lambda project: Flask app behind `serverless` + a forked
  `serverless-wsgi`, with a separate static `web/` front-end.
- **2020-06** (`5e62248`) — rewritten as a single Heroku container: Flask + Pipenv + Docker,
  `web/index.html` became a Jinja2 template, images consolidated under `static/`.
- **2022-04** (`4545883`) — gunicorn added, with `workers = cpu_count() * 2 + 1`.
- **2022-04-16** (PR #9, `b8646a5`) — the big one: Flask → FastAPI, Pipenv → Poetry, flat modules →
  `src/` package, `processors.py` split into `base.py` + `deal_with_it.py`, gunicorn dropped for
  bare uvicorn, `.travis.yml` deleted in favour of GitHub Actions.
- **2022-04-16** (PRs #10, #11) — the tests and validators in their current shape. Last commits.
- The seven `origin/archive/pr-*` branches are all abandoned dependabot Pillow/Jinja2 bumps against
  the **pre-FastAPI Pipfile**. Nothing there to salvage; they can be deleted.

Design intent that no longer holds: it was deliberately built to run in **one Heroku container with
no worker/queue**, doing CPU-bound face detection inline in the request. Multi-container is now on
the table, so that constraint is gone — this is the main thing the revival is about.

## Known pitfalls (roughly by severity)

These were found by reading the code, not by running it — the ones marked *unverified* need a
reproduction before being treated as fact.

**Blocking / concurrency**
1. `src/app.py:28` — the endpoint is `async def` but calls fully synchronous, CPU-heavy code
   (`fr.face_locations`, Pillow). Every request blocks the event loop, so one uvicorn worker
   serialises all traffic and can't even answer a health check while processing. Either move the
   work off-process (queue + worker) or, as a stopgap, make the handler `def` so Starlette runs it
   in a threadpool.
2. `src/models.py:53,67` — `requests.head` / `requests.get` run *inside pydantic validators*, i.e.
   synchronous network I/O in the request path, with **no timeout**. A slow host hangs a worker
   indefinitely.
3. The URL is fetched twice (HEAD to validate, GET to download). Racy and wasteful.

**Correctness**
4. *unverified* — The demo form is almost certainly broken against the current API. `main.js`
   `getData()` always builds `{url: null, base64: null}` and fills one in, so both keys are always
   present in the JSON; `a_single_parameter_must_be_passed` (`src/models.py:36`) tests key
   *presence*, not value, and rejects it with "not both". The validator should check for non-`None`
   values instead.
5. Error display in the browser is also broken: `main.js` reads `data.message`, but FastAPI
   validation failures return `{"detail": [...]}`. The `message` shape is a leftover from the Flask
   API.
6. No faces found → `self.output` stays `None` → `base64_output` returns `None` → fails
   `ApiResponseModel.image: str` → 500. The Flask version returned a clean 400 "No faces were
   found on this image". This is a regression from PR #9.
7. Malformed base64 raises `binascii.Error` from `ImageModel.__init__` → 500 instead of 422.
   Doing the decode in `__init__` (outside the validation machinery) is the root cause; it belongs
   in a validator or, better, out of the model entirely.
8. `functools.cache` on the method `BaseProcessor._base64_output` (`src/processors/base.py:24`)
   caches on `self`, so the global cache holds a strong reference to every processor — and its
   decoded numpy image — for the life of the process. Unbounded memory growth per request.

**Security**
9. Unauthenticated server-side fetch of any user-supplied URL = SSRF. No allowlist, no
   private-IP/link-local blocking, no redirect limit, no `Content-Length` cap, no timeout.
10. No cap on input image size or dimensions anywhere; a large image is a cheap way to exhaust
    CPU and memory (no `Image.MAX_IMAGE_PIXELS` handling / decompression-bomb guard).
11. No rate limiting, no CORS config, and `--forwarded-allow-ips '*'` in the Dockerfile trusts
    `X-Forwarded-*` from anyone.

**Dependencies / toolchain**
12. `pyproject.toml` claims `python = "^3.9"` but `src/models.py:22` uses `X | None`, which needs
    3.10. The declared floor is wrong.
13. Everything is pinned to 2022: pydantic **1.9.0** (v1 `validator`/`root_validator` API — a v2
    migration is a rewrite of `models.py`), fastapi 0.75.1, starlette 0.17.1, uvicorn 0.17.6,
    Pillow 9.1.0, numpy 1.22.3, requests 2.27.1, pytest 7.1.1.
14. `dlib` hard-pinned to `19.19.0` and built from source in the base image — the reason for the
    whole custom-base-image dance. `face_recognition` 1.3.0 is unmaintained.
    **User's decision: keep both for now, replace later.** Candidate replacements when that time
    comes: mediapipe, InsightFace, or ONNX-exported detectors/landmarkers.
15. `image_to_numpy` 1.0.0 is a tiny abandoned package whose only job is EXIF-orientation-correct
    loading. Trivially replaceable with `PIL.ImageOps.exif_transpose` + `np.asarray`.
16. `Dockerfile` uses `poetry install --no-dev --remove-untracked`; both flags are gone in modern
    Poetry (`--without dev`, `--sync`). `[tool.poetry.dev-dependencies]` is likewise deprecated.
17. Base image is referenced as `:latest` — unpinned and mutable.
18. No `.dockerignore`, so `.git` and everything else lands in the build context.

**Tests / CI**
19. Tests hit the live internet: `tests/conftest.py:22` and several `test_image_model.py` cases
    depend on `https://emagalha.es/...` responding a particular way. Not hermetic, and the 404/
    non-image cases assert on someone's live server behaviour.
20. `tests/test_api.py` asserts on exact pydantic v1 error strings — these will all change under
    pydantic v2.
21. `.github/workflows/tests.yml` calls `docker-compose` (v1 CLI, no longer on GitHub runners) and
    posts to CodeClimate with a hardcoded reporter ID; CodeClimate's free OSS product is
    discontinued, so those README badges are dead weight.
22. Coverage config lives nowhere — `coverage run -m pytest` with no `[tool.coverage]` section.

**Stale content**
23. `index.html` still says the REST API "is written using Flask" (`:79`) and links
    `deal-with-it.herokuapp.com` in five places (og/twitter meta + API docs); Heroku's free tier is
    gone, so that host is dead. README points at the same URL.
24. `main.js:3` uses `hljs.initHighlightingOnLoad()`, removed in highlight.js 11; the page pins
    10.0.3 from cdnjs.
25. `docker-compose.yml` declares the obsolete `version: '3'` key; its `DEBUG: 1` env var is read
    by nothing.

**Minor / quality**
26. `deal_with_it.py:45` calls `fr.face_landmarks` once per face inside the loop, re-processing the
    whole image each time. One call with all `face_locations` does the same work once.
27. Magic numbers with no explanation: `offset = 415/1024`, `increase=0.3`. Worth a comment.
28. No `/health` endpoint, no logging configuration, no settings/config module.
29. `BaseProcessor.call()` is a silent `pass` rather than raising `NotImplementedError`.

## Direction for the revival

Agreed so far: keep `dlib` + `face_recognition` for now, and move off the single-container design
to a multi-container one (API + worker + broker), which is what unblocks pitfalls 1–3. Nothing
else is decided yet — ask before assuming a target architecture, queue technology, or deployment
platform.

## Conventions

- 4-space indent, single quotes for strings, double quotes in decorators/route paths (mixed in
  practice; follow the file you're in).
- Type hints on function signatures in `src/processors/`, absent in `src/app.py`.
- Processors subclass `BaseProcessor`, take an `ImageModel`, and expose `call()` + `base64_output`.
- Tests are plain functions with descriptive `test_<sentence>` names and pytest fixtures from
  `conftest.py`. No mocking library in use.
