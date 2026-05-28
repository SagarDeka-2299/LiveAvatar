"""SQLAlchemy 2.x declarative models for the per-tenant Postgres schema.

Columns mirror the previous SQLite schema in ``app/db.py`` 1:1 with one
naming change: every ``*_path`` column holding a local file path becomes a
``*_url`` column holding an Azure Blob URL (typically a short-lived SAS URL).

A single ``Base`` metadata is shared across all tenant DBs — schemas are
identical; only the connection string differs per tenant.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class PersonaEntity(Base):
    __tablename__ = "persona_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    image_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    gender: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="processing")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    voice_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    voice_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    voice_source: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    voice_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice_sample_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice_preview_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    voice_status: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    voice_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # No SQLAlchemy-side cascade on delete — the database FK is
    # ``ON DELETE SET NULL`` so children survive with persona_id=NULL.
    avatars: Mapped[list["PersonaAvatar"]] = relationship(
        back_populates="persona",
        passive_deletes=True,
    )


class PersonaAvatar(Base):
    __tablename__ = "persona_avatars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("persona_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    decoration: Mapped[str] = mapped_column(Text, nullable=False, default="")
    theme_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preset_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    face_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    image_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="processing")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    persona: Mapped[PersonaEntity | None] = relationship(back_populates="avatars")
    assistants: Mapped[list["Assistant"]] = relationship(
        back_populates="avatar",
        passive_deletes=True,
    )


class Assistant(Base):
    __tablename__ = "assistants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    first_message: Mapped[str] = mapped_column(Text, nullable=False)
    persona_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("persona_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    avatar_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("persona_avatars.id", ondelete="SET NULL"),
        nullable=True,
    )
    face_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    simli_agent_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    voice_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    voice_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    voice_model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    llm_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    llm_model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="processing")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    avatar: Mapped[PersonaAvatar | None] = relationship(back_populates="assistants")


class Voice(Base):
    __tablename__ = "voices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="elevenlabs")
    voice_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Remix metadata for designed voices: the presets the user picked and the
    # raw free text they typed (never the assembled brief). ``description`` keeps
    # the full assembled brief used for generation; these two let the Remix flow
    # restore the exact original selections.
    preset_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sample_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    preview_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    persona_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="ready")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
