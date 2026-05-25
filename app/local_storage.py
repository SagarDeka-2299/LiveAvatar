"""Local-disk blob store used by the ``local_tenant`` fallback.

Exposes the same async surface as :class:`app.blob.BlobStore` so callers
don't care whether they're talking to Azure or to the local filesystem.
Files live under ``LOCAL_DATA_DIR/{tenant_id}/blob/{key}``; the URL we
stamp into DB rows points at the ``/local-blob/{tenant_id}/{key}`` route
defined in :mod:`app.main`.

The class deliberately does *not* import :class:`app.blob.BlobStore` —
it just mirrors its method names + return shape so they're
interchangeable from the caller's point of view.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import settings


@dataclass(frozen=True)
class BlobUploadResult:
    key: str
    url: str


class LocalBlobStore:
    """Filesystem-backed equivalent of :class:`app.blob.BlobStore`.

    Same method signatures, same return shapes — only the storage backend
    is different.
    """

    def __init__(self, *, tenant_id: str, base_dir: str | None = None) -> None:
        self._tenant_id = tenant_id
        root = Path(base_dir or settings.local_data_dir).resolve()
        self._root = root / tenant_id / "blob"

    @property
    def container_name(self) -> str:
        return f"local::{self._tenant_id}"

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def close(self) -> None:
        return None

    async def ensure_container(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Disallow traversal: the resolved key must remain inside the
        # tenant's blob root.
        target = (self._root / key).resolve()
        if self._root not in target.parents and target != self._root:
            raise ValueError(f"refusing key outside blob root: {key!r}")
        return target

    def _url_for(self, key: str) -> str:
        base = settings.local_blob_base_url.rstrip("/")
        return f"{base}/{self._tenant_id}/{key.lstrip('/')}"

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> BlobUploadResult:
        path = self._path_for(key)
        await asyncio.to_thread(_write_blob, path, data, content_type)
        return BlobUploadResult(key=key, url=self._url_for(key))

    async def delete(self, key: str) -> None:
        path = self._path_for(key)
        await asyncio.to_thread(_delete_blob, path)

    def signed_url(self, key: str) -> str:
        # Local URLs are not signed — return the plain serving URL.
        return self._url_for(key)

    def read_local(self, key: str) -> tuple[bytes, str]:
        """Synchronous helper for the ``/local-blob`` serving route.

        Returns ``(bytes, content_type)``. Raises ``FileNotFoundError``
        when the key does not exist.
        """
        path = self._path_for(key)
        if not path.exists():
            raise FileNotFoundError(str(path))
        sidecar = path.with_suffix(path.suffix + ".ct")
        content_type = "application/octet-stream"
        if sidecar.exists():
            try:
                content_type = sidecar.read_text().strip() or content_type
            except OSError:
                pass
        return path.read_bytes(), content_type


def _write_blob(path: Path, data: bytes, content_type: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if content_type:
        sidecar = path.with_suffix(path.suffix + ".ct")
        sidecar.write_text(content_type)


def _delete_blob(path: Path) -> None:
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    sidecar = path.with_suffix(path.suffix + ".ct")
    if sidecar.exists():
        try:
            sidecar.unlink()
        except OSError:
            pass


def reset_local_tenant(tenant_id: str, base_dir: str | None = None) -> None:
    """Wipe everything for a local tenant. Test-only helper."""
    root = Path(base_dir or settings.local_data_dir).resolve() / tenant_id
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


__all__ = ["LocalBlobStore", "BlobUploadResult", "reset_local_tenant"]
