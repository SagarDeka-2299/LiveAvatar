FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Standalone defaults so a bare `docker run` is a single, self-contained
# process. Simli Auto needs no companion worker (unlike LiveKit, which adds
# a second `worker` service in docker-compose.livekit.yml). An explicit
# SIMLI_TRANSPORT in the environment / --env-file still overrides these.
ENV SIMLI_TRANSPORT=auto \
    LOCAL_DATA_DIR=/app/local_data

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
COPY livekit_agent ./livekit_agent
COPY .env.example ./.env.example

# Default location for the local-tenant SQLite + blob fallback. Declared as a
# volume so the data survives `docker rm` even on a plain `docker run`
# (mount a named volume: `-v lili_data:/app/local_data`). In production
# tenants this directory is never touched.
RUN mkdir -p /app/local_data
VOLUME ["/app/local_data"]

EXPOSE 8000

# Container-level health probe (used by `docker run`, Compose, and most PaaS).
# Uses the venv Python so we don't need to install curl.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD [".venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=3).status==200 else 1)"]

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
