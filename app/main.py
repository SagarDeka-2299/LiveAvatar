from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import ai_router, elevenlabs_client
from app.elevenlabs_client import ElevenLabsError

from app.config import settings
from app.db import (
    count_assistants_for_avatar,
    count_assistants_for_persona,
    count_avatars_for_persona,
    delete_all_assistants,
    delete_all_persona_avatars,
    delete_all_persona_entities,
    delete_assistant_entity,
    delete_persona_avatar,
    delete_persona_entity,
    delete_voice_entity,
    get_assistant,
    get_persona_avatar,
    get_persona_entity,
    get_voice,
    init_db,
    insert_assistant,
    insert_persona_avatar,
    insert_persona_entity,
    insert_voice,
    list_assistants,
    list_persona_avatars,
    list_persona_entities,
    list_voices,
    update_assistant,
    update_persona_avatar,
    update_persona_entity,
    update_voice,
)
from app.livekit_tokens import build_agent_dispatch_room_config, create_join_token
from app.schemas import (
    Assistant,
    AssistantCallCreate,
    AssistantCallResponse,
    AssistantCreate,
    AvatarPreviewResponse,
    LivekitTokenRequest,
    PersonaAvatar,
    PersonaEntity,
    PersonaVoiceDesignRequest,
    SimliSessionTokenRequest,
    VoiceEntity,
)
from app.simli_client import (
    SimliError,
    create_auto_session_token,
    delete_auto_agent,
    delete_face,
    extract_face_id,
    extract_generation_id,
    get_face_generation_status,
    list_auto_agents,
    list_faces,
    normalize_generation_status,
    upload_face_image,
)
from app.ws import UpdateHub

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
UPLOADS_DIR = STATIC_DIR / "uploads"
PLACEHOLDER_IMAGE_PATH = "/static/avatar-placeholder.svg"

app = FastAPI(title="Avatar Agent Studio", version="0.3.0")
update_hub = UpdateHub()


