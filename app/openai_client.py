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
                    "You are a visual classifier for avatar customization. Examine the portrait and identify "
                    "the apparent gender presentation of the adult subject. Reply with exactly one word — "
                    "female, male, or unknown — and nothing else. Use unknown if the subject is ambiguous, "
                    "not an adult, or not clearly visible."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is the apparent gender presentation of the person in this portrait?"},
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
