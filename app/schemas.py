from __future__ import annotations

from pydantic import BaseModel, Field


class ThemePreset(BaseModel):
    id: str
    label: str
    category: str
    gender: str = "all"
    description: str
    prompt: str


class PersonaAvatar(BaseModel):
    id: int
    persona_id: int
    name: str
    decoration: str
    theme_prompt: str
    preset_ids: list[str] = Field(default_factory=list)
    face_id: str
    image_path: str
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None


class PersonaEntity(BaseModel):
    id: int
    name: str
    image_path: str
    gender: str = "unknown"
    status: str
    progress: int = 0
    stage: str = "queued"
    last_error: str | None = None
    voice_provider: str = ""
    voice_id: str = ""
    voice_source: str = ""
    voice_description: str = ""
    voice_sample_path: str = ""
    voice_preview_path: str = ""
    voice_status: str = ""
    voice_last_error: str | None = None
    avatars: list[PersonaAvatar] = Field(default_factory=list)


class VoiceEntity(BaseModel):
    id: int
    name: str
    provider: str = "elevenlabs"
    voice_id: str = ""
    source: str = ""
    description: str = ""
    sample_path: str = ""
    preview_path: str = ""
    persona_id: int | None = None
    status: str = "ready"
    last_error: str | None = None
    created_at: str | None = None


class PersonaVoiceDesignRequest(BaseModel):
    user_prompt: str = Field(default="", max_length=500)
    name: str = Field(default="", max_length=100)
    client_id: str | None = None


class Assistant(BaseModel):
    id: int
    name: str
    prompt: str
    first_message: str
    persona_id: int
    avatar_id: int
    face_id: str
    simli_agent_id: str = ""
    voice_provider: str
    voice_id: str | None
    voice_model: str
    language: str
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


class AvatarPreviewRequest(BaseModel):
    persona_id: int
    name: str = Field(min_length=1, max_length=100)
    theme_prompt: str = Field(default="", max_length=1000)
    preset_ids: list[str] = Field(default_factory=list)


class AvatarPreviewResponse(BaseModel):
    preview_path: str
    applied_prompt: str
    preset_ids: list[str] = Field(default_factory=list)


class AvatarCreateRequest(BaseModel):
    persona_id: int
    name: str = Field(min_length=1, max_length=100)
    theme_prompt: str = Field(default="", max_length=1000)
    preset_ids: list[str] = Field(default_factory=list)
    preview_path: str = Field(min_length=1)
    client_id: str | None = None


class AssistantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1)
    first_message: str = Field(default="Hi, I am your avatar assistant. How can I help?")
    persona_id: int
    avatar_id: int
    llm_provider: str | None = None
    llm_model: str | None = None
    client_id: str | None = None


class AssistantCallCreate(BaseModel):
    assistant_id: int


class AssistantCallResponse(BaseModel):
    assistant_id: int
    assistant_name: str
    room_name: str
    livekit_url: str
    participant_token: str
    participant_identity: str


class LivekitTokenRequest(BaseModel):
    room_name: str = Field(min_length=1, max_length=128)
    identity: str = Field(min_length=1, max_length=128)
    participant_name: str | None = None


class SimliSessionTokenRequest(BaseModel):
    create_transcript: bool = True
    expiry_stamp: int = -1
