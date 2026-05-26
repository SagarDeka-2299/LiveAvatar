from __future__ import annotations

import asyncio
import ssl
from typing import Any

import httpx

SIMLI_BASE_URL = "https://api.simli.ai"


class SimliError(RuntimeError):
    pass


def _parse_json_response(response: httpx.Response) -> Any: 
    try:
        return response.json()
    except ValueError:
        return response.text


def extract_face_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("face_id", "faceId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            face_id = extract_face_id(value)
            if face_id:
                return face_id
    if isinstance(payload, list):
        for item in payload:
            face_id = extract_face_id(item)
            if face_id:
                return face_id
    return ""


def extract_generation_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("character_uid", "face_id", "faceId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            generation_id = extract_generation_id(value)
            if generation_id:
                return generation_id
    if isinstance(payload, list):
        for item in payload:
            generation_id = extract_generation_id(item)
            if generation_id:
                return generation_id
    return ""


def normalize_generation_status(payload: Any) -> str:
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status:
            return status.lower()
        message = payload.get("message")
        if isinstance(message, str) and "queue" in message.lower():
            return "processing"
    return "unknown"


async def create_agent(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "x-simli-api-key": api_key,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{SIMLI_BASE_URL}/agent", headers=headers, json=payload)
    if response.status_code >= 400:
        raise SimliError(f"Simli create agent failed ({response.status_code}): {response.text}")
    return response.json()


async def upload_face_image(api_key: str, image_bytes: bytes, filename: str, face_name: str) -> dict[str, Any]:
    files = {"image": (filename, image_bytes)}
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"{SIMLI_BASE_URL}/faces/trinity",
                    headers={"x-simli-api-key": api_key},
                    params={"face_name": face_name},
                    files=files,
                )

            payload = _parse_json_response(response)
            if response.status_code >= 400:
                raise SimliError(f"Simli face upload failed ({response.status_code}): {payload}")
            if not isinstance(payload, dict):
                return {"raw": payload}
            return payload
        except (ssl.SSLError, httpx.NetworkError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(2.0 * attempt)
    raise SimliError(f"Simli face upload failed after 3 attempts: {last_exc}") from last_exc


async def get_face_generation_status(api_key: str, face_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{SIMLI_BASE_URL}/faces/trinity/generation_status",
            headers={"x-simli-api-key": api_key},
            params={"face_id": face_id},
        )
    payload = _parse_json_response(response)
    if response.status_code >= 400:
        raise SimliError(f"Simli generation status failed ({response.status_code}): {payload}")
    if not isinstance(payload, dict):
        return {"raw": payload}
    return payload


async def create_auto_session_token(
    api_key: str,
    create_transcript: bool,
    expiry_stamp: int,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "x-simli-api-key": api_key,
    }
    payload = {
        "simliAPIKey": api_key,
        "expiryStamp": expiry_stamp,
        "llmAPIKey": "",
        "ttsAPIKey": "",
        "originAllowList": [],
        "createTranscript": create_transcript,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{SIMLI_BASE_URL}/auto/token", headers=headers, json=payload)
    if response.status_code >= 400:
        raise SimliError(f"Simli session token failed ({response.status_code}): {response.text}")
    return response.json()


async def list_faces(api_key: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{SIMLI_BASE_URL}/faces",
            headers={"x-simli-api-key": api_key},
        )
    payload = _parse_json_response(response)
    if response.status_code >= 400:
        raise SimliError(f"Simli get faces failed ({response.status_code}): {payload}")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def delete_face(api_key: str, face_id: str) -> None:
    headers = {"x-simli-api-key": api_key}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(f"{SIMLI_BASE_URL}/faces/trinity/{face_id}", headers=headers)
        if response.status_code in {200, 202, 204, 404}:
            return
        legacy_response = await client.delete(f"{SIMLI_BASE_URL}/faces/legacy/{face_id}", headers=headers)
        if legacy_response.status_code in {200, 202, 204, 404}:
            return
    payload = _parse_json_response(legacy_response)
    raise SimliError(f"Simli delete face failed ({legacy_response.status_code}): {payload}")


async def start_auto_session(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /auto/start/configurable — one-shot Simli Auto session.

    Returns ``{"roomUrl": ..., "sessionId": ...}``. ``roomUrl`` is a Daily
    room URL the frontend joins via the Daily JS SDK.
    """
    headers = {
        "Content-Type": "application/json",
        "x-simli-api-key": api_key,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{SIMLI_BASE_URL}/auto/start/configurable",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        raise SimliError(f"Simli auto-session start failed ({response.status_code}): {response.text}")
    return response.json()


async def list_auto_agents(api_key: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{SIMLI_BASE_URL}/auto/agents",
            headers={"x-simli-api-key": api_key},
        )
    payload = _parse_json_response(response)
    if response.status_code >= 400:
        raise SimliError(f"Simli get agents failed ({response.status_code}): {payload}")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


async def delete_auto_agent(api_key: str, agent_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.delete(
            f"{SIMLI_BASE_URL}/auto/agent/{agent_id}",
            headers={"x-simli-api-key": api_key},
        )
    if response.status_code in {200, 202, 204, 404}:
        return
    payload = _parse_json_response(response)
    raise SimliError(f"Simli delete agent failed ({response.status_code}): {payload}")