def _parse_preset_ids(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


AVATAR_PRESETS: dict[str, dict[str, str]] = {
    "casual_outfit":  {"cat": "Outfit",      "prompt": "casual modern streetwear"},
    "formal_suit":    {"cat": "Outfit",      "prompt": "a formal professional suit"},
    "sporty_wear":    {"cat": "Outfit",      "prompt": "athletic sporty outfit"},
    "elegant_dress":  {"cat": "Outfit",      "prompt": "an elegant sophisticated dress"},
    "biz_blouse":     {"cat": "Outfit",      "prompt": "a professional business blouse"},
    "biz_suit_m":     {"cat": "Outfit",      "prompt": "a sharp tailored business suit with tie"},
    "leather_jkt":    {"cat": "Outfit",      "prompt": "a cool leather jacket"},
    "glasses":        {"cat": "Accessories", "prompt": "fashionable eyeglasses"},
    "watch_acc":      {"cat": "Accessories", "prompt": "a luxury wristwatch"},
    "studio_bg":      {"cat": "Background",  "prompt": "a clean neutral studio background"},
    "office_bg":      {"cat": "Background",  "prompt": "a modern corporate office background"},
    "outdoor_bg":     {"cat": "Background",  "prompt": "a natural outdoor environment background"},
    "red_lips":       {"cat": "Makeup",      "prompt": "vibrant red lipstick"},
    "smoky_eyes":     {"cat": "Makeup",      "prompt": "dramatic smoky eye makeup"},
    "natural_glow":   {"cat": "Makeup",      "prompt": "natural glowing skin with minimal makeup"},
    "earrings":       {"cat": "Jewelry",     "prompt": "elegant drop earrings"},
    "necklace":       {"cat": "Jewelry",     "prompt": "a delicate necklace"},
    "updo_hair":      {"cat": "Hair",        "prompt": "an elegant updo hairstyle"},
    "braids":         {"cat": "Hair",        "prompt": "beautifully braided hair"},
    "beard_stubble":  {"cat": "Facial Hair", "prompt": "a short stylish beard stubble"},
    "full_beard":     {"cat": "Facial Hair", "prompt": "a well-groomed full beard"},
    "clean_shave":    {"cat": "Facial Hair", "prompt": "clean shaven smooth skin"},
}

PRESET_CATEGORY_LABEL = {
    "Outfit": "Clothing",
    "Hair": "Hair",
    "Facial Hair": "Facial Hair",
    "Makeup": "Makeup",
    "Accessories": "Accessories",
    "Jewelry": "Jewelry",
    "Background": "Background",
}
PRESET_ORDER = ["Outfit", "Hair", "Facial Hair", "Makeup", "Accessories", "Jewelry", "Background"]


def build_avatar_edit_prompt(preset_ids: list[str], custom_prompt: str = "") -> str:
    """Build a structured face-locked prompt grouping presets by category."""
    grouped: dict[str, list[str]] = {}
    for pid in preset_ids:
        p = AVATAR_PRESETS.get(pid)
        if not p:
            continue
        grouped.setdefault(p["cat"], []).append(p["prompt"])

    sections: list[str] = []
    for cat in PRESET_ORDER:
        if cat in grouped:
            label = PRESET_CATEGORY_LABEL[cat]
            sections.append(f"- {label}: {', '.join(grouped[cat])}")
    if custom_prompt.strip():
        sections.append(f"- Additional: {custom_prompt.strip()}")

    if not sections:
        return ""

    body = "\n".join(sections)
    return (
        "Edit the provided portrait. CRITICAL: keep the face, identity, age, "
        "ethnicity, skin tone and bone structure of the person EXACTLY identical "
        "to the source image. Do not alter face shape, eyes, nose, mouth, "
        "expression, or proportions. Only modify the elements listed below.\n\n"
        f"Apply these changes:\n{body}\n\n"
        "Maintain photorealistic quality, natural lighting, and the original "
        "portrait framing and aspect ratio."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


def normalize_image_path(image_path: str) -> str:
    if image_path.startswith("/static/"):
        static_relative_path = image_path.removeprefix("/static/")
        if (STATIC_DIR / static_relative_path).exists():
            return image_path
    return PLACEHOLDER_IMAGE_PATH


async def send_avatar_event(client_id: str | None, event: str, **payload: object) -> None:
    if not client_id:
        return
    await update_hub.send(client_id, {"event": event, **payload})


def build_persona_library() -> list[PersonaEntity]:
    personas = list_persona_entities()
    avatars = list_persona_avatars()
    avatar_models = {
        avatar["id"]: PersonaAvatar(
            **{
                **avatar,
                "image_path": normalize_image_path(avatar["image_path"]),
            }
        )
        for avatar in avatars
    }

    persona_models: list[PersonaEntity] = []
    for persona in personas:
        nested = [
            avatar_models[avatar["id"]]
            for avatar in avatars
            if avatar["persona_id"] == persona["id"]
        ]
        persona_models.append(
            PersonaEntity(
                id=persona["id"],
                name=persona["name"],
                image_path=normalize_image_path(persona["image_path"]),
                avatars=nested,
            )
        )
    return persona_models


async def refresh_avatar_status(avatar: dict[str, object]) -> dict[str, object]:
    if avatar["status"] != "processing":
        avatar["image_path"] = normalize_image_path(str(avatar["image_path"]))
        return avatar

    try:
        status_response = await get_face_generation_status(settings.simli_api_key, str(avatar["face_id"]))
    except SimliError as exc:
        avatar["last_error"] = str(exc)
        return avatar

    status = normalize_generation_status(status_response)
    ready_face_id = extract_face_id(status_response) or str(avatar["face_id"])
    if status in {"processing", "queued", "pending"}:
        avatar["status"] = "processing"
    elif status in {"failed", "error"}:
        avatar["status"] = "failed"
        avatar["last_error"] = str(status_response)
    else:
        avatar["status"] = "ready"
        avatar["face_id"] = ready_face_id
        avatar["last_error"] = None

    update_persona_avatar(
        int(avatar["id"]),
        face_id=str(avatar["face_id"]),
        status=str(avatar["status"]),
        last_error=None if avatar["last_error"] is None else str(avatar["last_error"]),
    )
    avatar["image_path"] = normalize_image_path(str(avatar["image_path"]))
    return avatar


async def poll_avatar_until_ready(avatar_id: int, client_id: str | None) -> None:
    while True:
        avatar = get_persona_avatar(avatar_id)
        if not avatar:
            await send_avatar_event(client_id, "avatar.missing", avatar_id=avatar_id)
            return

        # Bail out if user cancelled
        if str(avatar.get("status", "")) == "cancelled":
            await send_avatar_event(client_id, "avatar.cancelled", avatar_id=avatar_id)
            return
        avatar = await refresh_avatar_status(avatar)
        await send_avatar_event(
            client_id,
            "avatar.status",
            avatar_id=avatar["id"],
            persona_id=avatar["persona_id"],
            name=avatar["name"],
            status=avatar["status"],
            face_id=avatar["face_id"],
            image_path=avatar["image_path"],
        )

        if avatar["status"] == "processing":
            await send_avatar_event(client_id, "simli.status", avatar_id=avatar["id"], status="processing")
            await asyncio.sleep(7)
            continue

        if avatar["status"] == "ready":
            await send_avatar_event(
                client_id,
                "avatar.ready",
                avatar_id=avatar["id"],
                persona_id=avatar["persona_id"],
                name=avatar["name"],
                status="ready",
                face_id=avatar["face_id"],
                image_path=avatar["image_path"],
            )
            return

        await send_avatar_event(
            client_id,
            "avatar.failed",
            avatar_id=avatar["id"],
            persona_id=avatar["persona_id"],
            name=avatar["name"],
            status="failed",
            detail=avatar["last_error"] or "Avatar generation failed",
        )
        return


def validate_image_upload(upload: UploadFile, image_bytes: bytes) -> None:
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Image is required")
    if upload.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Use a PNG, JPG, or WEBP image")


def write_upload_file(filename: str, image_bytes: bytes) -> str:
    suffix = Path(filename).suffix or ".png"
    image_name = f"{uuid4().hex}{suffix.lower()}"
    file_path = UPLOADS_DIR / image_name
    file_path.write_bytes(image_bytes)
    return f"/static/uploads/{image_name}"


def read_persona_image_bytes(persona: dict[str, object]) -> bytes:
    image_path = str(persona["image_path"])
    if not image_path.startswith("/static/uploads/"):
        raise HTTPException(status_code=400, detail="Persona image is not stored locally")
    local_path = STATIC_DIR / image_path.removeprefix("/static/")
    if not local_path.exists():
        raise HTTPException(status_code=404, detail="Persona image file is missing")
    return local_path.read_bytes()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, str | bool]:
    return {
        "livekit_url": settings.livekit_url,
        "has_livekit_creds": bool(settings.livekit_api_key and settings.livekit_api_secret),
        "has_simli_api_key": bool(settings.simli_api_key),
        "simli_widget_script": "https://app.simli.com/simli-widget/index.js",
    }


@app.get("/api/studio/personas", response_model=list[PersonaEntity])
async def get_studio_personas() -> list[PersonaEntity]:
    avatars = [await refresh_avatar_status(avatar) for avatar in list_persona_avatars()]
    persona_map = {persona["id"]: persona for persona in list_persona_entities()}
    nested: dict[int, list[PersonaAvatar]] = {persona_id: [] for persona_id in persona_map}
    for avatar in avatars:
        nested.setdefault(int(avatar["persona_id"]), []).append(
            PersonaAvatar(
                id=int(avatar["id"]),
                persona_id=int(avatar["persona_id"]),
                name=str(avatar["name"]),
                decoration=str(avatar["decoration"]),
                theme_prompt=str(avatar.get("theme_prompt") or ""),
                preset_ids=_parse_preset_ids(avatar.get("preset_ids")),
                face_id=str(avatar["face_id"]),
                image_path=normalize_image_path(str(avatar["image_path"])),
                status=str(avatar["status"]),
                progress=int(avatar.get("progress") or 0),
                stage=str(avatar.get("stage") or "queued"),
                last_error=None if avatar["last_error"] is None else str(avatar["last_error"]),
            )
        )

    return [
        _persona_to_model(persona, nested.get(persona_id, []))
        for persona_id, persona in sorted(persona_map.items(), reverse=True)
    ]


def _persona_to_model(persona: dict[str, object], avatars: list[PersonaAvatar]) -> PersonaEntity:
    return PersonaEntity(
        id=int(persona["id"]),
        name=str(persona["name"]),
        image_path=normalize_image_path(str(persona["image_path"])),
        gender=str(persona.get("gender") or "unknown"),
        status=str(persona.get("status") or "ready"),
        progress=int(persona.get("progress") or 0),
        stage=str(persona.get("stage") or "ready"),
        last_error=None if persona.get("last_error") is None else str(persona["last_error"]),
        voice_provider=str(persona.get("voice_provider") or ""),
        voice_id=str(persona.get("voice_id") or ""),
        voice_source=str(persona.get("voice_source") or ""),
        voice_description=str(persona.get("voice_description") or ""),
        voice_sample_path=str(persona.get("voice_sample_path") or ""),
        voice_preview_path=str(persona.get("voice_preview_path") or ""),
        voice_status=str(persona.get("voice_status") or ""),
        voice_last_error=None if persona.get("voice_last_error") is None else str(persona["voice_last_error"]),
        avatars=avatars,
    )


@app.post("/api/studio/personas", response_model=PersonaEntity, status_code=201)
async def post_studio_persona(
    persona_image: UploadFile = File(...),
    name: str = Form("Persona"),
    client_id: str | None = Form(default=None),
) -> PersonaEntity:
    image_bytes = await persona_image.read()
    validate_image_upload(persona_image, image_bytes)
    image_path = write_upload_file(persona_image.filename or "persona.png", image_bytes)
    persona_id = insert_persona_entity({
        "name": name,
        "image_path": image_path,
        "gender": "unknown",
        "status": "processing",
        "stage": "detecting_gender",
    })
    persona = PersonaEntity(
        id=persona_id, name=name, image_path=image_path,
        gender="unknown", status="processing", stage="detecting_gender", avatars=[],
    )
    await send_avatar_event(client_id, "persona.queued", persona_id=persona_id, name=name)
    asyncio.create_task(_process_new_persona(persona_id, image_bytes, client_id))
    return persona


async def _process_new_persona(persona_id: int, image_bytes: bytes, client_id: str | None) -> None:
    gender = "unknown"
    voice_description = ""
    try:
        gender = await ai_router.detect_gender(image_bytes)
    except Exception:
        gender = "unknown"
    try:
        voice_description = (await ai_router.describe_voice(image_bytes, user_prompt="") or "").strip()
    except Exception:
        voice_description = ""
    update_persona_entity(
        persona_id,
        gender=gender,
        voice_description=voice_description,
        status="ready", stage="ready", progress=100,
    )
    await send_avatar_event(client_id, "persona.ready", persona_id=persona_id, gender=gender)


@app.post("/api/studio/avatars", response_model=PersonaAvatar, status_code=201)
async def post_studio_avatar(
    persona_id: int = Form(...),
    name: str = Form(...),
    decoration: str = Form(default=""),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    draft_avatar_id: int | None = Form(default=None),
    client_id: str | None = Form(default=None),
) -> PersonaAvatar:
    if not settings.simli_api_key:
        raise HTTPException(status_code=500, detail="SIMLI_API_KEY is missing")

    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    # If saving a draft, use the draft's generated image (not the original persona photo)
    draft_avatar = get_persona_avatar(draft_avatar_id) if draft_avatar_id else None
    draft_image_path = str(draft_avatar["image_path"]) if draft_avatar and draft_avatar.get("image_path") else None
    source_image_path = draft_image_path or str(persona["image_path"])

    await send_avatar_event(client_id, "avatar.requested", name=name, persona_id=persona_id)
    source_local = STATIC_DIR / source_image_path.removeprefix("/static/")
    if not source_local.exists():
        raise HTTPException(status_code=404, detail="Avatar source image file is missing")
    image_bytes = source_local.read_bytes()
    await send_avatar_event(client_id, "api.uploading", name=name, persona_id=persona_id)

    try:
        upload_response = await upload_face_image(
            api_key=settings.simli_api_key,
            image_bytes=image_bytes,
            filename=f"{name}.png",
            face_name=name,
        )
    except SimliError as exc:
        await send_avatar_event(client_id, "avatar.failed", name=name, detail=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    status = normalize_generation_status(upload_response)
    if status in {"processing", "queued", "pending"}:
        face_id = extract_generation_id(upload_response)
        avatar_status = "processing"
    else:
        face_id = extract_face_id(upload_response)
        avatar_status = "ready"

    if not face_id:
        raise HTTPException(status_code=502, detail=f"Simli did not return a usable avatar id: {upload_response}")

    parsed_preset_ids = _parse_preset_ids(preset_ids)
    if draft_avatar_id:
        update_persona_avatar(
            draft_avatar_id,
            face_id=face_id,
            image_path=source_image_path,
            status=avatar_status,
            last_error=None,
        )
        avatar_id = draft_avatar_id
    else:
        avatar_id = insert_persona_avatar(
            {
                "persona_id": persona_id,
                "name": name,
                "decoration": decoration,
                "theme_prompt": theme_prompt,
                "preset_ids": json.dumps(parsed_preset_ids),
                "face_id": face_id,
                "image_path": source_image_path,
                "status": avatar_status,
                "last_error": None,
            }
        )
    avatar = PersonaAvatar(
        id=avatar_id,
        persona_id=persona_id,
        name=name,
        decoration=decoration,
        theme_prompt=theme_prompt,
        preset_ids=parsed_preset_ids,
        face_id=face_id,
        image_path=normalize_image_path(source_image_path),
        status=avatar_status,
        last_error=None,
    )

    if avatar.status == "processing":
        await send_avatar_event(
            client_id,
            "avatar.queued",
            avatar_id=avatar.id,
            persona_id=avatar.persona_id,
            name=avatar.name,
            status="processing",
        )
        asyncio.create_task(poll_avatar_until_ready(avatar.id, client_id))
    else:
        await send_avatar_event(
            client_id,
            "avatar.ready",
            avatar_id=avatar.id,
            persona_id=avatar.persona_id,
            name=avatar.name,
            status="ready",
            face_id=avatar.face_id,
            image_path=avatar.image_path,
        )

    return avatar


@app.post("/api/studio/avatars/preview")
async def post_avatar_preview(
    persona_id: int = Form(...),
    name: str = Form(...),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    custom_prompt: str = Form(default=""),
    client_id: str | None = Form(default=None),
) -> dict:
    """Queue avatar image generation in background. Returns avatar_id immediately."""
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    parsed_preset_ids = _parse_preset_ids(preset_ids)
    full_prompt = build_avatar_edit_prompt(parsed_preset_ids, custom_prompt) or theme_prompt
    avatar_id = insert_persona_avatar(
        {
            "persona_id": persona_id,
            "name": name,
            "decoration": theme_prompt,
            "theme_prompt": full_prompt,
            "preset_ids": json.dumps(parsed_preset_ids),
            "face_id": "",
            "image_path": str(persona["image_path"]),
            "status": "generating",
            "last_error": None,
        }
    )
    asyncio.create_task(
        _run_avatar_image_generation(
            avatar_id=avatar_id,
            persona_id=persona_id,
            persona_image_bytes=read_persona_image_bytes(persona),
            preset_ids=parsed_preset_ids,
            custom_prompt=custom_prompt,
            name=name,
            client_id=client_id,
        )
    )
    await send_avatar_event(client_id, "avatar.generating", avatar_id=avatar_id, persona_id=persona_id, name=name)
    return {"avatar_id": avatar_id}


async def _run_avatar_image_generation(
    *,
    avatar_id: int,
    persona_id: int,
    persona_image_bytes: bytes,
    preset_ids: list[str],
    custom_prompt: str,
    name: str,
    client_id: str | None,
) -> None:
    try:
        prompt = build_avatar_edit_prompt(preset_ids, custom_prompt)
        if prompt:
            edited_bytes = await ai_router.generate_avatar(
                persona_image_bytes,
                prompt_chain=[prompt],
                filename=f"{name}.png",
            )
        else:
            edited_bytes = persona_image_bytes
        preview_ext = ".jpg" if edited_bytes.startswith(b"\xff\xd8\xff") else ".png"
        preview_name = f"preview_{uuid4().hex}{preview_ext}"
        preview_path = UPLOADS_DIR / preview_name
        preview_path.write_bytes(edited_bytes)
        update_persona_avatar(
            avatar_id,
            image_path=f"/static/uploads/{preview_name}",
            status="not_saved",
            last_error=None,
        )
        await send_avatar_event(
            client_id,
            "avatar.preview_ready",
            avatar_id=avatar_id,
            persona_id=persona_id,
            name=name,
        )
    except Exception as exc:
        update_persona_avatar(avatar_id, status="failed", last_error=str(exc))
        await send_avatar_event(
            client_id,
            "avatar.failed",
            avatar_id=avatar_id,
            persona_id=persona_id,
            name=name,
            detail=str(exc),
        )


@app.get("/api/studio/assistants", response_model=list[Assistant])
def get_studio_assistants() -> list[Assistant]:
    return [Assistant(**item) for item in list_assistants()]


@app.post("/api/studio/assistants", response_model=Assistant, status_code=201)
async def post_studio_assistant(payload: AssistantCreate) -> Assistant:
    persona = get_persona_entity(payload.persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    avatar = get_persona_avatar(payload.avatar_id)
    if not avatar or int(avatar["persona_id"]) != payload.persona_id:
        raise HTTPException(status_code=404, detail="Avatar not found for persona")

    avatar = await refresh_avatar_status(avatar)
    if avatar["status"] != "ready":
        raise HTTPException(status_code=409, detail="Avatar is still processing")

    llm_provider = (payload.llm_provider or settings.llm_provider).lower()
    if llm_provider == "gemini":
        llm_model = payload.llm_model or settings.llm_model_gemini
    elif llm_provider == "openai":
        llm_model = payload.llm_model or settings.llm_model_openai
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported llm_provider: {llm_provider}")

    # Inherit the persona's designed/cloned voice if present; else fall back to env defaults.
    persona_voice_id = str(persona.get("voice_id") or "")
    persona_voice_provider = str(persona.get("voice_provider") or "")
    if persona_voice_id and persona_voice_provider:
        voice_provider = persona_voice_provider
        voice_id_val: str | None = persona_voice_id
        voice_model = settings.tts_model if voice_provider == "elevenlabs" else settings.default_simli_voice_model
    else:
        voice_provider = settings.default_simli_voice_provider
        voice_id_val = settings.default_simli_voice_id or None
        voice_model = settings.default_simli_voice_model

    assistant_id = insert_assistant(
        {
            "name": payload.name,
            "prompt": payload.prompt,
            "first_message": payload.first_message,
            "persona_id": payload.persona_id,
            "avatar_id": payload.avatar_id,
            "face_id": avatar["face_id"],
            "simli_agent_id": "",
            "voice_provider": voice_provider,
            "voice_id": voice_id_val,
            "voice_model": voice_model,
            "language": "en",
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "status": "ready",
            "stage": "ready",
            "progress": 100,
        }
    )
    result = Assistant(
        id=assistant_id,
        name=payload.name,
        prompt=payload.prompt,
        first_message=payload.first_message,
        persona_id=payload.persona_id,
        avatar_id=payload.avatar_id,
        face_id=str(avatar["face_id"]),
        simli_agent_id="",
        voice_provider=voice_provider,
        voice_id=voice_id_val,
        voice_model=voice_model,
        language="en",
        llm_provider=llm_provider,
        llm_model=llm_model,
        status="ready",
        stage="ready",
        progress=100,
    )
    await send_avatar_event(payload.client_id, "assistant.ready", assistant_id=assistant_id, name=payload.name)
    return result


@app.post("/api/studio/calls", response_model=AssistantCallResponse)
async def post_studio_call(payload: AssistantCallCreate) -> AssistantCallResponse:
    if not (settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret):
        raise HTTPException(status_code=500, detail="LiveKit credentials are missing")

    assistant = get_assistant(payload.assistant_id)
    if not assistant:
        raise HTTPException(status_code=404, detail="Assistant not found")

    room_name = f"assistant-{assistant['id']}-{uuid4().hex[:8]}"
    identity = f"user-{uuid4().hex[:8]}"

    room_config = build_agent_dispatch_room_config(
        room_name=room_name,
        assistant_id=int(assistant["id"]),
    )

    token = create_join_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        identity=identity,
        room_name=room_name,
        participant_name=f"User {identity[-4:]}",
        room_config=room_config,
    )

    return AssistantCallResponse(
        assistant_id=int(assistant["id"]),
        assistant_name=str(assistant["name"]),
        room_name=room_name,
        livekit_url=settings.livekit_url,
        participant_token=token,
        participant_identity=identity,
    )


# ── Delete endpoints ────────────────────────────────────────────────────────────

@app.get("/api/studio/personas/{persona_id}/cascade-count")
def get_persona_cascade_count(persona_id: int) -> dict:
    avatars = count_avatars_for_persona(persona_id)
    assts = count_assistants_for_persona(persona_id)
    return {"avatars": avatars, "assistants": assts}


@app.patch("/api/studio/personas/{persona_id}", response_model=PersonaEntity)
async def patch_studio_persona(persona_id: int, body: dict) -> PersonaEntity:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    fields: dict = {}
    if "name" in body:
        fields["name"] = str(body["name"])
    if "voice_ref_id" in body:
        vid = body["voice_ref_id"]
        if vid:
            voice = get_voice(int(vid))
            if voice and voice["voice_id"]:
                fields.update({
                    "voice_ref_id": int(vid),
                    "voice_provider": voice["provider"],
                    "voice_id": voice["voice_id"],
                    "voice_source": voice["source"],
                    "voice_description": voice["description"],
                    "voice_sample_path": voice["sample_path"],
                    "voice_preview_path": voice["preview_path"],
                    "voice_status": "ready",
                    "voice_last_error": None,
                })
        else:
            fields.update({
                "voice_ref_id": None,
                "voice_provider": "",
                "voice_id": "",
                "voice_source": "",
                "voice_description": "",
                "voice_sample_path": "",
                "voice_preview_path": "",
                "voice_status": "",
                "voice_last_error": None,
            })
    if fields:
        update_persona_entity(persona_id, **fields)
        # Propagate voice changes to all assistants linked to this persona,
        # otherwise calls keep using the assistant's stale voice_id.
        if "voice_id" in fields:
            new_voice_provider = fields.get("voice_provider") or settings.default_simli_voice_provider
            new_voice_id = fields.get("voice_id") or (settings.default_simli_voice_id or "")
            new_voice_model = (
                settings.tts_model if new_voice_provider == "elevenlabs"
                else settings.default_simli_voice_model
            )
            for asst in list_assistants():
                if asst.get("persona_id") == persona_id:
                    update_assistant(
                        int(asst["id"]),
                        voice_provider=new_voice_provider,
                        voice_id=new_voice_id,
                        voice_model=new_voice_model,
                    )
    updated = get_persona_entity(persona_id)
    return _persona_to_model(updated, [])


@app.delete("/api/studio/personas/{persona_id}", status_code=200)
async def delete_studio_persona(persona_id: int) -> dict:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    # Best-effort: remove the ElevenLabs voice that belongs to this persona.
    voice_id = str(persona.get("voice_id") or "")
    if voice_id and settings.elevenlabs_api_key:
        try:
            await elevenlabs_client.delete_voice(settings.elevenlabs_api_key, voice_id)
        except ElevenLabsError:
            pass
    deleted = delete_persona_entity(persona_id)
    await update_hub.broadcast({"event": "persona.deleted", "persona_id": persona_id})
    return {"deleted": deleted}


# ── Voice customization ─────────────────────────────────────────────────────────

VOICE_PREVIEW_TEXT = (
    "Hello! I am your new avatar assistant. I am excited to chat with you today. "
    "Let me know how I can help, and we can take it from there."
)


async def _reset_persona_voice(persona_id: int) -> None:
    persona = get_persona_entity(persona_id)
    if not persona:
        return
    old_voice_id = str(persona.get("voice_id") or "")
    if old_voice_id and settings.elevenlabs_api_key:
        try:
            await elevenlabs_client.delete_voice(settings.elevenlabs_api_key, old_voice_id)
        except ElevenLabsError:
            pass
    update_persona_entity(
        persona_id,
        voice_provider="",
        voice_id="",
        voice_source="",
        voice_description="",
        voice_sample_path="",
        voice_preview_path="",
        voice_status="",
        voice_last_error=None,
    )


def _write_audio_file(suffix: str, audio_bytes: bytes) -> str:
    filename = f"voice_{uuid4().hex}{suffix}"
    file_path = UPLOADS_DIR / filename
    file_path.write_bytes(audio_bytes)
    return f"/static/uploads/{filename}"


async def _store_voice_preview(api_key: str, voice_id: str) -> str:
    """Call TTS once to cache an MP3 playback preview. Best-effort — returns '' on failure."""
    try:
        mp3 = await elevenlabs_client.synthesize(
            api_key,
            voice_id=voice_id,
            text=VOICE_PREVIEW_TEXT,
            model_id=settings.tts_model,
        )
    except ElevenLabsError:
        return ""
    if not mp3:
        return ""
    return _write_audio_file(".mp3", mp3)


async def _send_voice_event(client_id: str | None, persona_id: int, extra: dict | None = None) -> None:
    row = get_persona_entity(persona_id) or {}
    payload = {
        "persona_id": persona_id,
        "voice_status": row.get("voice_status") or "",
        "voice_provider": row.get("voice_provider") or "",
        "voice_id": row.get("voice_id") or "",
        "voice_source": row.get("voice_source") or "",
        "voice_description": row.get("voice_description") or "",
        "voice_preview_path": row.get("voice_preview_path") or "",
        "voice_last_error": row.get("voice_last_error"),
    }
    if extra:
        payload.update(extra)
    await send_avatar_event(client_id, "persona.voice", **payload)


async def _run_voice_design(
    persona_id: int,
    *,
    user_prompt: str,
    voice_name: str,
    client_id: str | None,
) -> None:
    await _reset_persona_voice(persona_id)
    update_persona_entity(persona_id, voice_status="processing", voice_source="designed", voice_last_error=None)
    await _send_voice_event(client_id, persona_id)

    try:
        persona = get_persona_entity(persona_id)
        if not persona:
            return
        image_bytes = read_persona_image_bytes(persona)

        description = await ai_router.describe_voice(image_bytes, user_prompt=user_prompt)
        description = (description or "").strip()
        if len(description) < 20:
            raise RuntimeError("Voice description was too short — model returned little or nothing.")

        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing — cannot create voice.")

        previews = await elevenlabs_client.create_voice_design_previews(
            settings.elevenlabs_api_key,
            voice_description=description,
            text=VOICE_PREVIEW_TEXT,
        )
        first = previews[0]
        generated_voice_id = str(first.get("generated_voice_id") or "")
        if not generated_voice_id:
            raise RuntimeError(f"Preview had no generated_voice_id: {first}")

        # Save the preview audio immediately so the UI can play it without a second TTS call.
        preview_bytes = elevenlabs_client.decode_preview_audio(first)
        preview_path = _write_audio_file(".mp3", preview_bytes) if preview_bytes else ""

        voice_id = await elevenlabs_client.create_voice_from_preview(
            settings.elevenlabs_api_key,
            name=voice_name or f"{persona.get('name') or 'persona'}-voice",
            description=description[:500],
            generated_voice_id=generated_voice_id,
        )

        update_persona_entity(
            persona_id,
            voice_provider="elevenlabs",
            voice_id=voice_id,
            voice_source="designed",
            voice_description=description,
            voice_preview_path=preview_path,
            voice_status="ready",
            voice_last_error=None,
        )
    except Exception as exc:
        update_persona_entity(
            persona_id,
            voice_status="failed",
            voice_last_error=str(exc),
        )
    finally:
        await _send_voice_event(client_id, persona_id)


async def _run_voice_clone(
    persona_id: int,
    *,
    audio_bytes: bytes,
    sample_filename: str,
    sample_mime: str,
    voice_name: str,
    client_id: str | None,
) -> None:
    await _reset_persona_voice(persona_id)
    update_persona_entity(persona_id, voice_status="processing", voice_source="cloned", voice_last_error=None)
    await _send_voice_event(client_id, persona_id)

    try:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing — cannot create voice.")

        # Persist the uploaded sample for reference/playback.
        suffix = Path(sample_filename).suffix.lower() or ".mp3"
        sample_path_url = _write_audio_file(suffix, audio_bytes)

        voice_id = await elevenlabs_client.add_cloned_voice(
            settings.elevenlabs_api_key,
            name=voice_name or f"persona-{persona_id}-voice",
            description=f"Cloned from uploaded sample for persona {persona_id}",
            audio_bytes=audio_bytes,
            filename=sample_filename,
            mime_type=sample_mime,
        )

        preview_path = await _store_voice_preview(settings.elevenlabs_api_key, voice_id)

        update_persona_entity(
            persona_id,
            voice_provider="elevenlabs",
            voice_id=voice_id,
            voice_source="cloned",
            voice_description="",
            voice_sample_path=sample_path_url,
            voice_preview_path=preview_path,
            voice_status="ready",
            voice_last_error=None,
        )
    except Exception as exc:
        update_persona_entity(
            persona_id,
            voice_status="failed",
            voice_last_error=str(exc),
        )
    finally:
        await _send_voice_event(client_id, persona_id)


@app.post("/api/studio/personas/{persona_id}/voice/design", response_model=PersonaEntity)
async def post_persona_voice_design(persona_id: int, payload: PersonaVoiceDesignRequest) -> PersonaEntity:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    asyncio.create_task(
        _run_voice_design(
            persona_id,
            user_prompt=payload.user_prompt or "",
            voice_name=payload.name or "",
            client_id=payload.client_id,
        )
    )
    return _persona_to_model(
        {**persona, "voice_status": "processing", "voice_source": "designed", "voice_last_error": None},
        [],
    )


@app.post("/api/studio/personas/{persona_id}/voice/clone", response_model=PersonaEntity)
async def post_persona_voice_clone(
    persona_id: int,
    voice_sample: UploadFile = File(...),
    name: str = Form(default=""),
    client_id: str | None = Form(default=None),
) -> PersonaEntity:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")

    audio_bytes = await voice_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Voice sample is empty")
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Voice sample is too large (max 20 MB)")

    mime = voice_sample.content_type or "audio/mpeg"
    filename = voice_sample.filename or f"sample-{uuid4().hex}.mp3"

    asyncio.create_task(
        _run_voice_clone(
            persona_id,
            audio_bytes=audio_bytes,
            sample_filename=filename,
            sample_mime=mime,
            voice_name=name,
            client_id=client_id,
        )
    )
    return _persona_to_model(
        {**persona, "voice_status": "processing", "voice_source": "cloned", "voice_last_error": None},
        [],
    )


@app.delete("/api/studio/personas/{persona_id}/voice", response_model=PersonaEntity)
async def delete_persona_voice(persona_id: int) -> PersonaEntity:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    await _reset_persona_voice(persona_id)
    refreshed = get_persona_entity(persona_id) or persona
    await update_hub.broadcast({"event": "persona.voice", "persona_id": persona_id, "voice_status": ""})
    return _persona_to_model(refreshed, [])


# ── Standalone Voices ──────────────────────────────────────────────────────────

@app.get("/api/studio/voices", response_model=list[VoiceEntity])
def get_studio_voices() -> list[VoiceEntity]:
    return [VoiceEntity(**v) for v in list_voices()]


async def _send_voice_entity_event(client_id: str | None, voice_id: int) -> None:
    row = get_voice(voice_id) or {}
    await send_avatar_event(client_id, "voice.status",
        voice_id=voice_id,
        status=row.get("status") or "",
        voice_id_el=row.get("voice_id") or "",
        preview_path=row.get("preview_path") or "",
        last_error=row.get("last_error"),
    )


async def _run_standalone_voice_design(
    voice_id: int,
    *,
    user_description: str,
    image_bytes: bytes | None,
    voice_name: str,
    client_id: str | None,
    persona_voice_description: str = "",
) -> None:
    try:
        description = user_description.strip()
        if persona_voice_description.strip():
            description = (description + ("; " if description else "") + persona_voice_description.strip())
        update_voice(voice_id, status="processing", description=description, last_error=None)
        await _send_voice_entity_event(client_id, voice_id)
        if len(description) < 20:
            raise RuntimeError("Voice description too short — please describe the voice in more detail.")
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing.")
        el_voice_id = ""
        preview_path = ""
        try:
            previews = await elevenlabs_client.create_voice_design_previews(
                settings.elevenlabs_api_key,
                voice_description=description,
                text=VOICE_PREVIEW_TEXT,
            )
            first = previews[0]
            gen_id = str(first.get("generated_voice_id") or "")
            if not gen_id:
                raise RuntimeError(f"No generated_voice_id returned: {first}")
            preview_bytes = elevenlabs_client.decode_preview_audio(first)
            preview_path = _write_audio_file(".mp3", preview_bytes) if preview_bytes else ""
            el_voice_id = await elevenlabs_client.create_voice_from_preview(
                settings.elevenlabs_api_key,
                name=voice_name or f"voice-{voice_id}",
                description=description[:500],
                generated_voice_id=gen_id,
            )
        except ElevenLabsError as _design_err:
            if _design_err.status_code != 403:
                raise
            # Voice Design requires a higher ElevenLabs plan — fall back to TTS preview
            el_voice_id = settings.tts_voice_id or "EXAVITQu4vr4xnSDxMaL"
            preview_path = await _store_voice_preview(settings.elevenlabs_api_key, el_voice_id)
        update_voice(voice_id,
            voice_id=el_voice_id, source="designed", description=description,
            preview_path=preview_path, status="ready", last_error=None,
        )
    except Exception as exc:
        update_voice(voice_id, status="failed", last_error=str(exc))
    finally:
        await _send_voice_entity_event(client_id, voice_id)
        await update_hub.broadcast({"event": "voice.done", "voice_id": voice_id})


async def _run_standalone_voice_clone(
    voice_id: int,
    *,
    audio_bytes: bytes,
    sample_filename: str,
    sample_mime: str,
    voice_name: str,
    client_id: str | None,
) -> None:
    update_voice(voice_id, status="processing", last_error=None)
    await _send_voice_entity_event(client_id, voice_id)
    try:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing.")
        suffix = Path(sample_filename).suffix.lower() or ".mp3"
        sample_path = _write_audio_file(suffix, audio_bytes)
        el_voice_id = await elevenlabs_client.add_cloned_voice(
            settings.elevenlabs_api_key,
            name=voice_name or f"voice-{voice_id}",
            description=f"Cloned voice",
            audio_bytes=audio_bytes,
            filename=sample_filename,
            mime_type=sample_mime,
        )
        preview_path = await _store_voice_preview(settings.elevenlabs_api_key, el_voice_id)
        update_voice(voice_id,
            voice_id=el_voice_id, source="cloned", sample_path=sample_path,
            preview_path=preview_path, status="ready", last_error=None,
        )
    except Exception as exc:
        update_voice(voice_id, status="failed", last_error=str(exc))
    finally:
        await _send_voice_entity_event(client_id, voice_id)
        await update_hub.broadcast({"event": "voice.done", "voice_id": voice_id})


@app.post("/api/studio/voices/design", response_model=VoiceEntity, status_code=201)
async def post_voice_design(
    name: str = Form(...),
    description: str = Form(default=""),
    persona_id: int | None = Form(default=None),
    include_persona_traits: bool = Form(default=False),
    client_id: str | None = Form(default=None),
) -> VoiceEntity:
    image_bytes: bytes | None = None
    persona_voice_description = ""
    if persona_id:
        persona = get_persona_entity(persona_id)
        if persona:
            try:
                image_bytes = read_persona_image_bytes(persona)
            except Exception:
                pass
            if include_persona_traits:
                persona_voice_description = str(persona.get("voice_description") or "")
    initial_description = description.strip()
    if persona_voice_description.strip():
        initial_description = (
            initial_description
            + ("; " if initial_description else "")
            + persona_voice_description.strip()
        )
    vid = insert_voice({
        "name": name or "New Voice",
        "source": "designed",
        "description": initial_description,
        "status": "processing",
        "persona_id": persona_id,
    })
    asyncio.create_task(_run_standalone_voice_design(
        vid, user_description=description, image_bytes=image_bytes,
        voice_name=name, client_id=client_id,
        persona_voice_description=persona_voice_description,
    ))
    return VoiceEntity(**{**(get_voice(vid) or {}), "status": "processing"})


@app.post("/api/studio/voices/clone", response_model=VoiceEntity, status_code=201)
async def post_voice_clone(
    voice_sample: UploadFile = File(...),
    name: str = Form(default=""),
    client_id: str | None = Form(default=None),
) -> VoiceEntity:
    audio_bytes = await voice_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Voice sample is empty")
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Voice sample is too large (max 20 MB)")
    mime = voice_sample.content_type or "audio/mpeg"
    filename = voice_sample.filename or f"sample-{uuid4().hex}.mp3"
    vid = insert_voice({"name": name or "Cloned Voice", "source": "cloned", "status": "processing"})
    asyncio.create_task(_run_standalone_voice_clone(
        vid, audio_bytes=audio_bytes, sample_filename=filename,
        sample_mime=mime, voice_name=name, client_id=client_id,
    ))
    return VoiceEntity(**{**(get_voice(vid) or {}), "status": "processing"})


@app.post("/api/studio/voices/suggest-description")
async def post_voice_suggest_description(body: dict) -> dict[str, str]:
    persona_id = body.get("persona_id")
    user_hint = str(body.get("user_hint") or "")
    if not persona_id:
        return {"description": ""}
    persona = get_persona_entity(int(persona_id))
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    try:
        image_bytes = read_persona_image_bytes(persona)
        description = await ai_router.describe_voice(image_bytes, user_prompt=user_hint)
        return {"description": (description or "").strip()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


_library_cache: list[dict] = []
_default_preview_path: str = ""


@app.get("/api/studio/voices/library")
async def get_voice_library() -> list[dict]:
    global _library_cache
    if _library_cache:
        return _library_cache
    if not settings.elevenlabs_api_key:
        return []
    try:
        all_voices = await elevenlabs_client.list_voices(settings.elevenlabs_api_key)
        _library_cache = [
            {
                "voice_id": v["voice_id"],
                "name": v["name"],
                "preview_url": v.get("preview_url") or "",
                "category": v.get("category") or "premade",
                "labels": v.get("labels") or {},
            }
            for v in all_voices
            if v.get("category") == "premade"
        ]
    except Exception:
        _library_cache = []
    return _library_cache


@app.post("/api/studio/voices/from-library", response_model=VoiceEntity, status_code=201)
async def post_voice_from_library(body: dict) -> VoiceEntity:
    el_voice_id = str(body.get("voice_id") or "")
    name = str(body.get("name") or "")
    preview_url = str(body.get("preview_url") or "")
    if not el_voice_id:
        raise HTTPException(status_code=400, detail="voice_id required")
    existing = next((v for v in list_voices() if v.get("voice_id") == el_voice_id and v.get("source") == "premade"), None)
    if existing:
        return VoiceEntity(**existing)
    vid = insert_voice({
        "name": name or f"EL {el_voice_id[:8]}",
        "provider": "elevenlabs",
        "voice_id": el_voice_id,
        "source": "premade",
        "description": "",
        "preview_path": preview_url,
        "status": "ready",
    })
    return VoiceEntity(**(get_voice(vid) or {}))


@app.get("/api/studio/voices/preview-default")
async def get_default_voice_preview() -> FileResponse:
    global _default_preview_path
    if _default_preview_path:
        local = STATIC_DIR / _default_preview_path.removeprefix("/static/")
        if local.exists():
            return FileResponse(str(local), media_type="audio/mpeg")
    voice_id = settings.tts_voice_id
    if not voice_id or not settings.elevenlabs_api_key:
        raise HTTPException(status_code=404, detail="No default voice configured")
    mp3 = await elevenlabs_client.synthesize(
        settings.elevenlabs_api_key, voice_id=voice_id,
        text=VOICE_PREVIEW_TEXT, model_id=settings.tts_model,
    )
    _default_preview_path = _write_audio_file(".mp3", mp3)
    return FileResponse(
        str(STATIC_DIR / _default_preview_path.removeprefix("/static/")),
        media_type="audio/mpeg",
    )


@app.delete("/api/studio/voices/{voice_id}", status_code=200)
async def delete_studio_voice(voice_id: int) -> dict:
    voice = get_voice(voice_id)
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")
    el_vid = str(voice.get("voice_id") or "")
    if el_vid and settings.elevenlabs_api_key:
        try:
            await elevenlabs_client.delete_voice(settings.elevenlabs_api_key, el_vid)
        except ElevenLabsError:
            pass
    delete_voice_entity(voice_id)
    await update_hub.broadcast({"event": "voice.deleted", "voice_id": voice_id})
    return {"deleted": 1}


@app.get("/api/studio/avatars/{avatar_id}/cascade-count")
def get_avatar_cascade_count(avatar_id: int) -> dict:
    assts = count_assistants_for_avatar(avatar_id)
    return {"assistants": assts}


@app.delete("/api/studio/avatars/{avatar_id}", status_code=200)
async def delete_studio_avatar(avatar_id: int) -> dict:
    avatar = get_persona_avatar(avatar_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    deleted = delete_persona_avatar(avatar_id)
    await update_hub.broadcast({"event": "avatar.deleted", "avatar_id": avatar_id})
    return {"deleted": deleted}


@app.delete("/api/studio/assistants/{assistant_id}", status_code=200)
async def delete_studio_assistant(assistant_id: int) -> dict:
    asst = get_assistant(assistant_id)
    if not asst:
        raise HTTPException(status_code=404, detail="Assistant not found")
    deleted = delete_assistant_entity(assistant_id)
    await update_hub.broadcast({"event": "assistant.deleted", "assistant_id": assistant_id})
    return {"deleted": deleted}


# ── Cancel endpoints ────────────────────────────────────────────────────────────

@app.post("/api/studio/personas/{persona_id}/cancel")
async def cancel_persona(persona_id: int) -> dict:
    persona = get_persona_entity(persona_id)
    if not persona:
        raise HTTPException(status_code=404, detail="Persona not found")
    update_persona_entity(persona_id, status="cancelled", stage="cancelled")
    await update_hub.broadcast({"event": "persona.cancelled", "persona_id": persona_id})
    return {"status": "cancelled"}


@app.post("/api/studio/avatars/{avatar_id}/cancel")
async def cancel_avatar(avatar_id: int) -> dict:
    avatar = get_persona_avatar(avatar_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    update_persona_avatar(avatar_id, status="cancelled", stage="cancelled")
    await update_hub.broadcast({"event": "avatar.cancelled", "avatar_id": avatar_id})
    return {"status": "cancelled"}


# ── Retry endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/studio/avatars/{avatar_id}/retry")
async def retry_avatar(avatar_id: int, client_id: str | None = None) -> dict:
    avatar = get_persona_avatar(avatar_id)
    if not avatar:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if avatar["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Avatar is not in a retryable state")

    face_id = str(avatar.get("face_id") or "")
    if not face_id:
        persona_id = int(avatar["persona_id"])
        persona = get_persona_entity(persona_id)
        if not persona:
            raise HTTPException(status_code=404, detail="Persona not found")
        update_persona_avatar(avatar_id, status="generating", stage="image_generation", last_error=None)
        asyncio.create_task(
            _run_avatar_image_generation(
                avatar_id=avatar_id,
                persona_id=persona_id,
                persona_image_bytes=read_persona_image_bytes(persona),
                preset_ids=_parse_preset_ids(avatar.get("preset_ids")),
                custom_prompt="",
                name=str(avatar.get("name") or f"avatar-{avatar_id}"),
                client_id=client_id,
            )
        )
        await update_hub.broadcast({"event": "avatar.retrying", "avatar_id": avatar_id})
        return {"status": "retrying"}

    update_persona_avatar(avatar_id, status="processing", stage="queued", last_error=None)
    asyncio.create_task(poll_avatar_until_ready(avatar_id, client_id))
    await update_hub.broadcast({"event": "avatar.retrying", "avatar_id": avatar_id})
    return {"status": "retrying"}


@app.post("/api/studio/voices/{voice_id}/retry")
async def retry_voice(voice_id: int, client_id: str | None = None) -> dict:
    voice = get_voice(voice_id)
    if not voice:
        raise HTTPException(status_code=404, detail="Voice not found")
    if str(voice.get("status") or "") not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Voice is not in a retryable state")
    source = str(voice.get("source") or "designed")
    if source == "cloned":
        sample_path = str(voice.get("sample_path") or "")
        if not sample_path:
            raise HTTPException(status_code=400, detail="Original sample not found — please re-upload.")
        local = STATIC_DIR / sample_path.removeprefix("/static/")
        if not local.exists():
            raise HTTPException(status_code=400, detail="Sample file missing — please re-upload.")
        audio_bytes = local.read_bytes()
        update_voice(voice_id, status="processing", last_error=None)
        asyncio.create_task(_run_standalone_voice_clone(
            voice_id, audio_bytes=audio_bytes, sample_filename=local.name,
            sample_mime="audio/mpeg", voice_name=str(voice.get("name") or f"voice-{voice_id}"),
            client_id=client_id,
        ))
    else:
        description = str(voice.get("description") or "")
        if not description:
            raise HTTPException(status_code=400, detail="No description stored — please recreate the voice.")
        image_bytes: bytes | None = None
        if voice.get("persona_id"):
            try:
                p = get_persona_entity(int(voice["persona_id"]))
                if p:
                    image_bytes = read_persona_image_bytes(p)
            except Exception:
                pass
        update_voice(voice_id, status="processing", last_error=None)
        asyncio.create_task(_run_standalone_voice_design(
            voice_id, user_description=description, image_bytes=image_bytes,
            voice_name=str(voice.get("name") or f"voice-{voice_id}"), client_id=client_id,
        ))
    await update_hub.broadcast({"event": "voice.retrying", "voice_id": voice_id})
    return {"status": "retrying"}


@app.websocket("/ws/updates/{client_id}")
async def updates_ws(websocket: WebSocket, client_id: str) -> None:
    await update_hub.connect(client_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        update_hub.disconnect(client_id, websocket)


def clear_local_uploads() -> int:
    if not UPLOADS_DIR.exists():
        return 0
    removed = 0
    for item in UPLOADS_DIR.iterdir():
        if item.is_file():
            item.unlink(missing_ok=True)
            removed += 1
    return removed


@app.post("/api/simli/cleanup")
async def post_simli_cleanup() -> dict[str, object]:
    if not settings.simli_api_key:
        raise HTTPException(status_code=500, detail="SIMLI_API_KEY is missing")

    deleted_agents = 0
    deleted_faces = 0
    errors: list[str] = []

    try:
        agents = await list_auto_agents(settings.simli_api_key)
    except SimliError as exc:
        agents = []
        errors.append(str(exc))
    for agent in agents:
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        try:
            await delete_auto_agent(settings.simli_api_key, agent_id)
            deleted_agents += 1
        except SimliError as exc:
            errors.append(f"agent {agent_id}: {exc}")

    try:
        faces = await list_faces(settings.simli_api_key)
    except SimliError as exc:
        faces = []
        errors.append(str(exc))
    for face in faces:
        face_id = face.get("id")
        if not isinstance(face_id, str) or not face_id:
            continue
        try:
            await delete_face(settings.simli_api_key, face_id)
            deleted_faces += 1
        except SimliError as exc:
            errors.append(f"face {face_id}: {exc}")

    local_assistants_deleted = delete_all_assistants()
    local_avatars_deleted = delete_all_persona_avatars()
    local_personas_deleted = delete_all_persona_entities()
    local_uploads_deleted = clear_local_uploads()

    return {
        "deleted_agents": deleted_agents,
        "deleted_faces": deleted_faces,
        "local_assistants_deleted": local_assistants_deleted,
        "local_avatars_deleted": local_avatars_deleted,
        "local_personas_deleted": local_personas_deleted,
        "local_uploads_deleted": local_uploads_deleted,
        "errors": errors,
    }


@app.post("/api/simli/session-token")
async def post_simli_session_token(payload: SimliSessionTokenRequest) -> dict:
    if not settings.simli_api_key:
        raise HTTPException(status_code=500, detail="SIMLI_API_KEY is missing")
    try:
        return await create_auto_session_token(
            api_key=settings.simli_api_key,
            create_transcript=payload.create_transcript,
            expiry_stamp=payload.expiry_stamp,
        )
    except SimliError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/livekit/token")
def post_livekit_token(payload: LivekitTokenRequest) -> dict[str, str]:
    if not (settings.livekit_api_key and settings.livekit_api_secret):
        raise HTTPException(status_code=500, detail="LiveKit credentials are missing")

    token = create_join_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        identity=payload.identity,
        room_name=payload.room_name,
        participant_name=payload.participant_name,
    )

    return {
        "token": token,
        "livekit_url": settings.livekit_url,
        "room_name": payload.room_name,
        "identity": payload.identity,
    }
