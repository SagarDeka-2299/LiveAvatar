"""Async repository functions over the SQLAlchemy models.

These thin wrappers replace the sync helpers that lived in the old
``app/db.py``. Every function takes an explicit ``AsyncSession`` so callers
remain in charge of transaction scoping (typically the per-request session
provided by ``app.tenancy.get_tenant_context``).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Assistant, PersonaAvatar, PersonaEntity, Voice


# ── PersonaEntity ──

async def insert_persona_entity(session: AsyncSession, **fields: Any) -> PersonaEntity:
    row = PersonaEntity(**fields)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_persona_entities(
    session: AsyncSession,
    *,
    gender: str | None = None,
    status: str | None = None,
    voice_status: str | None = None,
    voice_provider: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[PersonaEntity]:
    stmt = select(PersonaEntity).order_by(PersonaEntity.id.desc())
    if gender is not None:
        stmt = stmt.where(PersonaEntity.gender == gender)
    if status is not None:
        stmt = stmt.where(PersonaEntity.status == status)
    if voice_status is not None:
        stmt = stmt.where(PersonaEntity.voice_status == voice_status)
    if voice_provider is not None:
        stmt = stmt.where(PersonaEntity.voice_provider == voice_provider)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_persona_entity(
    session: AsyncSession, persona_id: int
) -> PersonaEntity | None:
    return await session.get(PersonaEntity, persona_id)


async def update_persona_entity(
    session: AsyncSession, persona_id: int, **fields: Any
) -> PersonaEntity | None:
    row = await session.get(PersonaEntity, persona_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_persona_entity(session: AsyncSession, persona_id: int) -> int:
    result = await session.execute(
        delete(PersonaEntity).where(PersonaEntity.id == persona_id)
    )
    return result.rowcount or 0


# ── PersonaAvatar ──

async def insert_persona_avatar(session: AsyncSession, **fields: Any) -> PersonaAvatar:
    row = PersonaAvatar(**fields)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_persona_avatars(
    session: AsyncSession,
    *,
    persona_id: int | None = None,
    gender: str | None = None,
    voice_id: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[PersonaAvatar]:
    stmt = select(PersonaAvatar).order_by(PersonaAvatar.id.desc())
    if persona_id is not None:
        stmt = stmt.where(PersonaAvatar.persona_id == persona_id)
    if status is not None:
        stmt = stmt.where(PersonaAvatar.status == status)
    if gender is not None or voice_id is not None:
        stmt = stmt.join(PersonaEntity, PersonaEntity.id == PersonaAvatar.persona_id)
        if gender is not None:
            stmt = stmt.where(PersonaEntity.gender == gender)
        if voice_id is not None:
            stmt = stmt.where(PersonaEntity.voice_id == voice_id)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_avatars_for_persona(
    session: AsyncSession, persona_id: int
) -> list[PersonaAvatar]:
    result = await session.execute(
        select(PersonaAvatar)
        .where(PersonaAvatar.persona_id == persona_id)
        .order_by(PersonaAvatar.id.desc())
    )
    return list(result.scalars().all())


async def get_persona_avatar(
    session: AsyncSession, avatar_id: int
) -> PersonaAvatar | None:
    return await session.get(PersonaAvatar, avatar_id)


async def update_persona_avatar(
    session: AsyncSession, avatar_id: int, **fields: Any
) -> PersonaAvatar | None:
    row = await session.get(PersonaAvatar, avatar_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_persona_avatar(session: AsyncSession, avatar_id: int) -> int:
    result = await session.execute(
        delete(PersonaAvatar).where(PersonaAvatar.id == avatar_id)
    )
    return result.rowcount or 0


# ── Assistant ──

async def insert_assistant(session: AsyncSession, **fields: Any) -> Assistant:
    row = Assistant(**fields)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_assistants(
    session: AsyncSession,
    *,
    persona_id: int | None = None,
    avatar_id: int | None = None,
    voice_id: str | None = None,
    llm_provider: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Assistant]:
    stmt = select(Assistant).order_by(Assistant.id.desc())
    if persona_id is not None:
        stmt = stmt.where(Assistant.persona_id == persona_id)
    if avatar_id is not None:
        stmt = stmt.where(Assistant.avatar_id == avatar_id)
    if voice_id is not None:
        stmt = stmt.where(Assistant.voice_id == voice_id)
    if llm_provider is not None:
        stmt = stmt.where(Assistant.llm_provider == llm_provider)
    if status is not None:
        stmt = stmt.where(Assistant.status == status)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_assistant(session: AsyncSession, assistant_id: int) -> Assistant | None:
    return await session.get(Assistant, assistant_id)


async def update_assistant(
    session: AsyncSession, assistant_id: int, **fields: Any
) -> Assistant | None:
    row = await session.get(Assistant, assistant_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_assistant(session: AsyncSession, assistant_id: int) -> int:
    result = await session.execute(
        delete(Assistant).where(Assistant.id == assistant_id)
    )
    return result.rowcount or 0


# ── Voice ──

async def insert_voice(session: AsyncSession, **fields: Any) -> Voice:
    row = Voice(**fields)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_voices(
    session: AsyncSession,
    *,
    gender: str | None = None,
    source: str | None = None,
    provider: str | None = None,
    status: str | None = None,
    persona_id: int | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> list[Voice]:
    stmt = select(Voice).order_by(Voice.id.desc())
    if gender is not None:
        stmt = stmt.where(Voice.gender == gender)
    if source is not None:
        stmt = stmt.where(Voice.source == source)
    if provider is not None:
        stmt = stmt.where(Voice.provider == provider)
    if status is not None:
        stmt = stmt.where(Voice.status == status)
    if persona_id is not None:
        stmt = stmt.where(Voice.persona_id == persona_id)
    if offset is not None:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_voice(session: AsyncSession, voice_id: int) -> Voice | None:
    return await session.get(Voice, voice_id)


async def update_voice(
    session: AsyncSession, row_id: int, **fields: Any
) -> Voice | None:
    """Update one ``voices`` row by its primary-key ``row_id``.

    The parameter is named ``row_id`` (not ``voice_id``) on purpose —
    the ``voices`` table has its own ``voice_id`` column (the upstream
    ElevenLabs / Simli voice identifier), so a caller updating that
    column passes ``voice_id=<el_id>`` as a kwarg and we mustn't shadow
    it on the positional argument."""
    row = await session.get(Voice, row_id)
    if row is None:
        return None
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def delete_voice(session: AsyncSession, voice_id: int) -> int:
    result = await session.execute(delete(Voice).where(Voice.id == voice_id))
    return result.rowcount or 0


# ── Link-clear helpers (run by delete handlers — see app/main.py) ──
#
# The DB-level FK on assistants.avatar_id is ``ON DELETE SET NULL``, but the
# ``face_id`` column is a free string (not an FK), so we have to clear it
# explicitly when its avatar is deleted.

async def clear_face_id_for_avatar(session: AsyncSession, avatar_id: int) -> None:
    await session.execute(
        update(Assistant).where(Assistant.avatar_id == avatar_id).values(face_id="")
    )


# Clears every voice-related field on any persona that referenced the given
# standalone voice via ``voice_ref_id``.
async def clear_persona_voice_ref(session: AsyncSession, voice_ref_id: int) -> None:
    await session.execute(
        update(PersonaEntity)
        .where(PersonaEntity.voice_ref_id == voice_ref_id)
        .values(
            voice_ref_id=None,
            voice_provider="",
            voice_id="",
            voice_source="",
            voice_description="",
            voice_sample_url="",
            voice_preview_url="",
            voice_status="",
            voice_last_error=None,
        )
    )


# ── Cascade counters (used by delete-warning modals on the client) ──

async def count_avatars_for_persona(session: AsyncSession, persona_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(PersonaAvatar)
        .where(PersonaAvatar.persona_id == persona_id)
    )
    return int(result.scalar() or 0)


async def count_assistants_for_persona(session: AsyncSession, persona_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Assistant)
        .where(Assistant.persona_id == persona_id)
    )
    return int(result.scalar() or 0)


async def count_assistants_for_avatar(session: AsyncSession, avatar_id: int) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Assistant)
        .where(Assistant.avatar_id == avatar_id)
    )
    return int(result.scalar() or 0)


# ── Bulk resets (cleanup endpoint) ──

async def delete_all_assistants(session: AsyncSession) -> int:
    result = await session.execute(delete(Assistant))
    return result.rowcount or 0


async def delete_all_persona_avatars(session: AsyncSession) -> int:
    result = await session.execute(delete(PersonaAvatar))
    return result.rowcount or 0


async def delete_all_persona_entities(session: AsyncSession) -> int:
    result = await session.execute(delete(PersonaEntity))
    return result.rowcount or 0


