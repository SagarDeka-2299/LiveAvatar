"""Per-tenant Azure Blob Storage wrapper.

A ``BlobStore`` is bound to a single tenant's storage account + container
(resolved from Key Vault in ``app.tenancy``). It uploads bytes to a stable
key. ``signed_url()`` returns a time-limited SAS URL (default 1 hour) so
the container can stay private — no public-access ACL required.

Key conventions inside the container:

    personas/{persona_id}/source{ext}
    avatars/{avatar_id}/preview{ext}
    voices/{voice_id}/sample{ext}
    voices/{voice_id}/preview{ext}
"""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timedelta, timezone

from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient, ContainerClient

from .config import settings  # noqa: F401  (kept for future config reads)


@dataclass(frozen=True)
class BlobUploadResult:
    key: str
    url: str  # permanent, unsigned blob URL


class BlobStore:
    def __init__(
        self,
        *,
        account_url: str,
        account_name: str,
        account_key: str,
        container: str,
    ) -> None:
        self._account_url = account_url.rstrip("/") + "/"
        self._account_name = account_name
        self._account_key = account_key
        self._container_name = container

        self._service: BlobServiceClient = BlobServiceClient(
            account_url=self._account_url,
            credential=self._account_key,
        )
        self._container: ContainerClient = self._service.get_container_client(
            self._container_name
        )

    @property
    def container_name(self) -> str:
        return self._container_name

    async def close(self) -> None:
        await self._container.close()
        await self._service.close()

    async def ensure_container(self) -> None:
        """Create the container if it doesn't already exist. Idempotent."""
        try:
            await self._container.create_container()
        except Exception:
            # Already exists or no permission to create — let the caller
            # decide whether to treat this as fatal by using it afterwards.
            pass

    def _public_url(self, key: str) -> str:
        return f"{self._account_url}{self._container_name}/{key}"

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> BlobUploadResult:
        blob = self._container.get_blob_client(key)
        await blob.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type)
            if content_type
            else None,
        )
        return BlobUploadResult(key=key, url=self._public_url(key))

    async def delete(self, key: str) -> None:
        blob = self._container.get_blob_client(key)
        try:
            await blob.delete_blob()
        except Exception:
            # Already gone — treat as success.
            pass

    def signed_url(self, key: str, expiry_hours: int = 1) -> str:
        """Return a time-limited SAS URL for the given blob key."""
        sas_token = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container_name,
            blob_name=key,
            account_key=self._account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=expiry_hours),
        )
        return f"{self._public_url(key)}?{sas_token}"
