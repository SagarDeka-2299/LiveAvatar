from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    # ── Core infra ──
    livekit_url: str = os.getenv("LIVEKIT_URL", "")
    livekit_api_key: str = os.getenv("LIVEKIT_API_KEY", "")
    livekit_api_secret: str = os.getenv("LIVEKIT_API_SECRET", "")
    simli_api_key: str = os.getenv("SIMLI_API_KEY", "")

    # ── AI provider keys ──
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")

    # ── Task 1: gender detection ──
    gender_provider: str = os.getenv("GENDER_PROVIDER", "gemini").lower()
    gender_model_gemini: str = os.getenv("GENDER_MODEL_GEMINI", "gemini-3-flash-preview")
    gender_model_openai: str = os.getenv("GENDER_MODEL_OPENAI", "gpt-5.4-nano")

    # ── Task 2: avatar image edit ──
    image_provider: str = os.getenv("IMAGE_PROVIDER", "gemini").lower()
    image_model_gemini: str = os.getenv("IMAGE_MODEL_GEMINI", "gemini-3.1-flash-image-preview")
    image_model_openai: str = os.getenv("IMAGE_MODEL_OPENAI", "gpt-image-1.5")

    # ── Task 3: in-call LLM ──
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai").lower()
    llm_model_openai: str = os.getenv("LLM_MODEL_OPENAI", "gpt-5.4-mini")
    llm_model_gemini: str = os.getenv("LLM_MODEL_GEMINI", "gemini-3-flash-preview")

    # ── Task 4: STT ──
    stt_provider: str = os.getenv("STT_PROVIDER", "deepgram").lower()
    stt_model: str = os.getenv("STT_MODEL", "nova-3-general")

    # ── Task 5: TTS ──
    tts_provider: str = os.getenv("TTS_PROVIDER", "elevenlabs").lower()
    tts_model: str = os.getenv("TTS_MODEL", "eleven_flash_v2_5")
    tts_voice_id: str = os.getenv("TTS_VOICE_ID", "")

    # ── Simli avatar defaults ──
    default_simli_face_id: str = os.getenv("DEFAULT_SIMLI_FACE_ID", "")
    default_simli_voice_provider: str = os.getenv("DEFAULT_SIMLI_VOICE_PROVIDER", "elevenlabs")
    default_simli_voice_model: str = os.getenv("DEFAULT_SIMLI_VOICE_MODEL", "eleven_flash_v2_5")
    default_simli_voice_id: str = os.getenv("DEFAULT_SIMLI_VOICE_ID", "")

    # ── Azure Key Vault (service principal that reads per-tenant secrets) ──
    azure_keyvault_url: str = os.getenv("AZURE_KEYVAULT_URL", "")
    azure_client_id: str = os.getenv("AZURE_CLIENT_ID", "")
    # AZURE_TENANT_ID is the Azure AD tenant of the service principal.
    # Do NOT confuse with our application's customer tenant_id concept.
    azure_ad_tenant_id: str = os.getenv("AZURE_TENANT_ID", "")
    azure_client_secret: str = os.getenv("AZURE_CLIENT_SECRET", "")

    # TTL (seconds) for the in-process cache of per-tenant secrets.
    tenant_secret_ttl_seconds: int = int(os.getenv("TENANT_SECRET_TTL_SECONDS", "600"))

    # ── Local fallback (used when tenant_id == LOCAL_TENANT_ID) ──
    # When the API is asked to operate on the sentinel tenant id, it
    # short-circuits Key Vault and uses a local SQLite DB + local-disk
    # blob storage rooted at LOCAL_DATA_DIR. Same code paths as the
    # production tenancy gateway — only the implementations differ.
    local_tenant_id: str = os.getenv("LOCAL_TENANT_ID", "local_tenant")
    local_data_dir: str = os.getenv("LOCAL_DATA_DIR", "./local_data")
    # Base URL the local blob store uses when stamping URLs into DB rows.
    # Demo UI and any browser-side consumer reach the file through
    # ``{LOCAL_BLOB_BASE_URL}/{tenant_id}/{key}`` — see the
    # ``GET /local-blob/{tenant_id}/{key}`` route in app/main.py.
    local_blob_base_url: str = os.getenv(
        "LOCAL_BLOB_BASE_URL", "http://localhost:8000/local-blob"
    )


settings = Settings()
