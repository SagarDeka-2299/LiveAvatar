FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=600 \
    UV_CONCURRENT_DOWNLOADS=4 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY scripts ./scripts
COPY livekit_agent ./livekit_agent
COPY .env.example ./.env.example

# Default location for the local-tenant SQLite + blob fallback. Mount a
# volume here in docker-compose to persist test data across container
# restarts. In production tenants this directory is never touched.
RUN mkdir -p /app/local_data

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
