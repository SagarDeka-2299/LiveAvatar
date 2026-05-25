"""add gender to voices

Revision ID: 0002_voice_gender
Revises: 0001_initial_schema
Create Date: 2026-05-25

Adds a ``gender`` column to the ``voices`` table so the listing endpoint
can be filtered by it. Populated at voice creation:
  * voice/design or voice/clone with ``persona_id`` → copies persona.gender
  * voice/from-library → reads ElevenLabs ``labels.gender``
  * otherwise defaults to ``"unknown"``
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_voice_gender"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voices",
        sa.Column(
            "gender",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
    )


def downgrade() -> None:
    op.drop_column("voices", "gender")
