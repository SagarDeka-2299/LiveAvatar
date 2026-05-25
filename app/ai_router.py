from __future__ import annotations

from app.config import settings
from app import gemini_client, openai_client


class AIProviderError(RuntimeError):
    pass


async def detect_gender(image_bytes: bytes, *, mime_type: str = "image/png") -> str:
    provider = settings.gender_provider
    if provider == "gemini":
        if not settings.gemini_api_key:
            return "unknown"
        return await gemini_client.detect_gender_from_image(
            settings.gemini_api_key,
            image_bytes,
            model=settings.gender_model_gemini,
            mime_type=mime_type,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return "unknown"
        return await openai_client.detect_gender_from_image(
            settings.openai_api_key,
            image_bytes,
            model=settings.gender_model_openai,
            mime_type=mime_type,
        )
    raise AIProviderError(f"Unknown GENDER_PROVIDER: {provider}")


async def detect_gender_from_audio(
    audio_bytes: bytes, *, mime_type: str = "audio/mpeg"
) -> str:
    """Identify the speaker's gender from an audio clip.

    ElevenLabs does not return gender metadata on Instant Voice Cloning, so
    we route this through Gemini's audio-understanding model regardless of
    the configured ``GENDER_PROVIDER``. Returns ``"unknown"`` when the
    Gemini key is missing or detection fails.
    """
    if not settings.gemini_api_key:
        return "unknown"
    try:
        return await gemini_client.detect_gender_from_audio(
            settings.gemini_api_key,
            audio_bytes,
            model=settings.gender_model_gemini,
            mime_type=mime_type,
        )
    except Exception:
        return "unknown"


async def describe_voice(
    image_bytes: bytes,
    *,
    user_prompt: str = "",
    mime_type: str = "image/png",
) -> str:
    """Produce a natural-language voice description from a portrait. Uses the gender-detection provider."""
    provider = settings.gender_provider
    if provider == "gemini":
        if not settings.gemini_api_key:
            return ""
        return await gemini_client.describe_voice_from_image(
            settings.gemini_api_key,
            image_bytes,
            user_prompt=user_prompt,
            model=settings.gender_model_gemini,
            mime_type=mime_type,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return ""
        return await openai_client.describe_voice_from_image(
            settings.openai_api_key,
            image_bytes,
            user_prompt=user_prompt,
            model=settings.gender_model_openai,
            mime_type=mime_type,
        )
    raise AIProviderError(f"Unknown provider for voice description: {provider}")


async def generate_avatar(
    base_image_bytes: bytes,
    *,
    prompt_chain: list[str],
    filename: str,
) -> bytes:
    provider = settings.image_provider
    if provider == "gemini":
        if not settings.gemini_api_key:
            return base_image_bytes
        return await gemini_client.generate_avatar_variant(
            settings.gemini_api_key,
            base_image_bytes,
            prompt_chain=prompt_chain,
            filename=filename,
            model=settings.image_model_gemini,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return base_image_bytes
        return await openai_client.generate_avatar_variant(
            settings.openai_api_key,
            base_image_bytes,
            prompt_chain=prompt_chain,
            filename=filename,
            model=settings.image_model_openai,
        )
    raise AIProviderError(f"Unknown IMAGE_PROVIDER: {provider}")
