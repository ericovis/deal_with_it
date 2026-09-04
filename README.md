# Deal With It! :sunglasses:
A Python API for creating "Deal With It"-like Images

[![Tests](https://github.com/ericovis/deal_with_it/actions/workflows/tests.yml/badge.svg?branch=master)](https://github.com/ericovis/deal_with_it/actions/workflows/tests.yml)

Give it a picture, get it back with the meme sunglasses on every face in it.

## How it works

Face detection is CPU-bound and slow enough to be unfriendly to a web request,
so the app is split in three:

| Service  | What it does                                                       |
| -------- | ------------------------------------------------------------------ |
| `web`    | Serves the UI and the JSON API. Never touches an image.            |
| `worker` | Pulls jobs off the queue, fetches/decodes the image, draws glasses. |
| `redis`  | The queue, and where results wait to be collected.                 |

Submitting an image enqueues a job and returns immediately with a job id; the
page then polls until the result is ready. There are two pages: the app at
`/`, where pictures go in and results come back, and `/docs`. Both are
[FastHTML](https://fastht.ml) + htmx, the JSON API is
[FastAPI](https://fastapi.tiangolo.com), the queue is
[RQ](https://python-rq.org), and the glasses are pasted on with
[Pillow](https://python-pillow.org) using landmarks from
[YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
and [dlib](http://dlib.net)'s 68-point shape predictor. The glasses
themselves are an SVG, rasterised at the size each face needs.

## Prerequisites

Docker (or Podman) with Compose. For running things outside a container you
also need [uv](https://docs.astral.sh/uv/).

## Running local

```
docker compose up
```

The app will be available at http://localhost:5000, the API docs at
http://localhost:5000/api/docs.

To run the web tier on your machine instead, you still need Redis and a
worker:

```
docker compose up -d redis
uv sync
uv run rq worker deal_with_it &
uv run uvicorn src.app:app --reload --port 5000
```

## Running tests

```
docker compose run --rm web pytest
```

or locally, with no services running:

```
uv sync
uv run pytest
```

The suite is hermetic: no test reaches the network or needs a Redis server.

There is also a browser suite, for the CSS-only interactions and the htmx
swaps that no test client can see:

```
uv sync --group e2e
uv run playwright install chromium
uv run pytest tests/e2e
```

It runs against a real server with a real worker, both started in-process.
Without the `e2e` group installed those tests are skipped and `uv run pytest`
still passes.

## API

`POST /api/jobs` with **either** a `url` **or** a `base64` data URI:

```json
{ "url": "https://example.com/photo.jpg" }
```
```json
{ "base64": "data:image/png;base64,..." }
```

Responds `202` with a job to poll:

```json
{
  "job_id": "9f2c...",
  "state": "queued",
  "status_url": "http://localhost:5000/api/jobs/9f2c..."
}
```

There is an [`/llms.txt`](https://deal-with-it.asrv.click/llms.txt) for agents,
generated from the running configuration so its limits cannot drift, and the
OpenAPI schema is at `/api/openapi.json`.

`GET /api/jobs/{job_id}` returns the state, and links to the pictures once it
is done:

```json
{
  "job_id": "9f2c...",
  "state": "finished",
  "images": {
    "view":   "https://example.com/i/9f2c.../view.webp",
    "thumb":  "https://example.com/i/9f2c.../thumb.webp",
    "before": "https://example.com/i/9f2c.../before.webp",
    "full":   "https://example.com/i/9f2c.../result.jpg"
  },
  "downloads": {"jpg": "...", "webp": "..."},
  "share_url": "https://example.com/s/9f2c...",
  "expires_at": "2026-09-04T17:32:00Z",
  "error": null,
  "progress": 100,
  "step": "Done",
  "detection": "plain",
  "faces": [
    {
      "box": [101, 420, 402, 197],
      "score": 0.95,
      "points": {"left_eye": [249.7, 225.3], "right_eye": [357.7, 213.2], "nose": [307.2, 282.4],
                 "mouth_left": [262.0, 330.0], "mouth_right": [352.0, 322.0]},
      "landmarks": [[199.0, 208.0], "... 68 points in dlib order"]
    }
  ]
}
```

`images` are links, not bytes. `view` is the one to show — WebP, 1600px, a
fortieth of the full-resolution file — while `full` keeps the format the
picture was submitted in, and `downloads` offers whichever other formats were
written for it. Everything under `/i/` stops existing at `expires_at`, and so
does `share_url`.

> **API 2.0.** The result used to come back inline as `image`, a base64 data
> URI. It is gone: it put megabytes through Redis and out again in every poll,
> which was most of the reason results were slow to appear.

`faces` lists every face the glasses went on, in the coordinates of the
submitted image: the detector's box (top, right, bottom, left) and five
points, and the 68 landmarks the placement used. `detection` says which
pass found them: `plain`, `upscaled` or `equalised`.

While the job is still `queued`, `ahead` says how many jobs are in front of
it — `0` means it is next. It goes null the moment a worker picks the job
up, which is when `progress` and `step` start reporting stages.

The result is a JPEG when the input was a JPEG and a PNG otherwise. A
submission may be refused with `429` (too many from one client in a minute)
or `503` (the queue is full); both carry `Retry-After`. Load figures and
the abuse checks are in [docs/LOAD.md](docs/LOAD.md).

`state` is one of `queued`, `started`, `finished` or `failed`. A `failed` job
carries a human-readable `error` -- an unreachable URL, an undecodable image,
or no faces found. An unknown or expired job id gives a `404`.

While a job runs, `progress` and `step` report the stage it has reached
("Looking for faces", "Drawing glasses on 8 faces", "Encoding the result").
They are checkpoints rather than measurements -- neither OpenCV nor Pillow
reports how far through it is.

The page uses the same fields. Each submission becomes a card in the session
list; the card polls itself, shows the progress bar, and turns into the result
in place -- with a Before/After toggle, a download link, and a Retry button if
it failed. Several pictures can be dropped at once, each getting its own card.
The list lives in that tab only.

`GET /api/health` reports whether the broker is reachable.

## Configuration

Every setting is an env var prefixed with `DWI_` (see `src/config.py`):
`DWI_REDIS_URL`, `DWI_MAX_IMAGE_BYTES`, `DWI_MAX_IMAGE_PIXELS`,
`DWI_HTTP_TIMEOUT`, `DWI_JOB_TIMEOUT`, `DWI_RESULT_TTL`,
`DWI_ALLOW_PRIVATE_ADDRESSES`.

That last one is a footgun on purpose: the worker refuses to fetch URLs that
resolve to private or loopback addresses, because otherwise anyone could use
the API to read whatever is reachable from inside your network. Only turn it
on for local fixtures.

The only thing the page stores about a person is a `dwi_theme` cookie holding
`light` or `dark`. With no cookie it follows the operating system.

## Contributing

1. Fork this repo
2. Make changes
3. Open a PR

#### Some features you could build:

- Add the ability to create a animated GIF out of the original image
- Add more processors
- Add APIs for Slack integration
- Nonsense stuff ;D

## Credits

Every sample picture is public domain and credited in
[`src/static/img/CREDITS.md`](/src/static/img/CREDITS.md) — NASA photographs,
the 1927 Solvay conference, and paintings by Leonardo, Vermeer, Rembrandt,
Grant Wood, van Gogh, Munch, Landseer, Ronner-Knip and Coolidge.

The sunglasses are the ones from the
[Deal With It](https://knowyourmeme.com/memes/deal-with-it) meme.

The group photo in the showcase is the 2013 class of NASA astronauts by
Robert Markowitz, a work of the US federal government and therefore public
domain.

## License

Created by [Eric Magalhães](https://ericovis.com) under the [MIT License](/LICENSE)
