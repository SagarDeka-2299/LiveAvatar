"""add remix fields to voices

Revision ID: 0004_voice_remix_fields
Revises: 0003_set_null_cascade
Create Date: 2026-05-28

Adds ``preset_ids`` and ``user_prompt`` columns to the ``voices`` table so the
Remix flow can restore a designed voice's exact original presets and the raw
free text the user typed. ``description`` keeps holding the full assembled
brief used for generation.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_voice_remix_fields"
down_revision: Union[str, None] = "0003_set_null_cascade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "voices",
        sa.Column(
            "preset_ids",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "voices",
        sa.Column(
            "user_prompt",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("voices", "user_prompt")
    op.drop_column("voices", "preset_ids")
