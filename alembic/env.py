"""Alembic environment for the multi-tenant Lili API.

The migration target DB is supplied at runtime in one of two ways:

  * ``alembic -x dburl=postgresql+psycopg://... upgrade head`` from the CLI
  * Programmatic invocation from ``app.tenancy.provision_tenant`` which sets
    ``cfg.set_main_option("sqlalchemy.url", ...)`` before calling
    ``command.upgrade``.

We force a sync driver (``psycopg`` or ``psycopg2``) for Alembic because the
``alembic`` runner is synchronous. If the supplied URL uses an async driver
(``+asyncpg``) we rewrite it to ``+psycopg`` for the duration of the
migration. Application code keeps using ``+asyncpg``.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("dburl") or config.get_main_option("sqlalchemy.url") or os.getenv(
        "ALEMBIC_DATABASE_URL", ""
    )
    if not url:
        raise RuntimeError(
            "No database URL provided. Pass -x dburl=... or set sqlalchemy.url."
        )
    # Alembic runs sync; normalise to +psycopg (v3).
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)

    # psycopg (v3) does not accept ?schema=...; convert to options=-c search_path=...
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    schema = qs.pop("schema", None)
    if schema:
        existing_options = qs.pop("options", [""])[0]
        new_options = f"-c search_path={schema[0]}"
        if existing_options:
            new_options = f"{existing_options} {new_options}"
        qs["options"] = [new_options]
    clean_qs = urlencode({k: v[0] for k, v in qs.items()})
    url = urlunparse(parsed._replace(query=clean_qs))
    return url


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg_section = config.get_section(config.config_ini_section) or {}
    cfg_section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
