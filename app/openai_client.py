from __future__ import annotations

import base64
import os
from typing import Any

import httpx

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
DEFAULT_GENDER_MODEL = os.getenv("OPENAI_GENDER_MODEL", "gpt-4o-mini")
DEFAULT_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
DEFAULT_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1024x1536")


class OpenAIError(RuntimeError):
    pass


async def detect_gender_from_image(
    api_key: str,
    image_bytes: bytes,
    *,
    model: str | None = None,
    mime_type: str = "image/png",
) -> str:
    if not api_key:
        return "unknown"

    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
    payload = {
        "model": model or DEFAULT_GENDER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify the apparent adult presentation in the portrait for avatar styling. "
                    "Return exactly one lowercase token: female, male, or unknown. "
                    "If the presentation is ambiguous or the subject is not clearly identifiable, return unknown."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Return only female, male, or unknown."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "max_completion_tokens": 8,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
    data = _parse_json(response)
    if response.status_code >= 400:
        raise OpenAIError(f"OpenAI gender detection failed ({response.status_code}): {data}")

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # pragma: no cover - defensive parsing
        raise OpenAIError(f"Unexpected OpenAI gender response: {data}") from exc

    text = str(content).strip().lower()
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
    model: str | None = None,
    mime_type: str = "image/png",
) -> str:
    """Return a 100-400 char voice-design description from a portrait + optional user prompt."""
    if not api_key:
        return ""

    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
    system_msg = (
        "You design voices for avatar assistants. Look at the portrait and suggest a voice that "
        "matches the person's apparent age, gender, mood, and style. Return ONE paragraph (120 "
        "to 350 characters) describing the voice in vivid but concrete terms: pitch, pace, timbre, "
        "accent, warmth, energy. Use natural English prose — no bullet lists, no labels, no JSON. "
        "If a user hint is provided, weave it in but never contradict the visual evidence."
    )
    user_text = "Describe the ideal voice for this person."
    if user_prompt:
        user_text += f"\n\nUser hint: {user_prompt.strip()}"

    payload = {
        "model": model or DEFAULT_GENDER_MODEL,
        "messages": [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "max_completion_tokens": 600,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(f"{OPENAI_BASE_URL}/chat/completions", headers=headers, json=payload)
    data = _parse_json(response)
    if response.status_code >= 400:
        raise OpenAIError(f"OpenAI voice description failed ({response.status_code}): {data}")
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:  # pragma: no cover
        raise OpenAIError(f"Unexpected OpenAI voice response: {data}") from exc
    return str(content).strip()


async def edit_image_with_prompt(
    api_key: str,
    image_bytes: bytes,
    *,
    filename: str,
    prompt: str,
    model: str | None = None,
) -> bytes:
    if not api_key:
        return image_bytes

    data = {
        "model": model or DEFAULT_IMAGE_MODEL,
        "prompt": prompt,
        "size": DEFAULT_IMAGE_SIZE,
        "quality": DEFAULT_IMAGE_QUALITY,
        "output_format": "png",
        "input_fidelity": "high",
    }
    files = {"image": (filename, image_bytes, "image/png")}
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(f"{OPENAI_BASE_URL}/images/edits", headers=headers, data=data, files=files)
    payload = _parse_json(response)
    if response.status_code >= 400:
        raise OpenAIError(f"OpenAI image edit failed ({response.status_code}): {payload}")

    if isinstance(payload, dict):
        image_entries = payload.get("data") or payload.get("output") or []
        if image_entries and isinstance(image_entries[0], dict):
            first = image_entries[0]
            b64 = first.get("b64_json") or first.get("image_base64") or first.get("base64")
            if b64:
                return base64.b64decode(b64)
            url = first.get("url")
            if url:
                async with httpx.AsyncClient(timeout=120) as client:
                    img_response = await client.get(url)
                if img_response.status_code >= 400:
                    raise OpenAIError(f"OpenAI image download failed ({img_response.status_code}): {img_response.text}")
                return img_response.content

    raise OpenAIError(f"OpenAI image edit returned no usable image payload: {payload}")


async def generate_avatar_variant(
    api_key: str,
    base_image_bytes: bytes,
    *,
    prompt_chain: list[str],
    filename: str,
    model: str | None = None,
) -> bytes:
    current = base_image_bytes
    prompts = [prompt.strip() for prompt in prompt_chain if prompt.strip()]
    if not prompts:
        return current

    for idx, prompt in enumerate(prompts, start=1):
        current = await edit_image_with_prompt(
            api_key,
            current,
            filename=f"step-{idx}-{filename}",
            prompt=prompt,
            model=model,
        )
    return current


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text
