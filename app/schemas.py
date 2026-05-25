from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ── Tenant scoping ──
#
# Every API consumer must identify the tenant they are operating against by
# passing ``tenant_id`` either as a JSON body field (POST / PATCH / DELETE
# with a body), a multipart form field (file-upload routes), or a query
# parameter (GET / DELETE without a body). Every request model below
# inherits from ``TenantScoped``.

class TenantScoped(BaseModel):
    # The pattern allows underscores because the local-fallback sentinel
    # ('local_tenant') contains one. Strict validation (no underscores
    # for real tenants, since Key Vault secret names disallow them)
    # happens server-side in ``app.keyvault.validate_tenant_id``.
    tenant_id: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description=(
            "Identifier of the customer tenant. Used to look up the tenant's "
            "DB connection string and blob credentials from Azure Key Vault. "
            "Required in every request body — never accepted in the URL."
        ),
    )


# ── Domain entities (response side) ──

class ThemePreset(BaseModel):
    id: str
    label: str
    category: str
    gender: str = "all"
    description: str
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
    presets: list[ThemePreset] = Field(default_factory=list)
    personas: list[PersonaEntity] = Field(default_factory=list)
    assistants: list[Assistant] = Field(default_factory=list)
    voices: list[VoiceEntity] = Field(default_factory=list)


# ── Per-resource status (replaces the WS event stream) ──

class StatusResponse(BaseModel):
    """Polled by clients while a background task is in flight.

    Returned by:
      GET /personas/{id}/status?tenant_id=...
      GET /avatars/{id}/status?tenant_id=...
      GET /voices/{id}/status?tenant_id=...
      GET /assistants/{id}/status?tenant_id=...
    """

    status: str
    stage: str | None = None
    progress: int | None = None
    last_error: str | None = None


# ── Request bodies (all tenant-scoped) ──

class PersonaVoiceDesignRequest(TenantScoped):
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


class AvatarPreviewRequest(TenantScoped):
    persona_id: int
    name: str = Field(min_length=1, max_length=100)
    theme_prompt: str = Field(default="", max_length=1000)
    preset_ids: list[str] = Field(default_factory=list)
    skip_styling: bool = False


class AvatarPreviewResponse(BaseModel):
    preview_url: str
    preview_key: str  # blob key — the client passes this back in AvatarCreateRequest
    applied_prompt: str
    preset_ids: list[str] = Field(default_factory=list)


class AvatarCreateRequest(TenantScoped):
    persona_id: int
    name: str = Field(min_length=1, max_length=100)
    theme_prompt: str = Field(default="", max_length=1000)
    preset_ids: list[str] = Field(default_factory=list)
    preview_key: str = Field(min_length=1)


class AssistantCreate(TenantScoped):
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    first_message: str = Field(default="Hi, I am your avatar assistant. How can I help?")
    persona_id: int
    avatar_id: int
    llm_provider: str | None = None
    llm_model: str | None = None


class AssistantCallCreate(TenantScoped):
    assistant_id: int


# ── New plug-and-play call response ──
#
# A single shot. The client has everything it needs to render a synced
# face+voice call without making any further API calls: LiveKit credentials
# for the actual realtime connection, plus metadata about the assistant,
# avatar (with image URL), and voice (with preview URL).

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


