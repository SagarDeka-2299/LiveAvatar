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
    # ``persona_id`` is null when the owning persona has been deleted; the
    # avatar row survives so any assistants still referencing it stay valid.
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
    # ``persona_id`` / ``avatar_id`` go null when the linked persona / avatar
    # is deleted. The assistant itself is preserved — only the link breaks.
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


class StudioState(BaseModel):
    presets: list[Preset] = Field(default_factory=list)
    personas: list[PersonaEntity] = Field(default_factory=list)
    assistants: list[Assistant] = Field(default_factory=list)
    voices: list[VoiceEntity] = Field(default_factory=list)


# ── Per-resource status (polled while a background task runs) ──

class StatusResponse(BaseModel):
    """Returned by ``GET /{resource}/{id}/status?tenant_id=…``."""

    status: str
    stage: str | None = None
    progress: int | None = None
    last_error: str | None = None


# ── Request bodies ──
#
# Bodies carry business inputs only. ``tenant_id`` is always a query
# parameter — it never appears in a request body.

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
    preview_key: str  # blob key — the client passes this back in AvatarCreateRequest
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


# ── /calls plug-and-play response ──

class CallLivekit(BaseModel):
    url: str
    token: str
    room: str
    identity: str


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
    livekit: CallLivekit
    assistant: CallAssistant
    avatar: CallAvatar
    voice: CallVoice
