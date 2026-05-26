from __future__ import annotations

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


# ── Tenant scoping ──
#
# Every API consumer identifies the tenant they are operating against by
# passing ``tenant_id`` as a **required query parameter** on every route.
# It never appears in request bodies. The FastAPI dependency
# ``app.tenancy.tenant_ctx_query`` validates the value and opens the
# tenant-scoped session.


# ── Domain entities (response side) ──

class Preset(BaseModel):
    """One entry from the in-code preset library.

    Three-level taxonomy: ``category → partition → subcategory``.
    A user can pick chips from multiple categories and from multiple
    partitions within a category, but at most one chip per partition
    (chips inside a partition are mutually exclusive).
    ``gender`` is the persona-gender filter — ``"unisex"`` chips are
    always shown; gendered chips only show when the persona's gender
    matches. ``subcategory`` doubles as the UI chip label; ``prompt``
    is the detailed paragraph appended to the model brief.
    """

    id: str
    category: str
    partition: str
    subcategory: str
    gender: Literal["male", "female", "unisex"]
    prompt: str


class PersonaAvatar(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    persona_id: int | None = None
    name: str
    decoration: str = ""
    theme_prompt: str = ""
    preset_ids: list[str] = Field(default_factory=list)
    face_id: str = ""
    image_url: str = ""
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    created_at: datetime | None = None


class PersonaEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_url: str = ""
    gender: str = "unknown"
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    voice_provider: str = ""
    voice_id: str = ""
    voice_source: str = ""
    voice_description: str = ""
    voice_sample_url: str = ""
    voice_preview_url: str = ""
    voice_status: str = ""
    voice_last_error: str | None = None
    avatars: list[PersonaAvatar] = Field(default_factory=list)
    created_at: datetime | None = None


class VoiceEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str = "elevenlabs"
    voice_id: str = ""
    source: str = ""
    description: str = ""
    sample_url: str = ""
    preview_url: str = ""
    persona_id: int | None = None
    gender: str = "unknown"
    status: str = "ready"
    last_error: str | None = None
    created_at: datetime | None = None


class Assistant(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prompt: str
    first_message: str
    persona_id: int | None = None
    avatar_id: int | None = None
    face_id: str = ""
    simli_agent_id: str = ""
    voice_provider: str = ""
    voice_id: str | None = None
    voice_model: str = ""
    language: str = "en"
    llm_provider: str = ""
    llm_model: str = ""
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    created_at: datetime | None = None


class StudioState(BaseModel):
    presets: list[Preset] = Field(default_factory=list)
    personas: list[PersonaEntity] = Field(default_factory=list)
    assistants: list[Assistant] = Field(default_factory=list)
    voices: list[VoiceEntity] = Field(default_factory=list)


# ── Slim list entities (for side panels on page load) ──

class PersonaListEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_url: str = ""
    created_at: datetime | None = None
    status: str
    gender: str = "unknown"
    avatar_count: int = 0


class AvatarListEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    image_url: str = ""
    created_at: datetime | None = None
    status: str


class VoiceListEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    created_at: datetime | None = None
    status: str
    source: str = ""
    gender: str = ""
    description: str = ""
    persona_id: int | None = None


class AssistantListEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    avatar_image_url: str = ""
    created_at: datetime | None = None
    status: str
    persona_id: int | None = None
    avatar_id: int | None = None
    persona_name: str = ""
    avatar_name: str = ""


# ── Card / Detailed entities ──

class PersonaAvatarDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    persona_id: int | None = None
    name: str
    decoration: str = ""
    theme_prompt: str = ""
    preset_ids: list[str] = Field(default_factory=list)
    face_id: str = ""
    image_url: str = ""
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    created_at: datetime | None = None
    persona: PersonaListEntity | None = None


class AssistantDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    prompt: str
    first_message: str
    persona_id: int | None = None
    avatar_id: int | None = None
    avatar: AvatarListEntity | None = None
    face_id: str = ""
    simli_agent_id: str = ""
    voice_provider: str = ""
    voice_id: str | None = None
    voice_model: str = ""
    language: str = "en"
    llm_provider: str = ""
    llm_model: str = ""
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    created_at: datetime | None = None


# ── Per-resource status (polled while a background task runs) ──

class StatusResponse(BaseModel):
    """Returned by ``GET /{resource}/{id}/status?tenant_id=…``."""

    status: str
    stage: str | None = None
    progress: int | None = None
    last_error: str | None = None


# ── Request bodies ──

class PersonaVoiceDesignRequest(BaseModel):
    user_prompt: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=100)
    gender: str | None = Field(
        default=None,
        description=(
            "Optional gender directive ('male' or 'female'). When supplied, "
            "appended to the voice-design prompt sent to ElevenLabs. When "
            "omitted, the persona's detected gender is used."
        ),
    )


class AvatarPreviewResponse(BaseModel):
    preview_url: str
    preview_key: str
    applied_prompt: str
    preset_ids: list[str] = Field(default_factory=list)


class AssistantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    first_message: str = Field(default="Hi, I am your avatar assistant. How can I help?")
    persona_id: int
    avatar_id: int
    llm_provider: str | None = None
    llm_model: str | None = None


class AssistantCallCreate(BaseModel):
    assistant_id: int


# ── Patch update payload requests ──

class PersonaPatchRequest(BaseModel):
    name: str | None = None
    voice_ref_id: int | None = None


class AssistantPatch(BaseModel):
    name: str | None = None
    prompt: str | None = None
    first_message: str | None = None
    avatar_id: int | None = None
    llm_provider: str | None = None
    llm_model: str | None = None


# ── /calls plug-and-play response ──

class CallLivekit(BaseModel):
    url: str
    token: str
    room: str
    identity: str


class CallSimliAuto(BaseModel):
    """Direct Simli Auto path — frontend joins Daily, no LiveKit involved."""

    room_url: str
    session_id: str


class CallAssistant(BaseModel):
    id: int
    name: str
    first_message: str


class CallAvatar(BaseModel):
    id: int
    face_id: str
    image_url: str = ""


class CallVoice(BaseModel):
    provider: str = ""
    voice_id: str | None = None
    name: str = ""
    preview_url: str = ""


class AssistantCallResponse(BaseModel):
    # Exactly one of these is populated, decided by SIMLI_TRANSPORT.
    transport: Literal["livekit", "auto"] = "livekit"
    livekit: CallLivekit | None = None
    simli_auto: CallSimliAuto | None = None
    assistant: CallAssistant
    avatar: CallAvatar
    voice: CallVoice
