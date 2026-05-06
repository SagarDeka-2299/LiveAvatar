from __future__ import annotations

import base64
from typing import Any

import httpx

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
                            "Classify the apparent adult presentation in the portrait for "
                            "avatar styling. Return exactly one lowercase token: female, male, "
                            "or unknown. If ambiguous or the subject is not clearly identifiable, "
                            "return unknown."
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
) -> bytes:
    if not api_key:
        return image_bytes

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
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(url, headers=headers, json=payload)
    data = _parse_json(response)
    if response.status_code >= 400:
        raise GeminiError(f"Gemini image edit failed ({response.status_code}): {data}")

    img_b64 = _extract_image_b64(data)
    if not img_b64:
        raise GeminiError(f"Gemini image edit returned no image payload: {data}")
    return base64.b64decode(img_b64)


async def generate_avatar_variant(
    api_key: str,
    base_image_bytes: bytes,
    *,
    prompt_chain: list[str],
    filename: str,
    model: str,
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


def _extract_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for cand in data.get("candidates") or []:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def _extract_image_b64(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for cand in data.get("candidates") or []:
        parts = ((cand or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                return inline["data"]
    return None
