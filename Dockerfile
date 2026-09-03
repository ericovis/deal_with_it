FROM docker.io/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Outside /app so the dev bind mount cannot hide it.
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=ghcr.io/astral-sh/uv:0.12.8 /uv /bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./


# What compose runs; the source is a bind mount.
FROM base AS dev

RUN uv sync --locked --all-extras


# No face stack: the web tier enqueues by name.
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
