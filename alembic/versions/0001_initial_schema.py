"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-25

Creates the four base tables: persona_entities, persona_avatars, assistants,
voices. Mirrors the previous SQLite layout with ``*_path`` columns renamed
to ``*_url`` because every media reference is now a blob URL.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "persona_entities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "gender", sa.String(length=20), nullable=False, server_default="unknown"
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="processing"
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stage", sa.String(length=40), nullable=False, server_default="queued"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "voice_provider", sa.String(length=40), nullable=False, server_default=""
        ),
        sa.Column("voice_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "voice_source", sa.String(length=40), nullable=False, server_default=""
        ),
        sa.Column("voice_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("voice_sample_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("voice_preview_url", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "voice_status", sa.String(length=40), nullable=False, server_default=""
        ),
        sa.Column("voice_last_error", sa.Text(), nullable=True),
        sa.Column("voice_ref_id", sa.Integer(), nullable=True),
    )

    op.create_table(
        "persona_avatars",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "persona_id",
            sa.Integer(),
            sa.ForeignKey("persona_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("decoration", sa.Text(), nullable=False, server_default=""),
        sa.Column("theme_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("preset_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("face_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("image_url", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="processing"
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stage", sa.String(length=40), nullable=False, server_default="queued"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_persona_avatars_persona_id", "persona_avatars", ["persona_id"]
    )

    op.create_table(
        "assistants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("first_message", sa.Text(), nullable=False),
        sa.Column(
            "persona_id",
            sa.Integer(),
            sa.ForeignKey("persona_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "avatar_id",
            sa.Integer(),
            sa.ForeignKey("persona_avatars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("face_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column(
            "simli_agent_id", sa.String(length=200), nullable=False, server_default=""
        ),
        sa.Column(
            "voice_provider", sa.String(length=40), nullable=False, server_default=""
        ),
        sa.Column("voice_id", sa.String(length=200), nullable=True),
        sa.Column(
            "voice_model", sa.String(length=100), nullable=False, server_default=""
        ),
        sa.Column("language", sa.String(length=20), nullable=False, server_default="en"),
        sa.Column(
            "llm_provider", sa.String(length=40), nullable=False, server_default=""
        ),
        sa.Column(
            "llm_model", sa.String(length=100), nullable=False, server_default=""
        ),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="processing"
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "stage", sa.String(length=40), nullable=False, server_default="queued"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_assistants_persona_id", "assistants", ["persona_id"])
    op.create_index("ix_assistants_avatar_id", "assistants", ["avatar_id"])

    op.create_table(
        "voices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "provider",
            sa.String(length=40),
            nullable=False,
            server_default="elevenlabs",
        ),
        sa.Column("voice_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sample_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("preview_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("persona_id", sa.Integer(), nullable=True),
        sa.Column(
            "status", sa.String(length=40), nullable=False, server_default="ready"
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("voices")
    op.drop_index("ix_assistants_avatar_id", table_name="assistants")
    op.drop_index("ix_assistants_persona_id", table_name="assistants")
    op.drop_table("assistants")
    op.drop_index("ix_persona_avatars_persona_id", table_name="persona_avatars")
    op.drop_table("persona_avatars")
    op.drop_table("persona_entities")
