"""Per-tenant credential resolution via Azure Key Vault.

For each customer tenant we expect five secrets in the vault, named:

    tenant-{tenant_id}-db-url
    tenant-{tenant_id}-blob-account-url
    tenant-{tenant_id}-blob-account-name
    tenant-{tenant_id}-blob-account-key
    tenant-{tenant_id}-blob-container

`tenant_id` must match ``^[a-zA-Z0-9-]{1,63}$`` because Key Vault secret names
allow only alphanumerics + hyphens, capped at 127 chars per name (we leave
plenty of room for the suffix).

Secrets are fetched lazily on first request per tenant and cached in-process
for ``settings.tenant_secret_ttl_seconds`` (default 10 minutes). Rotation
becomes effective after the TTL expires (or on app restart).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from azure.identity.aio import ClientSecretCredential
from azure.keyvault.secrets.aio import SecretClient

from .config import settings

# Real tenant ids are constrained to Key Vault's secret-name alphabet
# (alphanumerics + hyphens). The local-fallback sentinel uses an underscore
# (``local_tenant``) which intentionally lies outside that alphabet, so we
# carve out an explicit allow-rule for it.
TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9-]{1,63}$")
LOCAL_TENANT_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,63}$")

# Every per-tenant secret is namespaced under this prefix in Key Vault so
# the same vault can be shared with non-tenant operator secrets without
# collision risk. Full secret name: ``{_SECRET_PREFIX}{tenant_id}-{key}``.
_SECRET_PREFIX = "tenant-"

_SECRET_KEYS: tuple[str, ...] = (
    "db-url",
    "blob-account-url",
    "blob-account-name",
    "blob-account-key",
    "blob-container",
)


class InvalidTenantIdError(ValueError):
    pass


class TenantSecretsNotFoundError(LookupError):
    pass


def validate_tenant_id(tenant_id: str) -> None:
    # Imported here to avoid a circular import at module load time.
    from .config import settings as _s

    if tenant_id == _s.local_tenant_id:
        # The local-fallback sentinel may use underscores; check it
        # against the relaxed pattern instead.
        if not LOCAL_TENANT_PATTERN.fullmatch(tenant_id):
            raise InvalidTenantIdError(
                f"local tenant_id must match {LOCAL_TENANT_PATTERN.pattern!r}: {tenant_id!r}"
            )
        return
    if not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise InvalidTenantIdError(
            f"tenant_id must match {TENANT_ID_PATTERN.pattern!r}: {tenant_id!r}"
        )


@dataclass(frozen=True)
class TenantSecrets:
    tenant_id: str
    db_url: str
    blob_account_url: str
    blob_account_name: str
    blob_account_key: str
    blob_container: str


class _CacheEntry:
    __slots__ = ("secrets", "expires_at")

    def __init__(self, secrets: TenantSecrets, expires_at: float) -> None:
        self.secrets = secrets
        self.expires_at = expires_at


class KeyVaultClient:
    """Thin wrapper around ``SecretClient`` with a TTL cache per tenant."""

    def __init__(self) -> None:
        self._credential: ClientSecretCredential | None = None
        self._secret_client: SecretClient | None = None
        self._cache: dict[str, _CacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _ensure_client(self) -> SecretClient:
        if self._secret_client is not None:
            return self._secret_client
        if not settings.azure_keyvault_url:
            raise RuntimeError("AZURE_KEYVAULT_URL is not configured")
        if not (
            settings.azure_client_id
            and settings.azure_ad_tenant_id
            and settings.azure_client_secret
        ):
            raise RuntimeError(
                "AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET must all be set"
            )
        self._credential = ClientSecretCredential(
            tenant_id=settings.azure_ad_tenant_id,
            client_id=settings.azure_client_id,
            client_secret=settings.azure_client_secret,
        )
        self._secret_client = SecretClient(
            vault_url=settings.azure_keyvault_url,
            credential=self._credential,
        )
        return self._secret_client

    async def close(self) -> None:
        if self._secret_client is not None:
            await self._secret_client.close()
            self._secret_client = None
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        lock = self._locks.get(tenant_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[tenant_id] = lock
        return lock

    def invalidate(self, tenant_id: str | None = None) -> None:
        if tenant_id is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant_id, None)

    async def fetch_tenant_secrets(self, tenant_id: str) -> TenantSecrets:
        validate_tenant_id(tenant_id)

        now = time.monotonic()
        entry = self._cache.get(tenant_id)
        if entry is not None and entry.expires_at > now:
            return entry.secrets

        async with self._lock_for(tenant_id):
            entry = self._cache.get(tenant_id)
            now = time.monotonic()
            if entry is not None and entry.expires_at > now:
                return entry.secrets

            client = self._ensure_client()
            try:
                values = await asyncio.gather(
                    *(
                        client.get_secret(f"{_SECRET_PREFIX}{tenant_id}-{key}")
                        for key in _SECRET_KEYS
                    )
                )
            except Exception as exc:
                raise TenantSecretsNotFoundError(
                    f"failed to load secrets for tenant {tenant_id!r}: {exc}"
                ) from exc

            secrets = TenantSecrets(
                tenant_id=tenant_id,
                db_url=values[0].value or "",
                blob_account_url=values[1].value or "",
                blob_account_name=values[2].value or "",
                blob_account_key=values[3].value or "",
                blob_container=values[4].value or "",
            )
            for field_name in (
                "db_url",
                "blob_account_url",
                "blob_account_name",
                "blob_account_key",
                "blob_container",
            ):
                if not getattr(secrets, field_name):
                    raise TenantSecretsNotFoundError(
                        f"secret {_SECRET_PREFIX}{tenant_id}-{field_name.replace('_', '-')} is empty"
                    )

            ttl = max(1, settings.tenant_secret_ttl_seconds)
            self._cache[tenant_id] = _CacheEntry(
                secrets=secrets, expires_at=now + ttl
            )
            return secrets

    async def put_tenant_secrets(self, secrets: TenantSecrets) -> None:
        """Write all five per-tenant secrets to Key Vault. Used by the provisioning CLI."""
        validate_tenant_id(secrets.tenant_id)
        client = self._ensure_client()
        values = {
            "db-url": secrets.db_url,
            "blob-account-url": secrets.blob_account_url,
            "blob-account-name": secrets.blob_account_name,
            "blob-account-key": secrets.blob_account_key,
            "blob-container": secrets.blob_container,
        }
        await asyncio.gather(
            *(
                client.set_secret(
                    f"{_SECRET_PREFIX}{secrets.tenant_id}-{key}", value
                )
                for key, value in values.items()
            )
        )
        self.invalidate(secrets.tenant_id)


key_vault = KeyVaultClient()
