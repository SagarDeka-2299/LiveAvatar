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


settings = Settings()
