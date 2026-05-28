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


def pad_to_169(image_bytes: bytes, width: int = 1024, height: int = 576) -> bytes:
    """Letterbox an image into an exact ``width``×``height`` (default 1024×576,
    16:9) by scaling it to fit *within* the frame and padding with black bars.

    Unlike a cover-crop, this never cuts off any part of the image — a portrait
    gets side bars, a landscape gets top/bottom bars. Used to normalise the image
    sent to Simli's face endpoint so the face API always receives a fixed 16:9
    frame regardless of how the AI model or the user's crop produced the preview.
    Returns a PNG; falls back to the original bytes if PIL can't process the image.
    """
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode not in {"RGB", "RGBA", "L"}:
            img = img.convert("RGB")
        src_w, src_h = img.size
        if not src_w or not src_h:
            return image_bytes
        # Scale down to fit inside the target box (contain, not cover).
        scale = min(width / src_w, height / src_h)
        new_w = round(src_w * scale)
        new_h = round(src_h * scale)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        # Paste onto a black canvas.
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        left = (width - new_w) // 2
        top = (height - new_h) // 2
        canvas.paste(resized, (left, top))
        out = BytesIO()
        canvas.save(out, format="PNG")
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
    TTS preview synthesis. Returns ONE short sentence — kept deliberately
    brief so the preview audio is quick to listen to. Falls back to a
    generic line if the LLM is unavailable or the description is empty.

    NOTE: ElevenLabs voice design previews require the 'text' field to be
    at least 100 characters long, so we target ~100–130 characters (one
    sentence) — short, but still clearing that floor.
    """
    fallback = (
        "Hi there — it's lovely to meet you, and I hope today brings you "
        "something genuinely good and worth remembering."
    )
    desc = (description or "").strip()
    if not desc:
        return fallback

    system_msg = (
        "Write ONE short, natural sentence (about 100-130 characters) that a "
        "voice with the description below would say as a quick audio preview. "
        "Keep it to a single sentence — short and easy to listen to. Tailor it "
        "to the voice's character — a warm therapist gentle, an energetic coach "
        "motivational, a deep narrator cinematic. Return ONLY the sentence, no "
        "quotes, no preamble, no explanation."
    )
    user_msg = f"Voice description:\n{desc}\n\nWrite the one-sentence preview line (100-130 characters):"

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

    # ElevenLabs Voice Design needs ≥100 chars; nudge up only if we fell short.
    if len(cleaned) < 100:
        cleaned = f"{cleaned} It's a real pleasure to share this short preview with you today."
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
    # Compress the source image before sending to any image-edit API.
    # Large PNGs (e.g. the 1024×1024 from the persona cropper) cause Azure /
    # OpenAI to disconnect with "Server disconnected without sending a response"
    # because the multipart payload exceeds the API's soft size limit.
    compressed_bytes, _ = _prepare_vision_image(base_image_bytes)

    provider = settings.image_provider
    if provider == "gemini":
        if not settings.gemini_api_key:
            return base_image_bytes
        return await gemini_client.generate_avatar_variant(
            settings.gemini_api_key,
            compressed_bytes,
            prompt_chain=prompt_chain,
            filename=filename,
            model=settings.image_model_gemini,
        )
    if provider == "openai":
        if not settings.openai_api_key:
            return base_image_bytes
        return await openai_client.generate_avatar_variant(
            settings.openai_api_key,
            compressed_bytes,
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
            compressed_bytes,
            prompt_chain=prompt_chain,
            filename=filename,
            deployment=settings.azure_image_deployment,
            api_version=settings.azure_image_api_version,
        )
    raise AIProviderError(f"Unknown IMAGE_PROVIDER: {provider}")
