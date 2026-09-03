# Fully-qualified image names so Podman does not have to guess a registry.
FROM docker.io/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # The venv lives outside /app so that bind-mounting the source over /app
    # during development cannot hide it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# No apt layer: dlib-bin ships a manylinux wheel that needs nothing beyond
# the libstdc++ already in the slim image, and Pillow bundles its own codecs.
COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /bin/uv

WORKDIR /app

# Only the manifests, so editing src/ does not reinstall the world.
COPY pyproject.toml uv.lock ./


# What compose runs: every extra, plus the test tooling. The source arrives as
# a bind mount rather than a COPY, so edits are live.
FROM base AS dev

RUN uv sync --locked --all-extras


# The web tier enqueues jobs by name, so it needs neither dlib nor the ~100 MB
# of face_recognition model data.
FROM base AS web

RUN uv sync --locked --no-dev
COPY src ./src

EXPOSE 5000
CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "5000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]


FROM base AS worker

RUN uv sync --locked --extra worker --no-dev
COPY src ./src

CMD ["python", "-m", "src.worker"]
