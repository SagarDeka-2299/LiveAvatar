from __future__ import annotations

import base64
import asyncio
import logging
import ssl
import json
from io import BytesIO
from typing import Any
import urllib.error
import urllib.request

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiError(RuntimeError):
    pass


async def detect_gender_from_image(
    api_key: str,
    image_bytes: bytes,
    *,
    model: str,
    mime_type: str = "image/png",
) -> str:
    if not api_key:
        return "unknown"

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "You are a visual classifier for avatar customization. Examine the portrait "
                            "and identify the apparent gender presentation of the adult subject. Reply "
                            "with exactly one word — female, male, or unknown — and nothing else. "
                            "Use unknown if the subject is ambiguous, not an adult, or not clearly visible."
                        ),
                    },
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 8,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(url, headers=headers, json=payload)
    data = _parse_json(response)
    if response.status_code >= 400:
        raise GeminiError(f"Gemini gender detection failed ({response.status_code}): {data}")

    text = _extract_text(data).strip().lower()
    if "female" in text:
        return "female"
    if "male" in text:
        return "male"
    return "unknown"


async def describe_voice_from_image(
    api_key: str,
    image_bytes: bytes,
    *,
    user_prompt: str = "",
    model: str,
    mime_type: str = "image/png",
) -> str:
    """Return a 120-350 char voice-design description from a portrait + optional user prompt."""
    if not api_key:
        return ""

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    system_text = (
        "You are a voice casting director for AI avatar assistants. Your output will be sent directly "
        "to ElevenLabs Voice Design to synthesize a unique voice. Given a portrait, write a 2–4 sentence "
        "voice design brief that a voice synthesis engine can use directly. Cover: vocal register and pitch, "
        "speaking pace and rhythm, tonal quality (warm/crisp/deep/breathy etc.), and any accent or regional "
        "quality if evident. Write in plain English prose — no bullet points, no labels, no JSON. Be specific and vivid."
    )
    user_text = "Write a voice design brief for the person in this portrait."
    if user_prompt:
        user_text += f"\n\nStyle direction from user: {user_prompt.strip()}"

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": system_text + "\n\n" + user_text},
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 600,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=headers, json=payload)
    data = _parse_json(response)
    if response.status_code >= 400:
        raise GeminiError(f"Gemini voice description failed ({response.status_code}): {data}")
    return _extract_text(data).strip()


async def edit_image_with_prompt(
    api_key: str,
    image_bytes: bytes,
    *,
    filename: str,
    prompt: str,
    model: str,
    mime_type: str = "image/png",
) -> tuple[bytes, str]:
    if not api_key:
        return image_bytes, mime_type

    image_bytes, mime_type = _prepare_image_for_gemini(image_bytes, mime_type=mime_type)
    return await _generate_image_with_sdk_retries(
        api_key,
        image_bytes,
        prompt=prompt,
        model=model,
        mime_type=mime_type,
    )


async def generate_avatar_variant(
    api_key: str,
    base_image_bytes: bytes,
    *,
    prompt_chain: list[str],
    filename: str,
    model: str,
) -> bytes:
    current = base_image_bytes
    current_mime = "image/png"
    prompts = [prompt.strip() for prompt in prompt_chain if prompt.strip()]
    if not prompts:
        return current

    for idx, prompt in enumerate(prompts, start=1):
        try:
            current, current_mime = await edit_image_with_prompt(
                api_key,
                current,
                filename=f"step-{idx}-{filename}",
                prompt=prompt,
                model=model,
                mime_type=current_mime,
            )
            logger.info("avatar step %d/%d ok — %d bytes %s", idx, len(prompts), len(current), current_mime)
        except Exception as exc:
            logger.error("avatar step %d/%d failed, keeping last good frame: %s", idx, len(prompts), exc)
            break
    return current


async def _post_json_with_retries(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    attempts: int = 3,
) -> tuple[int, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(
                _post_json_sync,
                url,
                headers=headers,
                payload=payload,
                timeout=timeout,
            )
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            last_exc = exc
            if attempt == attempts:
                break
            await asyncio.sleep(1.5 * attempt)
    raise GeminiError(f"Gemini request failed after {attempts} attempts: {last_exc}") from last_exc


async def _generate_image_with_sdk_retries(
    api_key: str,
    image_bytes: bytes,
    *,
    prompt: str,
    model: str,
    mime_type: str,
    attempts: int = 3,
) -> tuple[bytes, str]:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(
                _generate_image_with_sdk_sync,
                api_key,
                image_bytes,
                prompt=prompt,
                model=model,
                mime_type=mime_type,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning("gemini image attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt == attempts:
                break
            await asyncio.sleep(1.5 * attempt)
    raise GeminiError(f"Gemini image edit failed after {attempts} attempts: {last_exc}") from last_exc


def _generate_image_with_sdk_sync(
    api_key: str,
    image_bytes: bytes,
    *,
    prompt: str,
    model: str,
    mime_type: str,
) -> tuple[bytes, str]:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": b64}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {
                "imageSize": "1K",
            },
        },
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={api_key}"
    status, data = _post_json_sync(
        url,
        headers={"Content-Type": "application/json"},
        payload=payload,
        timeout=120,
    )
    if status >= 400:
        raise GeminiError(f"Gemini image edit failed ({status}): {data}")
    result = _extract_image_data(data)
    if not result:
        raise GeminiError(f"Gemini image edit returned no image payload: {data}")
    b64_image, out_mime = result
    return base64.b64decode(b64_image), out_mime


def _prepare_image_for_gemini(image_bytes: bytes, *, mime_type: str) -> tuple[bytes, str]:
    if len(image_bytes) <= 450_000 and mime_type != "image/png":
        return image_bytes, mime_type
    try:
        image = Image.open(BytesIO(image_bytes))
        image.thumbnail((768, 768))
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        out = BytesIO()
        image.save(out, format="JPEG", quality=82, optimize=True)
        compressed = out.getvalue()
        if len(compressed) < len(image_bytes):
            return compressed, "image/jpeg"
    except Exception:
        pass
    return image_bytes, mime_type


def _post_json_sync(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            data = body
        return exc.code, data


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _extract_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates") or []:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _extract_image_data(data: Any) -> tuple[str, str] | None:
    """Returns (base64_data, mime_type) or None."""
    if not isinstance(data, dict):
        return None
    for cand in data.get("candidates") or []:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/jpeg"
                return inline["data"], mime
    return None
