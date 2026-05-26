"""Azure OpenAI client — chat/vision (gpt-4o) + image edits (gpt-image-2).

The two services live on **separate Azure resources** with their own
endpoints + keys, but they share the same vendor and the same general
request/response shape as standard OpenAI (chat completions / images).

Endpoints (deployment-rooted, ``api-version`` always on the query):

  Chat / vision:
      {endpoint}/openai/deployments/{deployment}/chat/completions?api-version={ver}

  Image edits (multi-part):
      {endpoint}/openai/deployments/{deployment}/images/edits?api-version={ver}

Auth: ``api-key`` header.

This module mirrors the public surface of ``app.openai_client`` /
``app.gemini_client`` so ``app.ai_router`` can swap providers without
touching call sites.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
from app.ai_types import PERSONA_ANALYSIS_JSON_SCHEMA, PersonaAnalysis


class AzureOpenAIError(RuntimeError):
    pass


# ── Chat / vision (gpt-4o) ────────────────────────────────────────────────────

def _chat_url(endpoint: str, deployment: str, api_version: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )


async def analyse_persona_from_image(
    endpoint: str,
    api_key: str,
    image_bytes: bytes,
    *,
    deployment: str,
    api_version: str,
    mime_type: str = "image/png",
) -> PersonaAnalysis:
    """Single vision call returning both apparent gender and a voice-design
    brief from one portrait.

    Uses Azure OpenAI's **strict structured output** mode
    (``response_format.type = json_schema`` with ``strict: true``) — the
    model is constrained at inference time to produce exactly the
    :class:`PersonaAnalysis` shape. Anything that escapes the schema or
    a transport-level error propagates as an exception; the caller is
    responsible for marking the persona as ``failed``.
    """
    if not (endpoint and api_key):
        raise AzureOpenAIError(
            "Azure OpenAI endpoint / api_key missing — cannot analyse persona."
        )

    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
    system_msg = (
        "You analyse portraits for an AI avatar product. Given a portrait, "
        "decide the apparent gender presentation (male or female) and "
        "write a 2–4 sentence voice-design brief that ElevenLabs Voice "
        "Design can consume directly. Cover vocal register and pitch, "
        "speaking pace and rhythm, tonal quality (warm/crisp/deep/breathy "
        "etc.), and any accent or regional quality if evident. The brief "
        "must be plain prose — no markdown, no bullet points, no labels."
    )
    payload = {
        "messages": [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Analyse the portrait."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        "max_tokens": 700,
        "temperature": 0.2,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "PersonaAnalysis",
                "schema": PERSONA_ANALYSIS_JSON_SCHEMA,
                "strict": True,
            },
        },
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            _chat_url(endpoint, deployment, api_version),
            headers=headers,
            json=payload,
        )
    data = _parse_json(response)
    if response.status_code >= 400:
        raise AzureOpenAIError(
            f"Azure OpenAI persona analysis failed ({response.status_code}): {data}"
        )
    # Strict structured output — Pydantic just confirms what Azure already
    # enforced server-side. A ValidationError here means the response
    # genuinely violated the schema (refusal, hallucinated extras, etc.)
    # and we want the caller to see it.
    return PersonaAnalysis.model_validate_json(_extract_chat_text(data))


async def describe_voice_from_image(
    endpoint: str,
    api_key: str,
    image_bytes: bytes,
    *,
    user_prompt: str = "",
    deployment: str,
    api_version: str,
    mime_type: str = "image/png",
) -> str:
    """Return a 100-400 char voice-design description from a portrait."""
    if not (endpoint and api_key):
        return ""

    image_url = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('utf-8')}"
    system_msg = (
        "You are a voice casting director for AI avatar assistants. Your "
        "output will be sent directly to ElevenLabs Voice Design to "
        "synthesize a unique voice. Given a portrait, write a 2–4 sentence "
        "voice design brief that a voice synthesis engine can use directly. "
        "Cover: vocal register and pitch, speaking pace and rhythm, tonal "
        "quality (warm/crisp/deep/breathy etc.), and any accent or regional "
        "quality if evident. Write in plain English prose — no bullet "
        "points, no labels, no JSON. Be specific and vivid."
    )
    user_text = "Write a voice design brief for the person in this portrait."
    if user_prompt:
        user_text += f"\n\nStyle direction from user: {user_prompt.strip()}"

    payload = {
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
        "max_tokens": 600,
        "temperature": 0.4,
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            _chat_url(endpoint, deployment, api_version),
            headers=headers,
            json=payload,
        )
    data = _parse_json(response)
    if response.status_code >= 400:
        raise AzureOpenAIError(
            f"Azure OpenAI voice description failed ({response.status_code}): {data}"
        )
    return _extract_chat_text(data).strip()


# ── Image edits (gpt-image-2) ─────────────────────────────────────────────────

async def generate_short_text(
    endpoint: str,
    api_key: str,
    *,
    system_msg: str,
    user_msg: str,
    deployment: str,
    api_version: str,
    max_tokens: int = 80,
    temperature: float = 0.6,
) -> str:
    """Plain chat completion that returns a short text string. Used for
    one-off generative chores like 'write a voice-actor audition line'."""
    if not (endpoint and api_key):
        raise AzureOpenAIError(
            "Azure OpenAI endpoint / api_key missing — cannot run short-text completion."
        )
    payload = {
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            _chat_url(endpoint, deployment, api_version),
            headers=headers,
            json=payload,
        )
    data = _parse_json(response)
    if response.status_code >= 400:
        raise AzureOpenAIError(
            f"Azure OpenAI short-text failed ({response.status_code}): {data}"
        )
    return _extract_chat_text(data)


def _images_edits_url(endpoint: str, deployment: str, api_version: str) -> str:
    return (
        f"{endpoint.rstrip('/')}/openai/deployments/{deployment}"
        f"/images/edits?api-version={api_version}"
    )


async def edit_image_with_prompt(
    endpoint: str,
    api_key: str,
    image_bytes: bytes,
    *,
    filename: str,
    prompt: str,
    deployment: str,
    api_version: str,
    size: str = "1024x1024",
    quality: str = "medium",
) -> bytes:
    """One-shot Azure gpt-image-2 edit. Returns the edited image bytes (PNG)."""
    if not (endpoint and api_key):
        return image_bytes

    data = {
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "output_format": "png",
        "n": 1,
    }
    files = {"image": (filename, image_bytes, "image/png")}
    headers = {"api-key": api_key}

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            _images_edits_url(endpoint, deployment, api_version),
            headers=headers,
            data=data,
            files=files,
        )
    payload = _parse_json(response)
    if response.status_code >= 400:
        raise AzureOpenAIError(
            f"Azure OpenAI image edit failed ({response.status_code}): {payload}"
        )
    if isinstance(payload, dict):
        entries = payload.get("data") or []
        if entries and isinstance(entries[0], dict):
            b64 = entries[0].get("b64_json")
            if b64:
                return base64.b64decode(b64)
            url = entries[0].get("url")
            if url:
                async with httpx.AsyncClient(timeout=120) as client:
                    img = await client.get(url)
                if img.status_code >= 400:
                    raise AzureOpenAIError(
                        f"Azure image download failed ({img.status_code}): {img.text}"
                    )
                return img.content
    raise AzureOpenAIError(
        f"Azure OpenAI image edit returned no usable image payload: {payload}"
    )


async def generate_avatar_variant(
    endpoint: str,
    api_key: str,
    base_image_bytes: bytes,
    *,
    prompt_chain: list[str],
    filename: str,
    deployment: str,
    api_version: str,
) -> bytes:
    """Apply each prompt in ``prompt_chain`` to the image sequentially."""
    current = base_image_bytes
    prompts = [p.strip() for p in prompt_chain if p.strip()]
    if not prompts:
        return current
    for idx, prompt in enumerate(prompts, start=1):
        current = await edit_image_with_prompt(
            endpoint,
            api_key,
            current,
            filename=f"step-{idx}-{filename}",
            prompt=prompt,
            deployment=deployment,
            api_version=api_version,
        )
    return current


# ── Internals ─────────────────────────────────────────────────────────────────

def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _extract_chat_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    content = msg.get("content")
    return str(content or "")
