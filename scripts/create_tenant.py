"""Provision a new customer tenant.

Writes the five per-tenant secrets to Azure Key Vault under the
``{tenant_id}-*`` naming convention, runs Alembic migrations against the
supplied Postgres database, and ensures the named blob container exists.

Idempotent: re-running with the same arguments simply overwrites the
Key Vault values and is a no-op for migrations and the container.

Usage:

    python -m scripts.create_tenant <tenant_id> \\
        --db-url postgresql+asyncpg://user:pass@host:5432/lili_<tenant_id> \\
        --blob-account-url https://<acct>.blob.core.windows.net/ \\
        --blob-account-name <acct> \\
        --blob-account-key <key> \\
        --blob-container lili-media

The operator is responsible for provisioning the Postgres database and the
storage account out-of-band (Terraform, az CLI, etc.) — this script only
seeds Key Vault and ensures the schema and container are in place.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.keyvault import (
    InvalidTenantIdError,
    TenantSecrets,
    TenantSecretsNotFoundError,
    validate_tenant_id,
)
from app.tenancy import provision_tenant


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts.create_tenant",
        description="Provision a new Lili Studio tenant.",
    )
    parser.add_argument(
        "tenant_id",
        help="Tenant identifier. Allowed characters: [a-zA-Z0-9-], max 63 chars.",
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help=(
            "Async SQLAlchemy URL for the tenant Postgres DB, e.g. "
            "postgresql+asyncpg://user:pass@host:5432/dbname"
        ),
    )
    parser.add_argument(
        "--blob-account-url",
        required=True,
        help="Azure Blob service endpoint, e.g. https://<account>.blob.core.windows.net/",
    )
    parser.add_argument("--blob-account-name", required=True)
    parser.add_argument("--blob-account-key", required=True)
    parser.add_argument(
        "--blob-container",
        required=True,
        help="Container that will hold this tenant's media (will be created if missing).",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    try:
        validate_tenant_id(args.tenant_id)
    except InvalidTenantIdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    secrets = TenantSecrets(
        tenant_id=args.tenant_id,
        db_url=args.db_url,
        blob_account_url=args.blob_account_url,
        blob_account_name=args.blob_account_name,
        blob_account_key=args.blob_account_key,
        blob_container=args.blob_container,
    )
    try:
        await provision_tenant(secrets)
    except TenantSecretsNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Tenant {args.tenant_id!r} provisioned: secrets written to Key Vault, "
        f"migrations applied, container {args.blob_container!r} ready."
    )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
