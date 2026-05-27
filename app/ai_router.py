from __future__ import annotations

from io import BytesIO
from PIL import Image
from app.ai_types import PersonaAnalysis
from app.config import settings
from app import azure_openai_client, gemini_client, openai_client


class AIProviderError(RuntimeError):
    pass


def _prepare_vision_image(image_bytes: bytes) -> tuple[bytes, str]:
    """Ensure the image is a standard, moderately sized JPEG (max 1024x1024)
    so it fits safely within Vision API request payload limits, avoiding
    TCP disconnects and timeouts.
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        # Downscale if larger than 1024x1024
        img.thumbnail((1024, 1024))
        # Convert to RGB if needed (JPEG doesn't support RGBA)
        if img.mode not in {"RGB", "L"}:
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        compressed = out.getvalue()
        return compressed, "image/jpeg"
    except Exception:
        # Fallback to original if PIL fails
        return image_bytes, "image/png"


def fit_to_169(image_bytes: bytes, width: int = 1024, height: int = 576) -> bytes:
    """Center-crop/scale an image to an exact ``width``×``height`` (default
    1024×576, 16:9) so a generated avatar matches the persona cropper's shape.

    Cover fit: scales to fill the frame and crops the overflow (no letterbox
    bars), mirroring the front-end cropper's ``getCroppedCanvas``. Returns a
    PNG; falls back to the original bytes if PIL can't process the image.
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode not in {"RGB", "RGBA", "L"}:
            img = img.convert("RGB")
        src_w, src_h = img.size
        if not src_w or not src_h:
            return image_bytes
        scale = max(width / src_w, height / src_h)
        resized = img.resize((round(src_w * scale), round(src_h * scale)), Image.LANCZOS)
        left = (resized.width - width) // 2
        top = (resized.height - height) // 2
        cropped = resized.crop((left, top, left + width, top + height))
        out = BytesIO()
        cropped.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return image_bytes


async def analyse_persona(
    image_bytes: bytes, *, mime_type: str = "image/png"
) -> PersonaAnalysis:
    """Single vision call returning apparent gender + a voice-design brief.

    Dispatches to the provider named by ``GENDER_PROVIDER``. Returns a
    strictly-typed :class:`PersonaAnalysis` (gender is
    ``Literal["male", "female"]``). On **any** failure — transport
    error, missing credentials, structured-output violation — the
    exception propagates so the caller (the persona create background
    task) can flip the persona row to ``status="failed"``.
    """
    image_bytes, mime_type = _prepare_vision_image(image_bytes)
    provider = settings.gender_provider
    if provider == "gemini":
        return await gemini_client.analyse_persona_from_image(
            settings.gemini_api_key,
            image_bytes,
            model=settings.gender_model_gemini,
            mime_type=mime_type,
        )
    if provider == "openai":
        return await openai_client.analyse_persona_from_image(
            settings.openai_api_key,
            image_bytes,
            model=settings.gender_model_openai,
            mime_type=mime_type,
        )
    if provider == "azure_openai":
        return await azure_openai_client.analyse_persona_from_image(
            settings.azure_openai_endpoint,
            settings.azure_openai_api_key,
            image_bytes,
            deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            mime_type=mime_type,
        )
    raise AIProviderError(f"Unknown GENDER_PROVIDER: {provider}")


async def generate_voice_preview_text(description: str) -> str:
    """One short audition line a voice with the given description would
    naturally say.

    Used to populate the ``text`` field of ElevenLabs Voice Design and
    TTS preview synthesis. Returns a single sentence (typically 5–15
    words). Falls back to a generic line if the LLM is unavailable or
    the description is empty.

    NOTE: ElevenLabs voice design previews require the 'text' field to
    be at least 100 characters long, so we ensure the prompt asks for
    at least 100 characters, and pad/fallback to a longer string if
    needed.
    """
    fallback = (
        "First, let me welcome you today. We are going to explore some truly "
        "wonderful things together in this session, and I hope you find it "
        "incredibly helpful and inspiring."
    )
    desc = (description or "").strip()
    if not desc:
        return fallback

    system_msg = (
        "You write a showcase paragraph that a voice actor would use to "
        "audition for the voice described. The audition passage MUST be at "
        "least 100 characters long (typically 20-30 words) to ensure a complete "
        "audio preview. Tailor it to the voice's character — a warm therapist "
        "would say something gentle, an energetic coach something motivational, "
        "a deep narrator something cinematic. Return ONLY the passage, no "
        "quotes, no preamble, no explanation."
    )
    user_msg = f"Voice description:\n{desc}\n\nWrite the showcase audition passage (must be at least 100 characters):"

    provider = settings.gender_provider
    try:
        if provider == "gemini":
            text = await gemini_client.generate_short_text(
                settings.gemini_api_key,
                system_msg=system_msg,
                user_msg=user_msg,
                model=settings.gender_model_gemini,
            )
        elif provider == "openai":
            text = await openai_client.generate_short_text(
                settings.openai_api_key,
                system_msg=system_msg,
                user_msg=user_msg,
                model=settings.gender_model_openai,
            )
        elif provider == "azure_openai":
            text = await azure_openai_client.generate_short_text(
                settings.azure_openai_endpoint,
                settings.azure_openai_api_key,
                system_msg=system_msg,
                user_msg=user_msg,
                deployment=settings.azure_openai_deployment,
                api_version=settings.azure_openai_api_version,
            )
        else:
            return fallback
    except Exception:
        return fallback

    # The model occasionally wraps the line in quotes or adds a leading
    # "Sure! Here's the line:" — strip both.
    cleaned = text.strip().strip('"').strip("'").strip()
    
    # Join non-empty lines to preserve the length of the generated passage
    lines = [line.strip().strip('"').strip("'").strip() for line in cleaned.splitlines() if line.strip()]
    if lines:
        cleaned = " ".join(lines)
    else:
        cleaned = fallback

    # Ensure it is at least 100 characters
    if len(cleaned) < 100:
        padding = (
            " This audition passage is designed to showcase the full range, "
            "depth, and unique qualities of this voice across multiple sentences."
        )
        cleaned = f"{cleaned} {padding}"
        if len(cleaned) < 100:
            cleaned = fallback

    return cleaned


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
    """Standalone voice-design brief from a portrait + an optional user prompt.

    Distinct from :func:`analyse_persona`: this is used by the persona-voice
    design endpoint and ``/voices/suggest-description`` where the caller
    supplies a specific style hint. The two-field combined analysis used at
    persona creation lives in :func:`analyse_persona`.
    """
    image_bytes, mime_type = _prepare_vision_image(image_bytes)
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
    if provider == "azure_openai":
        if not (settings.azure_openai_endpoint and settings.azure_openai_api_key):
            return ""
        return await azure_openai_client.describe_voice_from_image(
            settings.azure_openai_endpoint,
            settings.azure_openai_api_key,
            image_bytes,
            user_prompt=user_prompt,
            deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
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
    if provider == "azure_openai":
        if not (settings.azure_image_endpoint and settings.azure_image_api_key):
            return base_image_bytes
        return await azure_openai_client.generate_avatar_variant(
            settings.azure_image_endpoint,
            settings.azure_image_api_key,
            base_image_bytes,
            prompt_chain=prompt_chain,
            filename=filename,
            deployment=settings.azure_image_deployment,
            api_version=settings.azure_image_api_version,
        )
    raise AIProviderError(f"Unknown IMAGE_PROVIDER: {provider}")
