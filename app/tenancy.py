"""Tenant resolution gateway.

For every ``/*`` request the API consumer supplies a ``tenant_id``
(body field, multipart form field, or query parameter depending on the
route). The ``tenant_dep`` FastAPI dependency below turns that string into a
``TenantContext`` carrying:

  * an ``AsyncSession`` against the tenant's Postgres database; and
  * a ``BlobStore`` bound to the tenant's Azure Blob container.

Both DB engines and ``BlobStore`` instances are cached per tenant to avoid
re-opening pools/connections on every request. The cache is keyed by
``tenant_id`` *and* tied to the current set of secrets — if Key Vault returns
a new ``db_url`` (after credential rotation past the TTL), the cached engine
is disposed and rebuilt transparently.

This module also exposes ``provision_tenant``, used by the CLI in
``scripts/create_tenant.py`` to seed Key Vault with a new tenant's secrets,
run Alembic against the tenant DB, and create the blob container.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Union

from fastapi import HTTPException
from fastapi import Path as PathParam
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .blob import BlobStore
from .config import settings
from .keyvault import (
    InvalidTenantIdError,
    TenantSecrets,
    TenantSecretsNotFoundError,
    key_vault,
    validate_tenant_id,
)
from .local_storage import LocalBlobStore
from .models import Base


# Any object that exposes the BlobStore async surface. Used as a duck-typed
# union so the same TenantContext shape works for both remote (Azure) and
# local (filesystem) blob backends.
BlobLike = Union[BlobStore, LocalBlobStore]


_ENGINE_CACHE_LIMIT = 64


def _reconcile_sqlite_columns(conn) -> None:  # noqa: ANN001 (sync DBAPI conn)
    """Add any ORM-model columns that are missing from existing SQLite tables.

    ``Base.metadata.create_all`` never alters existing tables, so a DB created
    before a model gained a column would be missing it. This runs ``ALTER TABLE
    … ADD COLUMN`` for each absent column, deriving a safe DEFAULT for NOT NULL
    columns. Idempotent and additive — never drops or rewrites anything.
    """
    from sqlalchemy import Integer, Numeric, inspect, text

    insp = inspect(conn)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it — already matches the model
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl_type = col.type.compile(dialect=conn.dialect)
            clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'
            if not col.nullable:
                # SQLite requires a DEFAULT when adding a NOT NULL column to a
                # populated table. Prefer the model's declared default, else a
                # type-appropriate zero value.
                default_lit: str | None = None
                sd = col.server_default
                if sd is not None and getattr(sd, "arg", None) is not None:
                    arg = sd.arg
                    default_lit = getattr(arg, "text", None) or repr(str(arg))
                elif col.default is not None and getattr(col.default, "is_scalar", False):
                    val = col.default.arg
                    default_lit = (
                        str(val) if isinstance(val, (int, float)) else repr(str(val))
                    )
                if default_lit is None:
                    default_lit = "0" if isinstance(col.type, (Integer, Numeric)) else "''"
                clause += f" NOT NULL DEFAULT {default_lit}"
            conn.exec_driver_sql(clause)


@dataclass
class TenantContext:
    tenant_id: str
    session: AsyncSession
    blob: BlobLike
    # ``secrets`` is None when the request is operating on the
    # ``local_tenant`` sentinel (Key Vault is bypassed).
    secrets: TenantSecrets | None


class _EngineCacheEntry:
    __slots__ = ("engine", "sessionmaker", "db_url")

    def __init__(
        self, engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], db_url: str
    ) -> None:
        self.engine = engine
        self.sessionmaker = sessionmaker
        self.db_url = db_url


class _BlobCacheEntry:
    __slots__ = ("blob", "key")

    def __init__(
        self,
        blob: BlobLike,
        key: tuple[str, ...],
    ) -> None:
        self.blob = blob
        self.key = key


class _TenantRegistry:
    """Holds the per-tenant engine/blob caches with simple LRU eviction."""

    def __init__(self) -> None:
        self._engines: OrderedDict[str, _EngineCacheEntry] = OrderedDict()
        self._blobs: OrderedDict[str, _BlobCacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def _evict_engine(self, tenant_id: str) -> None:
        entry = self._engines.pop(tenant_id, None)
        if entry is not None:
            await entry.engine.dispose()

    async def _evict_blob(self, tenant_id: str) -> None:
        entry = self._blobs.pop(tenant_id, None)
        if entry is not None:
            await entry.blob.close()

    async def _get_engine(
        self,
        tenant_id: str,
        db_url: str,
        *,
        is_sqlite: bool = False,
    ) -> async_sessionmaker[AsyncSession]:
        async with self._lock:
            entry = self._engines.get(tenant_id)
            if entry is not None and entry.db_url == db_url:
                self._engines.move_to_end(tenant_id)
                return entry.sessionmaker
            if entry is not None:
                await self._evict_engine(tenant_id)

            if is_sqlite:
                # SQLite + aiosqlite — used for the local_tenant fallback.
                # No connection pool tuning needed; SQLite is single-file.
                engine = create_async_engine(db_url, future=True)
                # SQLite disables foreign-key enforcement by default — we
                # rely on FK ``ON DELETE SET NULL`` for parent-deletion
                # cascade, so enable per-connection.
                @event.listens_for(engine.sync_engine, "connect")
                def _enable_sqlite_fks(dbapi_conn, _record):  # noqa: ANN001
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.close()
            else:
                engine = create_async_engine(
                    db_url,
                    pool_pre_ping=True,
                    pool_size=5,
                    max_overflow=5,
                )

            # For SQLite tenants we materialise the schema on first connect
            # via ``Base.metadata.create_all`` instead of running Alembic
            # (the migrations were authored for Postgres; ``create_all``
            # gives an equivalent schema that matches the ORM models).
            if is_sqlite:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                    # ``create_all`` only creates *missing tables*, never adds
                    # columns to tables that already exist. For long-lived local
                    # SQLite DBs we additively reconcile any model columns the
                    # table is missing (the SQLite analogue of an Alembic
                    # ``add_column`` migration), so schema changes don't require
                    # a manual rebuild.
                    await conn.run_sync(_reconcile_sqlite_columns)

            sessionmaker = async_sessionmaker(
                engine, expire_on_commit=False, class_=AsyncSession
            )
            self._engines[tenant_id] = _EngineCacheEntry(
                engine=engine, sessionmaker=sessionmaker, db_url=db_url
            )
            self._engines.move_to_end(tenant_id)
            while len(self._engines) > _ENGINE_CACHE_LIMIT:
                oldest = next(iter(self._engines))
                await self._evict_engine(oldest)
            return sessionmaker

    async def _get_blob(self, tenant_id: str, secrets: TenantSecrets) -> BlobStore:
        cache_key: tuple[str, ...] = (
            secrets.blob_account_url,
            secrets.blob_account_name,
            secrets.blob_account_key,
            secrets.blob_container,
        )
        async with self._lock:
            entry = self._blobs.get(tenant_id)
            if entry is not None and entry.key == cache_key:
                self._blobs.move_to_end(tenant_id)
                return entry.blob  # type: ignore[return-value]
            if entry is not None:
                await self._evict_blob(tenant_id)

            blob = BlobStore(
                account_url=secrets.blob_account_url,
                account_name=secrets.blob_account_name,
                account_key=secrets.blob_account_key,
                container=secrets.blob_container,
            )
            self._blobs[tenant_id] = _BlobCacheEntry(blob=blob, key=cache_key)
            self._blobs.move_to_end(tenant_id)
            while len(self._blobs) > _ENGINE_CACHE_LIMIT:
                oldest = next(iter(self._blobs))
                await self._evict_blob(oldest)
            return blob

    async def _get_local_blob(self, tenant_id: str) -> LocalBlobStore:
        """Cached :class:`LocalBlobStore` for the local-tenant fallback."""
        cache_key = ("local", tenant_id, str(Path(settings.local_data_dir).resolve()))
        async with self._lock:
            entry = self._blobs.get(tenant_id)
            if entry is not None and entry.key == cache_key:
                self._blobs.move_to_end(tenant_id)
                return entry.blob  # type: ignore[return-value]
            if entry is not None:
                await self._evict_blob(tenant_id)

            blob = LocalBlobStore(tenant_id=tenant_id)
            await blob.ensure_container()
            self._blobs[tenant_id] = _BlobCacheEntry(blob=blob, key=cache_key)
            self._blobs.move_to_end(tenant_id)
            while len(self._blobs) > _ENGINE_CACHE_LIMIT:
                oldest = next(iter(self._blobs))
                await self._evict_blob(oldest)
            return blob

    def local_blob(self, tenant_id: str) -> LocalBlobStore:
        """Synchronous accessor used by the local-blob serving route. Returns
        a transient instance (no caching) — fine because LocalBlobStore is
        cheap to construct and stateless."""
        return LocalBlobStore(tenant_id=tenant_id)

    async def shutdown(self) -> None:
        async with self._lock:
            for tenant_id in list(self._engines.keys()):
                await self._evict_engine(tenant_id)
            for tenant_id in list(self._blobs.keys()):
                await self._evict_blob(tenant_id)


_registry = _TenantRegistry()


async def shutdown_tenants() -> None:
    """Hook for ``app.on_event('shutdown')``."""
    await _registry.shutdown()
    await key_vault.close()


def is_local_tenant(tenant_id: str) -> bool:
    """``True`` when the tenant id matches the local-fallback sentinel."""
    return tenant_id == settings.local_tenant_id


def _local_sqlite_url(tenant_id: str) -> str:
    """Async SQLAlchemy URL for the local SQLite DB belonging to ``tenant_id``."""
    base = Path(settings.local_data_dir).resolve() / tenant_id
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / "db.sqlite3"
    return f"sqlite+aiosqlite:///{db_path}"


async def _enter_tenant_context(tenant_id: str) -> tuple[TenantContext, AsyncSession]:
    """Resolve tenant DB engine and blob store; open a fresh DB session.

    Two branches:

    * **Local fallback** — when ``tenant_id`` matches the ``LOCAL_TENANT_ID``
      sentinel, Key Vault is bypassed entirely. The session points at a
      per-tenant SQLite file under ``LOCAL_DATA_DIR``, and the blob store
      is a :class:`LocalBlobStore` rooted at the same directory. Same
      ``TenantContext`` shape — only the implementations differ.

    * **Remote** — original behaviour: fetch secrets from Azure Key Vault,
      open an asyncpg pool, and bind to an Azure Blob container.

    Returns the ``TenantContext`` plus the underlying ``AsyncSession`` so
    the caller can manage commit / rollback / close.
    """
    try:
        validate_tenant_id(tenant_id)
    except InvalidTenantIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if is_local_tenant(tenant_id):
        db_url = _local_sqlite_url(tenant_id)
        sessionmaker = await _registry._get_engine(
            tenant_id, db_url, is_sqlite=True
        )
        blob = await _registry._get_local_blob(tenant_id)
        session = sessionmaker()
        ctx = TenantContext(
            tenant_id=tenant_id, session=session, blob=blob, secrets=None
        )
        return ctx, session

    try:
        secrets = await key_vault.fetch_tenant_secrets(tenant_id)
    except TenantSecretsNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        # Misconfigured Key Vault credentials at the app level.
        raise HTTPException(status_code=503, detail=str(exc))

    sessionmaker = await _registry._get_engine(tenant_id, secrets.db_url)
    blob = await _registry._get_blob(tenant_id, secrets)

    session = sessionmaker()
    ctx = TenantContext(
        tenant_id=tenant_id, session=session, blob=blob, secrets=secrets
    )
    return ctx, session


async def get_tenant_context(tenant_id: str) -> AsyncIterator[TenantContext]:
    """FastAPI dependency that yields a tenant-scoped context.

    Commits on success, rolls back on exception, always closes the session.
    """
    ctx, session = await _enter_tenant_context(tenant_id)
    try:
        yield ctx
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def open_background_context(tenant_id: str) -> AsyncIterator[TenantContext]:
    """Async context manager for use inside background tasks.

    Background tasks fire outside the request lifecycle, so they cannot share
    the request's session. This helper opens a fresh tenant-scoped session
    and blob client for the task, commits on success, rolls back on
    exception, and closes the session on exit.
    """
    ctx, session = await _enter_tenant_context(tenant_id)
    try:
        yield ctx
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── FastAPI dependency helpers ──
#
# Every business route is mounted under the ``/{tenant_id}/...`` prefix.
# ``tenant_ctx`` is a FastAPI dependency that reads ``tenant_id`` from the
# path, resolves the per-tenant Postgres + Azure Blob, and yields a fully
# assembled ``TenantContext``. Background tasks that outlive the request
# open their own context via ``open_background_context``.


async def tenant_ctx(
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
) -> AsyncIterator[TenantContext]:
    """Yield a tenant-scoped context resolved from the path ``{tenant_id}``.

    Commits on success, rolls back on exception, always closes the
    session. The tenant id is validated against ``app.keyvault`` rules
    inside ``_enter_tenant_context`` — the ``Path(...)`` constraints
    here are an additional pre-filter (length only).
    """
    ctx, session = await _enter_tenant_context(tenant_id)
    try:
        yield ctx
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# ── Provisioning ──

async def provision_tenant(secrets: TenantSecrets) -> None:
    """Idempotently provision a new tenant.

    Writes the five secrets to Key Vault, then connects to ``secrets.db_url``
    and runs Alembic ``upgrade head`` against it, then ensures the blob
    container exists.
    """
    validate_tenant_id(secrets.tenant_id)
    await key_vault.put_tenant_secrets(secrets)

    # Run Alembic migrations against the tenant DB.
    from alembic import command  # local import; Alembic is heavy
    from alembic.config import Config as AlembicConfig
    from pathlib import Path

    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", secrets.db_url)
    # Alembic's command.upgrade is sync; run it on the default thread pool.
    await asyncio.to_thread(command.upgrade, cfg, "head")

    # Ensure blob container exists on the tenant's storage account.
    blob = BlobStore(
        account_url=secrets.blob_account_url,
        account_name=secrets.blob_account_name,
        account_key=secrets.blob_account_key,
        container=secrets.blob_container,
    )
    try:
        await blob.ensure_container()
    finally:
        await blob.close()
