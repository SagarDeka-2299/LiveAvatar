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

    # ── Call transport ──
    # "livekit": agent worker bridges TTS to Simli via a LiveKit room.
    # "auto": call /auto/start/configurable on Simli — Simli hosts the
    #   full STT/LLM/TTS/lipsync pipeline and the frontend joins a Daily
    #   room. No agent worker involved on this call. STT is fixed to
    #   Simli's internal provider; TTS supports ElevenLabs/Cartesia/PlayHT
    #   only (others fall back to the ElevenLabs default voice).
    # The lipsync model is left to Simli's server default so we always
    # ride their latest (currently artalk).
    simli_transport: str = os.getenv("SIMLI_TRANSPORT", "livekit").lower()

    # ── AI provider keys ──
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    deepgram_api_key: str = os.getenv("DEEPGRAM_API_KEY", "")
    elevenlabs_api_key: str = os.getenv("ELEVENLABS_API_KEY", "")

    # ── Task 1: gender detection (vision LLM) ──
    gender_provider: str = os.getenv("GENDER_PROVIDER", "gemini").lower()
    gender_model_gemini: str = os.getenv("GENDER_MODEL_GEMINI", "gemini-3-flash-preview")
    gender_model_openai: str = os.getenv("GENDER_MODEL_OPENAI", "gpt-5.4-nano")
    gender_model_azure_openai: str = os.getenv("GENDER_MODEL_AZURE_OPENAI", "gpt-4o")

    # ── Task 2: avatar image edit ──
    image_provider: str = os.getenv("IMAGE_PROVIDER", "gemini").lower()
    image_model_gemini: str = os.getenv("IMAGE_MODEL_GEMINI", "gemini-3.1-flash-image-preview")
    image_model_openai: str = os.getenv("IMAGE_MODEL_OPENAI", "gpt-image-1.5")

    # ── Task 3: in-call LLM (driven by the LiveKit agent worker) ──
    call_llm_provider: str = os.getenv("CALL_LLM_PROVIDER", "openai").lower()
    call_llm_model_openai: str = os.getenv("CALL_LLM_MODEL_OPENAI", "gpt-4o-mini")
    call_llm_model_gemini: str = os.getenv("CALL_LLM_MODEL_GEMINI", "gemini-3-flash-preview")
    call_llm_model_azure_openai: str = os.getenv("CALL_LLM_MODEL_AZURE_OPENAI", "gpt-4o")

    # ── Task 4: STT ──
    stt_provider: str = os.getenv("STT_PROVIDER", "deepgram").lower()
    stt_model: str = os.getenv("STT_MODEL", "nova-3-general")

    # ── Task 5: TTS ──
    tts_provider: str = os.getenv("TTS_PROVIDER", "elevenlabs").lower()
    tts_model: str = os.getenv("TTS_MODEL", "eleven_flash_v2_5")
    tts_voice_id: str = os.getenv("TTS_VOICE_ID", "")

    # ── Simli avatar defaults ──
    # Face *generation* endpoint. "trinity" -> /faces/trinity (GS faces; requires
    # a Simli plan with GS-face quota). Anything else -> /faces/legacy. Default
    # legacy because GS-face creation is plan-gated (403 "max GS Faces").
    simli_face_model: str = os.getenv("SIMLI_FACE_MODEL", "legacy").lower()
    # Live call/render model. "trinity" -> keep Simli emotion (Trinity) faceId
    # behaviour; anything else -> bare faceId (legacy, no emotion suffix).
    simli_call_model: str = os.getenv("SIMLI_CALL_MODEL", "trinity").lower()
    default_simli_face_id: str = os.getenv("DEFAULT_SIMLI_FACE_ID", "")
    default_simli_voice_provider: str = os.getenv("DEFAULT_SIMLI_VOICE_PROVIDER", "elevenlabs")
    default_simli_voice_model: str = os.getenv("DEFAULT_SIMLI_VOICE_MODEL", "eleven_flash_v2_5")
    default_simli_voice_id: str = os.getenv("DEFAULT_SIMLI_VOICE_ID", "")

    # ── Azure OpenAI (used when *_PROVIDER=azure_openai) ──
    # gpt-4o resource: chat + vision (gender detection, voice description,
    # in-call LLM).
    azure_openai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_openai_api_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    azure_openai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    azure_openai_deployment: str = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    # gpt-image-2 resource: image edits / generation (avatar styling).
    azure_image_endpoint: str = os.getenv("AZURE_IMAGE_ENDPOINT", "")
    azure_image_api_key: str = os.getenv("AZURE_IMAGE_API_KEY", "")
    # ``gpt-image-2`` ``/images/edits`` is only available on
    # ``2025-04-01-preview`` and later. Older versions (e.g.
    # ``2024-02-01``) work for ``/images/generations`` but 404 on edits.
    azure_image_api_version: str = os.getenv("AZURE_IMAGE_API_VERSION", "2025-04-01-preview")
    azure_image_deployment: str = os.getenv("AZURE_IMAGE_DEPLOYMENT", "gpt-image-2")

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
    # URL prefix the LocalBlobStore stamps into DB rows when generating
    # references. Served by ``GET /local-blob/{key:path}`` in
    # ``app/main.py`` (defined for local-fallback testing only — there's
    # no tenant identifier in the URL because local fallback only ever
    # uses the ``local_tenant`` sentinel).
    local_blob_base_url: str = os.getenv(
        "LOCAL_BLOB_BASE_URL", "http://localhost:8000/local-blob"
    )


settings = Settings()
