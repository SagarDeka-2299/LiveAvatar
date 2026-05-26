from __future__ import annotations

import base64
import json as _json
from typing import Any

import httpx

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


async def add_cloned_voice(
    api_key: str,
    *,
    name: str,
    description: str,
    audio_bytes: bytes,
    filename: str,
    mime_type: str = "audio/mpeg",
    labels: dict[str, str] | None = None,
) -> str:
    """Instant Voice Cloning. Returns voice_id. Requires paid tier.

    ``labels`` may include any of ``gender``, ``age``, ``accent``,
    ``language`` (free-form key/value strings — the documented keys are
    those four). Empty values are dropped.
    """
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is missing")

    files = [("files", (filename, audio_bytes, mime_type))]
    data: dict[str, str] = {
        "name": name,
        "description": description[:500] if description else "",
    }
    clean_labels = _clean_labels(labels)
    if clean_labels:
        data["labels"] = _json.dumps(clean_labels)
    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/voices/add",
            headers=headers,
            data=data,
            files=files,
        )
    payload = _parse_json(response)
    _raise_for_status(response, payload, action="voice clone")
    voice_id = _dig(payload, "voice_id")
    if not voice_id:
        raise ElevenLabsError(f"voices/add returned no voice_id: {payload}")
    return str(voice_id)


async def create_voice_design_previews(
    api_key: str,
    *,
    voice_description: str,
    text: str,
) -> list[dict[str, Any]]:
    """Voice Design step 1: generate 1-3 preview voices from a text description.

    Requires paid tier. Returns list of {generated_voice_id, audio_base_64, ...}.

    ElevenLabs caps ``voice_description`` at 1000 characters and 422s
    anything longer. Builders upstream (see ``build_voice_design_brief``
    in app.main) keep briefs well under that, but retries on stale DB
    rows can still surface an over-long string — so we hard-truncate
    here at the last clause boundary as defence in depth.
    """
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is missing")

    if len(voice_description) > 1000:
        cut = voice_description.rfind(";", 0, 1000)
        voice_description = (
            voice_description[:cut] if cut > 0 else voice_description[:1000]
        ).rstrip()

    payload = {
        "voice_description": voice_description,
        "model_id": "eleven_ttv_v3",
        "text": text,
        "auto_generate_text": False,
        "loudness": 0.5,
        "guidance_scale": 5,
        "should_enhance": True,
    }
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/text-to-voice/design",
            headers=headers,
            json=payload,
            params={"output_format": "mp3_22050_32"},
        )
    data = _parse_json(response)
    _raise_for_status(response, data, action="voice design previews")
    previews = (data or {}).get("previews") or []
    if not previews:
        raise ElevenLabsError(f"text-to-voice/design returned no previews: {data}")
    return previews


async def create_voice_from_preview(
    api_key: str,
    *,
    name: str,
    description: str,
    generated_voice_id: str,
    labels: dict[str, str] | None = None,
) -> str:
    """Voice Design step 2: commit a preview into a persistent voice. Returns voice_id.

    ``labels`` is forwarded to the upstream ``labels`` field — accepted keys
    documented as ``gender`` / ``age`` / ``accent`` / ``language``.
    """
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is missing")

    payload: dict[str, Any] = {
        "voice_name": name,
        "voice_description": description,
        "generated_voice_id": generated_voice_id,
    }
    clean_labels = _clean_labels(labels)
    if clean_labels:
        payload["labels"] = clean_labels
    headers = {"xi-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/text-to-voice",
            headers=headers,
            json=payload,
        )
    data = _parse_json(response)
    _raise_for_status(response, data, action="commit voice from preview")
    voice_id = _dig(data, "voice_id")
    if not voice_id:
        raise ElevenLabsError(f"text-to-voice returned no voice_id: {data}")
    return str(voice_id)


async def list_voices(api_key: str) -> list[dict[str, Any]]:
    """Return all voices in the account (premade + user-created)."""
    if not api_key:
        return []
    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(f"{ELEVENLABS_BASE_URL}/voices", headers=headers)
    data = _parse_json(response)
    if response.status_code >= 400:
        return []
    return data.get("voices") or []


async def delete_voice(api_key: str, voice_id: str) -> None:
    if not (api_key and voice_id):
        return
    headers = {"xi-api-key": api_key}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"{ELEVENLABS_BASE_URL}/voices/{voice_id}",
            headers=headers,
        )
    if response.status_code >= 400 and response.status_code != 404:
        raise ElevenLabsError(
            f"Failed to delete voice {voice_id}: {_parse_json(response)}",
            status_code=response.status_code,
        )


async def synthesize(
    api_key: str,
    *,
    voice_id: str,
    text: str,
    model_id: str = "eleven_flash_v2_5",
) -> bytes:
    """Call TTS to generate an MP3 preview. Returns audio bytes."""
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is missing")

    payload = {"text": text, "model_id": model_id}
    headers = {"xi-api-key": api_key, "Content-Type": "application/json", "accept": "audio/mpeg"}
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        data = _parse_json(response)
        _raise_for_status(response, data, action="TTS synthesize")
    return response.content


def decode_preview_audio(preview: dict[str, Any]) -> bytes:
    b64 = preview.get("audio_base_64") or preview.get("audio_base64") or ""
    if not isinstance(b64, str) or not b64:
        return b""
    return base64.b64decode(b64)


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _raise_for_status(response: httpx.Response, payload: Any, *, action: str) -> None:
    if response.status_code < 400:
        return
    code = ""
    message = ""
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload
        if isinstance(detail, dict):
            code = str(detail.get("status") or detail.get("code") or "")
            message = str(detail.get("message") or detail)
        else:
            message = str(detail)
    else:
        message = str(payload)
    raise ElevenLabsError(
        f"ElevenLabs {action} failed ({response.status_code}): {message}",
        status_code=response.status_code,
        code=code,
    )


def _clean_labels(labels: dict[str, str] | None) -> dict[str, str]:
    """Drop empty / non-string entries; coerce values to ``str``."""
    if not labels:
        return {}
    out: dict[str, str] = {}
    for k, v in labels.items():
        if not k:
            continue
        sv = str(v).strip() if v is not None else ""
        if sv:
            out[str(k)] = sv
    return out


def _dig(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            if isinstance(v, dict):
                r = _dig(v, key)
                if r is not None:
                    return r
    return None
