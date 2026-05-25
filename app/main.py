"""Lili Studio — pure multi-tenant API.

This module exposes the full Studio API surface. Compared to the legacy
single-tenant + bundled-front-end app, the differences are:

  * Every ``/*`` route is tenant-scoped: the caller must supply
    ``tenant_id`` as a JSON body field, multipart form field, or query
    parameter depending on the route shape. The tenancy layer resolves the
    tenant's Postgres database and Azure Blob credentials via Key Vault.
  * All persistent media (persona source images, avatar previews, voice
    samples, voice previews) lives in Azure Blob Storage and is referenced
    by short-lived SAS URLs. No local file IO.
  * The WebSocket hub is gone. Background-task progress is exposed via
    ``GET /{resource}/{id}/status`` for polling.
  * ``POST /calls`` returns a single plug-and-play payload with
    LiveKit credentials AND assistant/avatar/voice metadata.
  * No bundled front-end. The server does not serve HTML or static assets.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import ai_router, elevenlabs_client, repositories as repo, schemas as S
from app.config import settings
from app.elevenlabs_client import ElevenLabsError
from app.livekit_tokens import build_agent_dispatch_room_config, create_join_token
from app.models import Assistant as AssistantRow
from app.models import PersonaAvatar as AvatarRow
from app.models import PersonaEntity as PersonaRow
from app.models import Voice as VoiceRow
from app.simli_client import (
    SimliError,
    delete_face,
    extract_face_id,
    extract_generation_id,
    get_face_generation_status,
    normalize_generation_status,
    upload_face_image,
)
from app.local_storage import LocalBlobStore
from app.tenancy import (
    TenantContext,
    is_local_tenant,
    open_background_context,
    shutdown_tenants,
    tenant_ctx_form,
)


# ── App + lifespan ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await shutdown_tenants()


app = FastAPI(title="Lili Studio API", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── /demo: reference front-end ────────────────────────────────────────────────
#
# A static HTML/JS UI hardcoded to ``tenant_id=local_tenant`` (which routes
# the backend onto its local SQLite + filesystem-blob fallback). Intended
# as a working reference for front-end developers integrating against the
# API — not a production UI.
_DEMO_DIR = Path(__file__).resolve().parent / "demo_static"
if _DEMO_DIR.is_dir():
    app.mount("/demo", StaticFiles(directory=_DEMO_DIR, html=True), name="demo")


# ── Avatar / voice presets ────────────────────────────────────────────────────

AVATAR_PRESETS: dict[str, dict[str, str]] = {
    "casual_outfit":   {"cat": "Outfit",      "label": "Casual",          "gender": "all",    "prompt": "casual modern streetwear"},
    "formal_suit":     {"cat": "Outfit",      "label": "Formal Suit",     "gender": "all",    "prompt": "a formal professional suit"},
    "sporty_wear":     {"cat": "Outfit",      "label": "Sporty",          "gender": "all",    "prompt": "athletic sporty outfit"},
    "glasses":         {"cat": "Accessories", "label": "Glasses",         "gender": "all",    "prompt": "fashionable eyeglasses"},
    "studio_bg":       {"cat": "Background",  "label": "Studio BG",       "gender": "all",    "prompt": "a clean neutral studio background"},
    "office_bg":       {"cat": "Background",  "label": "Office BG",       "gender": "all",    "prompt": "a modern corporate office background"},
    "outdoor_bg":      {"cat": "Background",  "label": "Outdoor BG",      "gender": "all",    "prompt": "a natural outdoor environment background"},
    "library_bg":      {"cat": "Background",  "label": "Library",         "gender": "all",    "prompt": "a warm bookshelf library background"},
    "gym_bg":          {"cat": "Background",  "label": "Gym",             "gender": "all",    "prompt": "a modern gym or fitness studio background"},
    "cityscape_bg":    {"cat": "Background",  "label": "Cityscape",       "gender": "all",    "prompt": "a blurred urban cityscape background"},
    "medical_bg":      {"cat": "Background",  "label": "Medical Office",  "gender": "all",    "prompt": "a clean medical office or clinic background"},
    "gradient_bg":     {"cat": "Background",  "label": "Gradient",        "gender": "all",    "prompt": "a smooth abstract gradient background"},
    "elegant_dress":   {"cat": "Outfit",      "label": "Elegant Dress",   "gender": "female", "prompt": "an elegant sophisticated dress"},
    "biz_blouse":      {"cat": "Outfit",      "label": "Biz Blouse",      "gender": "female", "prompt": "a professional business blouse"},
    "blazer_f":        {"cat": "Outfit",      "label": "Blazer",          "gender": "female", "prompt": "a sharp tailored blazer"},
    "turtleneck_f":    {"cat": "Outfit",      "label": "Turtleneck",      "gender": "female", "prompt": "a fitted turtleneck top"},
    "lab_coat_f":      {"cat": "Outfit",      "label": "Lab Coat",        "gender": "female", "prompt": "a white lab coat over a blouse"},
    "red_lips":        {"cat": "Makeup",      "label": "Red Lips",        "gender": "female", "prompt": "vibrant red lipstick"},
    "smoky_eyes":      {"cat": "Makeup",      "label": "Smoky Eyes",      "gender": "female", "prompt": "dramatic smoky eye makeup"},
    "natural_glow":    {"cat": "Makeup",      "label": "Natural Glow",    "gender": "female", "prompt": "natural glowing skin with minimal makeup"},
    "bold_glam_f":     {"cat": "Makeup",      "label": "Bold Glam",       "gender": "female", "prompt": "bold glamorous full makeup with defined contouring"},
    "no_makeup_f":     {"cat": "Makeup",      "label": "No Makeup",       "gender": "female", "prompt": "bare natural no-makeup clean skin look"},
    "earrings":        {"cat": "Jewelry",     "label": "Earrings",        "gender": "female", "prompt": "elegant drop earrings"},
    "necklace":        {"cat": "Jewelry",     "label": "Necklace",        "gender": "female", "prompt": "a delicate necklace"},
    "bracelet_f":      {"cat": "Jewelry",     "label": "Bracelet",        "gender": "female", "prompt": "a delicate bracelet on the wrist"},
    "updo_hair":       {"cat": "Hair",        "label": "Updo",            "gender": "female", "prompt": "an elegant updo hairstyle"},
    "braids":          {"cat": "Hair",        "label": "Braids",          "gender": "female", "prompt": "beautifully braided hair"},
    "long_flowing_f":  {"cat": "Hair",        "label": "Long Flowing",    "gender": "female", "prompt": "long flowing straight hair"},
    "bob_cut_f":       {"cat": "Hair",        "label": "Bob Cut",         "gender": "female", "prompt": "a sleek chin-length bob cut"},
    "ponytail_f":      {"cat": "Hair",        "label": "Ponytail",        "gender": "female", "prompt": "a neat high ponytail"},
    "curly_natural_f": {"cat": "Hair",        "label": "Curly Natural",   "gender": "female", "prompt": "natural voluminous curly hair"},
    "sunglasses_f":    {"cat": "Accessories", "label": "Sunglasses",      "gender": "female", "prompt": "stylish oversized sunglasses"},
    "biz_suit_m":      {"cat": "Outfit",      "label": "Business Suit",   "gender": "male",   "prompt": "a sharp tailored business suit with tie"},
    "leather_jkt":     {"cat": "Outfit",      "label": "Leather Jacket",  "gender": "male",   "prompt": "a cool leather jacket"},
    "hoodie_m":        {"cat": "Outfit",      "label": "Hoodie",          "gender": "male",   "prompt": "a casual hoodie and jeans"},
    "polo_smart_m":    {"cat": "Outfit",      "label": "Smart Casual",    "gender": "male",   "prompt": "a smart casual polo shirt"},
    "tuxedo_m":        {"cat": "Outfit",      "label": "Tuxedo",          "gender": "male",   "prompt": "a black tie tuxedo"},
    "lab_coat_m":      {"cat": "Outfit",      "label": "Lab Coat",        "gender": "male",   "prompt": "a white lab coat over a collared shirt"},
    "watch_acc":       {"cat": "Accessories", "label": "Watch",           "gender": "male",   "prompt": "a luxury wristwatch"},
    "cap_m":           {"cat": "Accessories", "label": "Cap",             "gender": "male",   "prompt": "a stylish baseball cap"},
    "sunglasses_m":    {"cat": "Accessories", "label": "Sunglasses",      "gender": "male",   "prompt": "sleek stylish sunglasses"},
    "beard_stubble":   {"cat": "Facial Hair", "label": "Stubble",         "gender": "male",   "prompt": "a short stylish beard stubble"},
    "full_beard":      {"cat": "Facial Hair", "label": "Full Beard",      "gender": "male",   "prompt": "a well-groomed full beard"},
    "clean_shave":     {"cat": "Facial Hair", "label": "Clean Shave",     "gender": "male",   "prompt": "clean shaven smooth skin"},
    "slick_back_m":    {"cat": "Hair",        "label": "Slick Back",      "gender": "male",   "prompt": "slicked-back hair"},
    "crew_cut_m":      {"cat": "Hair",        "label": "Crew Cut",        "gender": "male",   "prompt": "a short neat crew cut"},
    "curly_natural_m": {"cat": "Hair",        "label": "Curly Natural",   "gender": "male",   "prompt": "natural curly hair"},
}

VOICE_PRESETS: dict[str, dict[str, str]] = {
    "warm_professional":  {"label": "Warm Professional",    "desc": "warm, confident, professional, clear diction, approachable"},
    "energetic":          {"label": "Energetic",            "desc": "energetic, upbeat, enthusiastic, bright, youthful"},
    "calm_soothing":      {"label": "Calm & Soothing",      "desc": "calm, gentle, soothing, soft, reassuring"},
    "deep_authoritative": {"label": "Deep & Authoritative", "desc": "deep, resonant, authoritative, commanding, mature baritone"},
    "bright_friendly":    {"label": "Bright & Friendly",    "desc": "bright, friendly, cheerful, light, welcoming, conversational"},
    "news_anchor":        {"label": "News Anchor",          "desc": "crisp, neutral, clear diction, professional broadcaster, measured pace"},
    "storyteller":        {"label": "Storyteller",          "desc": "expressive, dramatic, captivating storyteller, rich vocal range"},
    "tech_presenter":     {"label": "Tech Presenter",       "desc": "clear, intelligent, modern, precise, confident, tech-savvy"},
    "deep_narrator_m":    {"label": "Deep Narrator",        "desc": "deep, rich baritone, slow deliberate pace, resonant, cinematic narrator"},
    "friendly_sales_m":   {"label": "Friendly Sales",       "desc": "upbeat, persuasive, warm, enthusiastic, relatable, conversational"},
    "calm_therapist_m":   {"label": "Calm Therapist",       "desc": "calm, measured, empathetic, gentle, thoughtful, non-judgmental"},
    "wise_elder_m":       {"label": "Wise Elder",           "desc": "slow, thoughtful, warm, slightly gravelly, wise, seasoned storyteller"},
    "exec_formal_m":      {"label": "Executive",            "desc": "formal, commanding, polished, composed, authoritative, boardroom tone"},
    "confident_leader_f": {"label": "Confident Leader",     "desc": "confident, direct, clear, inspiring, composed, professional female voice"},
    "warm_teacher_f":     {"label": "Warm Teacher",         "desc": "patient, warm, encouraging, clear articulation, nurturing, approachable"},
    "fitness_coach_f":    {"label": "Fitness Coach",        "desc": "energetic, motivating, upbeat, strong, positive, high-energy female voice"},
    "meditation_guide_f": {"label": "Meditation Guide",     "desc": "soft, slow, breathy, deeply soothing, calming, mindfulness guide tone"},
    "customer_service_f": {"label": "Customer Service",     "desc": "helpful, friendly, patient, cheerful, clear, polite service representative"},
    "healthcare_pro":     {"label": "Healthcare",           "desc": "calm, precise, reassuring, clinical, professional, clear medical tone"},
    "legal_formal":       {"label": "Legal",                "desc": "formal, measured, authoritative, precise diction, serious, trustworthy"},
    "finance_trust":      {"label": "Finance",              "desc": "confident, composed, trustworthy, professional, measured, credible"},
    "edu_engaging":       {"label": "Educator",             "desc": "clear, patient, engaging, articulate, encouraging, instructional tone"},
    "ai_assistant":       {"label": "AI Assistant",         "desc": "neutral, clear, helpful, modern, conversational, natural synthetic voice"},
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
    has_background = "Background" in grouped
    return (
        "Edit the provided portrait with precision.\n\n"
        "IDENTITY LOCK — the following must remain absolutely identical to the source image:\n"
        "- Face shape, eyes, nose, mouth, chin, ears, and expression\n"
        "- Skin tone, complexion, and texture\n"
        "- Age appearance, bone structure, and facial proportions\n"
        "- Ethnic and cultural presentation\n"
        + ("" if has_background else "- Background and environment (preserve unchanged)\n")
        + "\nOnly apply the changes listed below. Everything else stays identical.\n\n"
        f"Changes to apply:\n{body}\n\n"
        "- Photorealistic output only — no illustration, painting, or cartoon style\n"
        "- Maintain natural lighting consistent with the source\n"
        "- Keep the original portrait framing and aspect ratio"
    )


VOICE_PREVIEW_TEXT = (
    "Hello there! I'm your AI avatar assistant — great to meet you. "
    "I'm here to help with questions, conversations, or whatever you need. "
    "Just let me know where you'd like to start. What can I do for you today?"
)


# ── Tiny utility helpers ──────────────────────────────────────────────────────

def _parse_preset_ids(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(str(raw or "[]"))
        return [str(x) for x in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _validate_image_upload(upload: UploadFile, image_bytes: bytes) -> None:
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Image is required")
    if upload.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Use a PNG, JPG, or WEBP image")


def _ext_for_image_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"RIFF") and b"WEBP" in data[:32]:
        return ".webp"
    return ".png"


def _content_type_for_ext(ext: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
    }.get(ext.lower(), "application/octet-stream")


async def _fetch_url_bytes(url: str) -> bytes:
    """HTTP-GET a SAS URL (or any URL) and return the bytes."""
    if not url:
        raise HTTPException(status_code=404, detail="Asset URL is empty")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch blob (status {resp.status_code})",
            )
        return resp.content


# ── SQLAlchemy → Pydantic serialisation ───────────────────────────────────────

def _avatar_row_to_model(row: AvatarRow) -> S.PersonaAvatar:
    return S.PersonaAvatar(
        id=row.id,
        persona_id=row.persona_id,
        name=row.name,
        decoration=row.decoration,
        theme_prompt=row.theme_prompt,
        preset_ids=_parse_preset_ids(row.preset_ids),
        face_id=row.face_id,
        image_url=row.image_url,
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
    )


def _persona_row_to_model(
    row: PersonaRow, avatars: list[S.PersonaAvatar]
) -> S.PersonaEntity:
    return S.PersonaEntity(
        id=row.id,
        name=row.name,
        image_url=row.image_url,
        gender=row.gender,
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
        voice_provider=row.voice_provider,
        voice_id=row.voice_id,
        voice_source=row.voice_source,
        voice_description=row.voice_description,
        voice_sample_url=row.voice_sample_url,
        voice_preview_url=row.voice_preview_url,
        voice_status=row.voice_status,
        voice_last_error=row.voice_last_error,
        avatars=avatars,
    )


def _voice_row_to_model(row: VoiceRow) -> S.VoiceEntity:
    return S.VoiceEntity(
        id=row.id,
        name=row.name,
        provider=row.provider,
        voice_id=row.voice_id,
        source=row.source,
        description=row.description,
        sample_url=row.sample_url,
        preview_url=row.preview_url,
        persona_id=row.persona_id,
        status=row.status,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _assistant_row_to_model(row: AssistantRow) -> S.Assistant:
    return S.Assistant(
        id=row.id,
        name=row.name,
        prompt=row.prompt,
        first_message=row.first_message,
        persona_id=row.persona_id,
        avatar_id=row.avatar_id,
        face_id=row.face_id,
        simli_agent_id=row.simli_agent_id,
        voice_provider=row.voice_provider,
        voice_id=row.voice_id,
        voice_model=row.voice_model,
        language=row.language,
        llm_provider=row.llm_provider,
        llm_model=row.llm_model,
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
    )


# ── Blob upload helpers ───────────────────────────────────────────────────────

async def _upload_image(
    ctx: TenantContext, key: str, image_bytes: bytes, *, ext: str | None = None
) -> str:
    chosen_ext = ext or _ext_for_image_bytes(image_bytes)
    full_key = f"{key}{chosen_ext}"
    result = await ctx.blob.upload_bytes(
        full_key, image_bytes, content_type=_content_type_for_ext(chosen_ext)
    )
    return result.url


async def _upload_audio(
    ctx: TenantContext, key: str, audio_bytes: bytes, *, ext: str = ".mp3"
) -> str:
    full_key = f"{key}{ext}"
    result = await ctx.blob.upload_bytes(
        full_key, audio_bytes, content_type=_content_type_for_ext(ext)
    )
    return result.url


def _key_from_blob_url(ctx: TenantContext, url: str) -> str | None:
    """Recover the blob key from a URL we previously stamped into a DB row.

    Handles both backends:

    * **Local fallback** — URLs look like
      ``http://host/local-blob/{tenant_id}/{key}``.
    * **Azure** — URLs look like
      ``https://{account}.blob.core.windows.net/{container}/{key}``,
      which we reconstruct from the tenant's secrets.

    Returns ``None`` when the URL doesn't match either pattern (defensive —
    e.g. an externally-hosted preview URL stored on a row).
    """
    if not url:
        return None
    marker = f"/local-blob/{ctx.tenant_id}/"
    if marker in url:
        return url.split(marker, 1)[1].split("?", 1)[0]
    if ctx.secrets is not None:
        prefix = (
            f"{ctx.secrets.blob_account_url.rstrip('/')}/"
            f"{ctx.secrets.blob_container}/"
        )
        if url.startswith(prefix):
            return url[len(prefix):].split("?", 1)[0]
    return None


async def _delete_blob_by_url(ctx: TenantContext, url: str) -> None:
    """Best-effort blob delete. No-op if the URL isn't a tenant blob."""
    key = _key_from_blob_url(ctx, url)
    if not key:
        return
    try:
        await ctx.blob.delete(key)
    except Exception:
        pass


# ── Healthchecks & utility routes ─────────────────────────────────────────────

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Local fallback: serves blobs written by LocalBlobStore ────────────────────
#
# Browser-side consumers reach files via `/local-blob/{tenant_id}/{key}`. This
# route only resolves for tenants in local-fallback mode; for remote tenants
# the URLs returned by the API point at Azure directly.

@app.get("/local-blob/{tenant_id}/{key:path}")
def serve_local_blob(tenant_id: str, key: str) -> Response:
    if not is_local_tenant(tenant_id):
        raise HTTPException(status_code=404, detail="Not a local tenant")
    store = LocalBlobStore(tenant_id=tenant_id)
    try:
        data, content_type = store.read_local(key)
    except (FileNotFoundError, ValueError):
        raise HTTPException(status_code=404, detail="Blob not found")
    return Response(content=data, media_type=content_type)


@app.get("/config")
def get_config() -> dict[str, object]:
    return {
        "livekit_url": settings.livekit_url,
        "has_livekit_creds": bool(settings.livekit_api_key and settings.livekit_api_secret),
        "has_simli_api_key": bool(settings.simli_api_key),
        "has_keyvault_creds": all(
            [
                settings.azure_keyvault_url,
                settings.azure_client_id,
                settings.azure_ad_tenant_id,
                settings.azure_client_secret,
            ]
        ),
    }


@app.get("/presets")
def get_studio_presets() -> dict[str, list[dict]]:
    avatar_presets = [
        {"id": pid, "label": meta["label"], "cat": meta["cat"], "gender": meta["gender"]}
        for pid, meta in AVATAR_PRESETS.items()
    ]
    voice_presets = [{"id": pid, "label": meta["label"]} for pid, meta in VOICE_PRESETS.items()]
    return {"avatar_presets": avatar_presets, "voice_presets": voice_presets}


# ── Avatar status refresh (Simli polling) ─────────────────────────────────────

async def _refresh_avatar_status(ctx: TenantContext, avatar: AvatarRow) -> AvatarRow:
    """If the avatar is still processing on Simli, query Simli and update the row."""
    if avatar.status != "processing":
        return avatar
    try:
        status_response = await get_face_generation_status(
            settings.simli_api_key, avatar.face_id
        )
    except SimliError as exc:
        avatar.last_error = str(exc)
        await ctx.session.flush()
        return avatar

    status = normalize_generation_status(status_response)
    ready_face_id = extract_face_id(status_response) or avatar.face_id
    if status in {"processing", "queued", "pending"}:
        avatar.status = "processing"
    elif status in {"failed", "error"}:
        avatar.status = "failed"
        avatar.last_error = str(status_response)
    else:
        avatar.status = "ready"
        avatar.face_id = ready_face_id
        avatar.last_error = None
    await ctx.session.flush()
    return avatar


async def _poll_avatar_until_ready(tenant_id: str, avatar_id: int) -> None:
    """Background poller: hits Simli every 7s until the face generation finishes."""
    while True:
        try:
            async with open_background_context(tenant_id) as ctx:
                avatar = await repo.get_persona_avatar(ctx.session, avatar_id)
                if avatar is None:
                    return
                if avatar.status == "cancelled":
                    return
                avatar = await _refresh_avatar_status(ctx, avatar)
                if avatar.status != "processing":
                    return
        except Exception:
            # Transient failure — back off and retry.
            pass
        await asyncio.sleep(7)


# ── Personas ──────────────────────────────────────────────────────────────────

@app.post("/personas/list", response_model=list[S.PersonaEntity])
async def list_personas(
    payload: S.TenantScoped,
    gender: str | None = Query(default=None),
    status: str | None = Query(default=None),
    voice_status: str | None = Query(default=None),
    voice_provider: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.PersonaEntity]:
    async with open_background_context(payload.tenant_id) as ctx:
        personas = await repo.list_persona_entities(
            ctx.session,
            gender=gender,
            status=status,
            voice_status=voice_status,
            voice_provider=voice_provider,
            limit=limit,
            offset=offset,
        )
        persona_ids = {p.id for p in personas}
        avatars = await repo.list_persona_avatars(
            ctx.session, persona_id=None
        )
        for av in avatars:
            if av.status == "processing":
                await _refresh_avatar_status(ctx, av)
        by_persona: dict[int, list[S.PersonaAvatar]] = {}
        for av in avatars:
            if av.persona_id in persona_ids:
                by_persona.setdefault(av.persona_id, []).append(_avatar_row_to_model(av))
        return [_persona_row_to_model(p, by_persona.get(p.id, [])) for p in personas]


@app.post("/personas/{persona_id}/get", response_model=S.PersonaEntity)
async def get_persona(persona_id: int, payload: S.TenantScoped) -> S.PersonaEntity:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        avatars = await repo.list_persona_avatars(ctx.session, persona_id=persona_id)
        for av in avatars:
            if av.status == "processing":
                await _refresh_avatar_status(ctx, av)
        return _persona_row_to_model(persona, [_avatar_row_to_model(av) for av in avatars])


@app.post(
    "/personas", response_model=S.PersonaEntity, status_code=201
)
async def create_persona(
    persona_image: UploadFile = File(...),
    name: str = Form("Persona"),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> S.PersonaEntity:
    image_bytes = await persona_image.read()
    upload_filename = persona_image.filename or "persona.png"
    upload_content_type = persona_image.content_type
    # Re-construct a thin shim so the validator sees the same shape it expects.
    class _Shim:
        def __init__(self, content_type: str | None) -> None:
            self.content_type = content_type
    _validate_image_upload(_Shim(upload_content_type), image_bytes)  # type: ignore[arg-type]

    async with open_background_context(tenant_id) as ctx:
        row = await repo.insert_persona_entity(
            ctx.session,
            name=name,
            image_url="",
            gender="unknown",
            status="processing",
            stage="detecting_gender",
        )
        persona_id = row.id
        image_url = await _upload_image(
            ctx,
            f"personas/{persona_id}/source",
            image_bytes,
            ext=os.path.splitext(upload_filename)[1].lower() or None,
        )
        await repo.update_persona_entity(
            ctx.session, persona_id, image_url=image_url
        )

    asyncio.create_task(
        _process_new_persona(tenant_id, persona_id, image_bytes)
    )

    return S.PersonaEntity(
        id=persona_id,
        name=name,
        image_url=image_url,
        gender="unknown",
        status="processing",
        stage="detecting_gender",
        avatars=[],
    )


async def _process_new_persona(
    tenant_id: str, persona_id: int, image_bytes: bytes
) -> None:
    gender = "unknown"
    voice_description = ""
    try:
        gender = await ai_router.detect_gender(image_bytes)
    except Exception:
        gender = "unknown"
    try:
        voice_description = (
            await ai_router.describe_voice(image_bytes, user_prompt="") or ""
        ).strip()
    except Exception:
        voice_description = ""
    async with open_background_context(tenant_id) as ctx:
        await repo.update_persona_entity(
            ctx.session,
            persona_id,
            gender=gender,
            voice_description=voice_description,
            status="ready",
            stage="ready",
            progress=100,
        )


@app.post("/personas/{persona_id}/status", response_model=S.StatusResponse)
async def persona_status(
    persona_id: int, payload: S.TenantScoped
) -> S.StatusResponse:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_persona_entity(ctx.session, persona_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        return S.StatusResponse(
            status=row.status,
            stage=row.stage,
            progress=row.progress,
            last_error=row.last_error,
        )


@app.post("/personas/{persona_id}/cascade-count")
async def persona_cascade_count(
    persona_id: int, payload: S.TenantScoped
) -> dict[str, int]:
    async with open_background_context(payload.tenant_id) as ctx:
        return {
            "avatars": await repo.count_avatars_for_persona(ctx.session, persona_id),
            "assistants": await repo.count_assistants_for_persona(ctx.session, persona_id),
        }


class PersonaPatchRequest(S.TenantScoped):
    name: str | None = None
    voice_ref_id: int | None = Field(default=None)


@app.patch("/personas/{persona_id}", response_model=S.PersonaEntity)
async def patch_persona(
    persona_id: int, payload: PersonaPatchRequest
) -> S.PersonaEntity:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        fields: dict[str, Any] = {}
        if payload.name is not None:
            fields["name"] = payload.name
        if payload.voice_ref_id is not None:
            if payload.voice_ref_id == 0:
                fields.update(
                    voice_ref_id=None,
                    voice_provider="",
                    voice_id="",
                    voice_source="",
                    voice_description="",
                    voice_sample_url="",
                    voice_preview_url="",
                    voice_status="",
                    voice_last_error=None,
                )
            else:
                voice = await repo.get_voice(ctx.session, payload.voice_ref_id)
                if voice and voice.voice_id:
                    fields.update(
                        voice_ref_id=voice.id,
                        voice_provider=voice.provider,
                        voice_id=voice.voice_id,
                        voice_source=voice.source,
                        voice_description=voice.description,
                        voice_sample_url=voice.sample_url,
                        voice_preview_url=voice.preview_url,
                        voice_status="ready",
                        voice_last_error=None,
                    )
        if fields:
            await repo.update_persona_entity(ctx.session, persona_id, **fields)
            if "voice_id" in fields:
                new_provider = fields.get("voice_provider") or settings.default_simli_voice_provider
                new_voice_id = fields.get("voice_id") or (settings.default_simli_voice_id or "")
                new_voice_model = (
                    settings.tts_model
                    if new_provider == "elevenlabs"
                    else settings.default_simli_voice_model
                )
                # Propagate to all assistants for this persona.
                for asst in await repo.list_assistants(ctx.session):
                    if asst.persona_id == persona_id:
                        await repo.update_assistant(
                            ctx.session,
                            asst.id,
                            voice_provider=new_provider,
                            voice_id=new_voice_id,
                            voice_model=new_voice_model,
                        )
        updated = await repo.get_persona_entity(ctx.session, persona_id)
        assert updated is not None
        return _persona_row_to_model(updated, [])


@app.delete("/personas/{persona_id}", status_code=200)
async def delete_persona(persona_id: int, payload: S.TenantScoped) -> dict[str, int]:
    """Delete a persona row + its source image blob + any owned ElevenLabs
    voice. Avatars and assistants that referenced this persona survive; the
    FK ``ON DELETE SET NULL`` nulls their ``persona_id``."""
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")

        # Blob: the persona's source image.
        if persona.image_url:
            await _delete_blob_by_url(ctx, persona.image_url)
        # Blob: any sample / preview audio owned directly by the persona.
        if persona.voice_sample_url:
            await _delete_blob_by_url(ctx, persona.voice_sample_url)
        if persona.voice_preview_url:
            await _delete_blob_by_url(ctx, persona.voice_preview_url)

        # External: the ElevenLabs voice the persona owned (if any).
        if persona.voice_id and settings.elevenlabs_api_key:
            try:
                await elevenlabs_client.delete_voice(
                    settings.elevenlabs_api_key, persona.voice_id
                )
            except ElevenLabsError:
                pass

        deleted = await repo.delete_persona_entity(ctx.session, persona_id)
        return {"deleted": deleted}


@app.post("/personas/{persona_id}/cancel")
async def cancel_persona(persona_id: int, payload: S.TenantScoped) -> dict[str, str]:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        await repo.update_persona_entity(
            ctx.session, persona_id, status="cancelled", stage="cancelled"
        )
        return {"status": "cancelled"}


# ── Persona voice (design / clone / clear) ────────────────────────────────────

async def _reset_persona_voice(ctx: TenantContext, persona_id: int) -> None:
    """Clear the voice attached to a persona — drops the ElevenLabs voice,
    the sample/preview blobs, and resets the persona's voice columns.

    Used by ``DELETE /personas/{id}/voice`` and as a cleanup step inside
    persona voice redesign / reclone."""
    persona = await repo.get_persona_entity(ctx.session, persona_id)
    if persona is None:
        return

    if persona.voice_sample_url:
        await _delete_blob_by_url(ctx, persona.voice_sample_url)
    if persona.voice_preview_url:
        await _delete_blob_by_url(ctx, persona.voice_preview_url)

    if persona.voice_id and settings.elevenlabs_api_key:
        try:
            await elevenlabs_client.delete_voice(
                settings.elevenlabs_api_key, persona.voice_id
            )
        except ElevenLabsError:
            pass

    await repo.update_persona_entity(
        ctx.session,
        persona_id,
        voice_provider="",
        voice_id="",
        voice_source="",
        voice_description="",
        voice_sample_url="",
        voice_preview_url="",
        voice_status="",
        voice_last_error=None,
    )


async def _store_voice_preview(api_key: str, voice_id: str) -> bytes | None:
    try:
        mp3 = await elevenlabs_client.synthesize(
            api_key,
            voice_id=voice_id,
            text=VOICE_PREVIEW_TEXT,
            model_id=settings.tts_model,
        )
    except ElevenLabsError:
        return None
    return mp3 or None


@app.post(
    "/personas/{persona_id}/voice/design",
    response_model=S.PersonaEntity,
)
async def design_persona_voice(
    persona_id: int, payload: S.PersonaVoiceDesignRequest
) -> S.PersonaEntity:
    requested_gender = _normalize_gender(payload.gender) if payload.gender else ""
    if payload.gender and requested_gender not in {"male", "female"}:
        raise HTTPException(
            status_code=400,
            detail="gender must be 'male' or 'female' when supplied",
        )

    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        snapshot = _persona_row_to_model(persona, []).model_copy(
            update=dict(
                voice_status="processing",
                voice_source="designed",
                voice_last_error=None,
            )
        )

    asyncio.create_task(
        _run_persona_voice_design(
            payload.tenant_id,
            persona_id,
            user_prompt=payload.user_prompt or "",
            voice_name=payload.name or "",
            gender_override=requested_gender,
        )
    )
    return snapshot


async def _run_persona_voice_design(
    tenant_id: str,
    persona_id: int,
    *,
    user_prompt: str,
    voice_name: str,
    gender_override: str = "",
) -> None:
    async with open_background_context(tenant_id) as ctx:
        await _reset_persona_voice(ctx, persona_id)
        await repo.update_persona_entity(
            ctx.session,
            persona_id,
            voice_status="processing",
            voice_source="designed",
            voice_last_error=None,
        )
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            return
        image_url = persona.image_url
        persona_gender = persona.gender or "unknown"

    # Persona-bound flow: persona's detected gender is the authoritative
    # source. Override only applies when persona.gender is still 'unknown'.
    if persona_gender in {"male", "female"}:
        effective_gender = persona_gender
    else:
        effective_gender = gender_override or "unknown"

    try:
        image_bytes = await _fetch_url_bytes(image_url)
        description = (
            await ai_router.describe_voice(image_bytes, user_prompt=user_prompt) or ""
        ).strip()
        if len(description) < 20:
            raise RuntimeError(
                "Voice description was too short — model returned little or nothing."
            )
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing — cannot create voice.")

        prompt_description = description + _gender_prompt_suffix(effective_gender)
        previews = await elevenlabs_client.create_voice_design_previews(
            settings.elevenlabs_api_key,
            voice_description=prompt_description,
            text=VOICE_PREVIEW_TEXT,
        )
        first = previews[0]
        generated_voice_id = str(first.get("generated_voice_id") or "")
        if not generated_voice_id:
            raise RuntimeError(f"Preview had no generated_voice_id: {first}")
        preview_bytes = elevenlabs_client.decode_preview_audio(first)

        voice_id = await elevenlabs_client.create_voice_from_preview(
            settings.elevenlabs_api_key,
            name=voice_name or f"persona-{persona_id}-voice",
            description=description[:500],
            generated_voice_id=generated_voice_id,
            labels=_labels_for_gender(effective_gender),
        )

        async with open_background_context(tenant_id) as ctx:
            preview_url = ""
            if preview_bytes:
                preview_url = await _upload_audio(
                    ctx, f"personas/{persona_id}/voice-preview", preview_bytes
                )
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_provider="elevenlabs",
                voice_id=voice_id,
                voice_source="designed",
                voice_description=description,
                voice_preview_url=preview_url,
                voice_status="ready",
                voice_last_error=None,
            )
    except Exception as exc:
        async with open_background_context(tenant_id) as ctx:
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_status="failed",
                voice_last_error=str(exc),
            )


@app.post(
    "/personas/{persona_id}/voice/clone",
    response_model=S.PersonaEntity,
)
async def clone_persona_voice(
    persona_id: int,
    voice_sample: UploadFile = File(...),
    name: str = Form(default=""),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> S.PersonaEntity:
    audio_bytes = await voice_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Voice sample is empty")
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=400, detail="Voice sample is too large (max 20 MB)"
        )
    mime = voice_sample.content_type or "audio/mpeg"
    filename = voice_sample.filename or f"sample-{uuid4().hex}.mp3"

    async with open_background_context(tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        await repo.update_persona_entity(
            ctx.session,
            persona_id,
            voice_status="processing",
            voice_source="cloned",
            voice_last_error=None,
        )
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        assert persona is not None
        snapshot = _persona_row_to_model(persona, [])

    asyncio.create_task(
        _run_persona_voice_clone(
            tenant_id,
            persona_id,
            audio_bytes=audio_bytes,
            sample_filename=filename,
            sample_mime=mime,
            voice_name=name,
        )
    )
    return snapshot


async def _run_persona_voice_clone(
    tenant_id: str,
    persona_id: int,
    *,
    audio_bytes: bytes,
    sample_filename: str,
    sample_mime: str,
    voice_name: str,
) -> None:
    sample_ext = os.path.splitext(sample_filename)[1].lower() or ".mp3"
    try:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing — cannot create voice.")

        async with open_background_context(tenant_id) as ctx:
            await _reset_persona_voice(ctx, persona_id)
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_status="processing",
                voice_source="cloned",
                voice_last_error=None,
            )
            persona = await repo.get_persona_entity(ctx.session, persona_id)
            persona_gender = (persona.gender if persona else "") or "unknown"
            sample_url = await _upload_audio(
                ctx, f"personas/{persona_id}/voice-sample", audio_bytes, ext=sample_ext
            )

        voice_id = await elevenlabs_client.add_cloned_voice(
            settings.elevenlabs_api_key,
            name=voice_name or f"persona-{persona_id}-voice",
            description=f"Cloned from uploaded sample for persona {persona_id}",
            audio_bytes=audio_bytes,
            filename=sample_filename,
            mime_type=sample_mime,
            labels=_labels_for_gender(persona_gender),
        )
        preview_bytes = await _store_voice_preview(
            settings.elevenlabs_api_key, voice_id
        )

        async with open_background_context(tenant_id) as ctx:
            preview_url = ""
            if preview_bytes:
                preview_url = await _upload_audio(
                    ctx, f"personas/{persona_id}/voice-preview", preview_bytes
                )
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_provider="elevenlabs",
                voice_id=voice_id,
                voice_source="cloned",
                voice_description="",
                voice_sample_url=sample_url,
                voice_preview_url=preview_url,
                voice_status="ready",
                voice_last_error=None,
            )
    except Exception as exc:
        async with open_background_context(tenant_id) as ctx:
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_status="failed",
                voice_last_error=str(exc),
            )


@app.delete("/personas/{persona_id}/voice", response_model=S.PersonaEntity)
async def delete_persona_voice(
    persona_id: int, payload: S.TenantScoped
) -> S.PersonaEntity:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        await _reset_persona_voice(ctx, persona_id)
        refreshed = await repo.get_persona_entity(ctx.session, persona_id)
        assert refreshed is not None
        return _persona_row_to_model(refreshed, [])


# ── Avatars ───────────────────────────────────────────────────────────────────

@app.post("/avatars/list", response_model=list[S.PersonaAvatar])
async def list_avatars(
    payload: S.TenantScoped,
    persona_id: int | None = Query(default=None),
    gender: str | None = Query(default=None),
    voice_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.PersonaAvatar]:
    async with open_background_context(payload.tenant_id) as ctx:
        avatars = await repo.list_persona_avatars(
            ctx.session,
            persona_id=persona_id,
            gender=gender,
            voice_id=voice_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        for av in avatars:
            if av.status == "processing":
                await _refresh_avatar_status(ctx, av)
        return [_avatar_row_to_model(av) for av in avatars]


@app.post("/avatars/{avatar_id}/get", response_model=S.PersonaAvatar)
async def get_avatar(avatar_id: int, payload: S.TenantScoped) -> S.PersonaAvatar:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_persona_avatar(ctx.session, avatar_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Avatar not found")
        if row.status == "processing":
            row = await _refresh_avatar_status(ctx, row)
        return _avatar_row_to_model(row)


@app.post("/avatars/preview")
async def avatar_preview(
    persona_id: int = Form(...),
    name: str = Form(...),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    custom_prompt: str = Form(default=""),
    skip_style: bool = Form(default=False),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> dict[str, int]:
    """Queue avatar preview generation in the background. Returns avatar_id immediately.

    The client polls ``GET /avatars/{id}/status`` and reads the
    persona's avatars list for the preview ``image_url`` once status flips
    from ``generating`` to ``not_saved``.
    """
    async with open_background_context(tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        parsed_preset_ids = [] if skip_style else _parse_preset_ids(preset_ids)
        effective_custom_prompt = "" if skip_style else custom_prompt
        full_prompt = (
            ""
            if skip_style
            else (build_avatar_edit_prompt(parsed_preset_ids, custom_prompt) or theme_prompt)
        )
        avatar = await repo.insert_persona_avatar(
            ctx.session,
            persona_id=persona_id,
            name=name,
            decoration="" if skip_style else theme_prompt,
            theme_prompt=full_prompt,
            preset_ids=json.dumps(parsed_preset_ids),
            face_id="",
            image_url=persona.image_url,
            status="generating",
            stage="image_generation",
            last_error=None,
        )
        avatar_id = avatar.id
        persona_image_url = persona.image_url

    persona_image_bytes = await _fetch_url_bytes(persona_image_url)
    asyncio.create_task(
        _run_avatar_image_generation(
            tenant_id=tenant_id,
            avatar_id=avatar_id,
            persona_id=persona_id,
            persona_image_bytes=persona_image_bytes,
            preset_ids=parsed_preset_ids,
            custom_prompt=effective_custom_prompt,
            name=name,
            skip_style=skip_style,
        )
    )
    return {"avatar_id": avatar_id}


async def _run_avatar_image_generation(
    *,
    tenant_id: str,
    avatar_id: int,
    persona_id: int,
    persona_image_bytes: bytes,
    preset_ids: list[str],
    custom_prompt: str,
    name: str,
    skip_style: bool = False,
) -> None:
    try:
        prompt = "" if skip_style else build_avatar_edit_prompt(preset_ids, custom_prompt)
        if prompt:
            edited_bytes = await ai_router.generate_avatar(
                persona_image_bytes,
                prompt_chain=[prompt],
                filename=f"{name}.png",
            )
        else:
            edited_bytes = persona_image_bytes

        async with open_background_context(tenant_id) as ctx:
            preview_url = await _upload_image(
                ctx, f"avatars/{avatar_id}/preview", edited_bytes
            )
            await repo.update_persona_avatar(
                ctx.session,
                avatar_id,
                image_url=preview_url,
                status="not_saved",
                last_error=None,
            )
    except Exception as exc:
        async with open_background_context(tenant_id) as ctx:
            await repo.update_persona_avatar(
                ctx.session, avatar_id, status="failed", last_error=str(exc)
            )


@app.post(
    "/avatars", response_model=S.PersonaAvatar, status_code=201
)
async def save_avatar(
    persona_id: int = Form(...),
    name: str = Form(...),
    decoration: str = Form(default=""),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    draft_avatar_id: int | None = Form(default=None),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> S.PersonaAvatar:
    if not settings.simli_api_key:
        raise HTTPException(status_code=500, detail="SIMLI_API_KEY is missing")

    async with open_background_context(tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        draft = (
            await repo.get_persona_avatar(ctx.session, draft_avatar_id)
            if draft_avatar_id
            else None
        )
        source_image_url = (
            draft.image_url if (draft and draft.image_url) else persona.image_url
        )

    image_bytes = await _fetch_url_bytes(source_image_url)

    try:
        upload_response = await upload_face_image(
            api_key=settings.simli_api_key,
            image_bytes=image_bytes,
            filename=f"{name}.png",
            face_name=name,
        )
    except SimliError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    status = normalize_generation_status(upload_response)
    if status in {"processing", "queued", "pending"}:
        face_id = extract_generation_id(upload_response)
        avatar_status = "processing"
    else:
        face_id = extract_face_id(upload_response)
        avatar_status = "ready"
    if not face_id:
        raise HTTPException(
            status_code=502,
            detail=f"Simli did not return a usable avatar id: {upload_response}",
        )

    parsed_preset_ids = _parse_preset_ids(preset_ids)
    async with open_background_context(tenant_id) as ctx:
        if draft_avatar_id:
            row = await repo.update_persona_avatar(
                ctx.session,
                draft_avatar_id,
                face_id=face_id,
                image_url=source_image_url,
                status=avatar_status,
                last_error=None,
            )
        else:
            row = await repo.insert_persona_avatar(
                ctx.session,
                persona_id=persona_id,
                name=name,
                decoration=decoration,
                theme_prompt=theme_prompt,
                preset_ids=json.dumps(parsed_preset_ids),
                face_id=face_id,
                image_url=source_image_url,
                status=avatar_status,
                last_error=None,
            )
        assert row is not None
        model = _avatar_row_to_model(row)

    if model.status == "processing":
        asyncio.create_task(_poll_avatar_until_ready(tenant_id, model.id))

    return model


@app.post("/avatars/{avatar_id}/status", response_model=S.StatusResponse)
async def avatar_status(
    avatar_id: int, payload: S.TenantScoped
) -> S.StatusResponse:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_persona_avatar(ctx.session, avatar_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Avatar not found")
        if row.status == "processing":
            row = await _refresh_avatar_status(ctx, row)
        return S.StatusResponse(
            status=row.status,
            stage=row.stage,
            progress=row.progress,
            last_error=row.last_error,
        )


@app.post("/avatars/{avatar_id}/cascade-count")
async def avatar_cascade_count(
    avatar_id: int, payload: S.TenantScoped
) -> dict[str, int]:
    async with open_background_context(payload.tenant_id) as ctx:
        return {
            "assistants": await repo.count_assistants_for_avatar(ctx.session, avatar_id),
        }


@app.delete("/avatars/{avatar_id}", status_code=200)
async def delete_avatar(avatar_id: int, payload: S.TenantScoped) -> dict[str, int]:
    """Delete an avatar row + its preview blob + the Simli face. Assistants
    that reference this avatar survive; the FK nulls their ``avatar_id`` and
    we additionally clear their stale ``face_id`` string."""
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_persona_avatar(ctx.session, avatar_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Avatar not found")

        # Blob: the avatar preview image.
        if row.image_url:
            await _delete_blob_by_url(ctx, row.image_url)

        # External: the Simli face attached to this avatar.
        if row.face_id and settings.simli_api_key:
            try:
                await delete_face(settings.simli_api_key, row.face_id)
            except SimliError:
                pass

        # face_id on assistants is a free-form string (not an FK) — clear it
        # explicitly before the FK SET NULL nulls their avatar_id.
        await repo.clear_face_id_for_avatar(ctx.session, avatar_id)

        deleted = await repo.delete_persona_avatar(ctx.session, avatar_id)
        return {"deleted": deleted}


@app.post("/avatars/{avatar_id}/cancel")
async def cancel_avatar(avatar_id: int, payload: S.TenantScoped) -> dict[str, str]:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_persona_avatar(ctx.session, avatar_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Avatar not found")
        await repo.update_persona_avatar(
            ctx.session, avatar_id, status="cancelled", stage="cancelled"
        )
        return {"status": "cancelled"}


@app.post("/avatars/{avatar_id}/retry")
async def retry_avatar(avatar_id: int, payload: S.TenantScoped) -> dict[str, str]:
    tenant_id = payload.tenant_id
    async with open_background_context(tenant_id) as ctx:
        row = await repo.get_persona_avatar(ctx.session, avatar_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Avatar not found")
        if row.status not in {"failed", "cancelled"}:
            raise HTTPException(
                status_code=409, detail="Avatar is not in a retryable state"
            )

        if not row.face_id:
            persona = await repo.get_persona_entity(ctx.session, row.persona_id)
            if persona is None:
                raise HTTPException(status_code=404, detail="Persona not found")
            await repo.update_persona_avatar(
                ctx.session,
                avatar_id,
                status="generating",
                stage="image_generation",
                last_error=None,
            )
            persona_image_url = persona.image_url
            name = row.name or f"avatar-{avatar_id}"
            persona_image_bytes = await _fetch_url_bytes(persona_image_url)
            asyncio.create_task(
                _run_avatar_image_generation(
                    tenant_id=tenant_id,
                    avatar_id=avatar_id,
                    persona_id=persona.id,
                    persona_image_bytes=persona_image_bytes,
                    preset_ids=_parse_preset_ids(row.preset_ids),
                    custom_prompt="",
                    name=name,
                )
            )
            return {"status": "retrying"}

        await repo.update_persona_avatar(
            ctx.session, avatar_id, status="processing", stage="queued", last_error=None
        )
    asyncio.create_task(_poll_avatar_until_ready(tenant_id, avatar_id))
    return {"status": "retrying"}


# ── Assistants ────────────────────────────────────────────────────────────────

@app.post("/assistants/list", response_model=list[S.Assistant])
async def list_assistants_endpoint(
    payload: S.TenantScoped,
    persona_id: int | None = Query(default=None),
    avatar_id: int | None = Query(default=None),
    voice_id: str | None = Query(default=None),
    llm_provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.Assistant]:
    async with open_background_context(payload.tenant_id) as ctx:
        rows = await repo.list_assistants(
            ctx.session,
            persona_id=persona_id,
            avatar_id=avatar_id,
            voice_id=voice_id,
            llm_provider=llm_provider,
            status=status,
            limit=limit,
            offset=offset,
        )
        return [_assistant_row_to_model(a) for a in rows]


@app.post("/assistants/{assistant_id}/get", response_model=S.Assistant)
async def get_assistant_endpoint(
    assistant_id: int, payload: S.TenantScoped
) -> S.Assistant:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_assistant(ctx.session, assistant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Assistant not found")
        return _assistant_row_to_model(row)


@app.post(
    "/assistants", response_model=S.Assistant, status_code=201
)
async def create_assistant(payload: S.AssistantCreate) -> S.Assistant:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, payload.persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        avatar = await repo.get_persona_avatar(ctx.session, payload.avatar_id)
        if avatar is None or avatar.persona_id != payload.persona_id:
            raise HTTPException(
                status_code=404, detail="Avatar not found for persona"
            )
        avatar = await _refresh_avatar_status(ctx, avatar)
        if avatar.status != "ready":
            raise HTTPException(
                status_code=409, detail="Avatar is still processing"
            )

        llm_provider = (payload.llm_provider or settings.llm_provider).lower()
        if llm_provider == "gemini":
            llm_model = payload.llm_model or settings.llm_model_gemini
        elif llm_provider == "openai":
            llm_model = payload.llm_model or settings.llm_model_openai
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported llm_provider: {llm_provider}"
            )

        if persona.voice_id and persona.voice_provider:
            voice_provider = persona.voice_provider
            voice_id: str | None = persona.voice_id
            voice_model = (
                settings.tts_model
                if voice_provider == "elevenlabs"
                else settings.default_simli_voice_model
            )
        else:
            voice_provider = settings.default_simli_voice_provider
            voice_id = settings.default_simli_voice_id or None
            voice_model = settings.default_simli_voice_model

        row = await repo.insert_assistant(
            ctx.session,
            name=payload.name,
            prompt=payload.prompt,
            first_message=payload.first_message,
            persona_id=payload.persona_id,
            avatar_id=payload.avatar_id,
            face_id=avatar.face_id,
            simli_agent_id="",
            voice_provider=voice_provider,
            voice_id=voice_id,
            voice_model=voice_model,
            language="en",
            llm_provider=llm_provider,
            llm_model=llm_model,
            status="ready",
            stage="ready",
            progress=100,
        )
        return _assistant_row_to_model(row)


@app.post(
    "/assistants/{assistant_id}/status",
    response_model=S.StatusResponse,
)
async def assistant_status(
    assistant_id: int, payload: S.TenantScoped
) -> S.StatusResponse:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_assistant(ctx.session, assistant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Assistant not found")
        return S.StatusResponse(
            status=row.status,
            stage=row.stage,
            progress=row.progress,
            last_error=row.last_error,
        )


@app.delete("/assistants/{assistant_id}", status_code=200)
async def delete_assistant(
    assistant_id: int, payload: S.TenantScoped
) -> dict[str, int]:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_assistant(ctx.session, assistant_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Assistant not found")
        deleted = await repo.delete_assistant(ctx.session, assistant_id)
        return {"deleted": deleted}


# ── Calls (plug-and-play LiveKit + face/voice metadata) ───────────────────────

@app.post("/calls", response_model=S.AssistantCallResponse)
async def start_call(payload: S.AssistantCallCreate) -> S.AssistantCallResponse:
    if not (
        settings.livekit_url
        and settings.livekit_api_key
        and settings.livekit_api_secret
    ):
        raise HTTPException(status_code=500, detail="LiveKit credentials are missing")

    async with open_background_context(payload.tenant_id) as ctx:
        assistant = await repo.get_assistant(ctx.session, payload.assistant_id)
        if assistant is None:
            raise HTTPException(status_code=404, detail="Assistant not found")
        avatar = await repo.get_persona_avatar(ctx.session, assistant.avatar_id)
        if avatar is None:
            raise HTTPException(status_code=404, detail="Avatar not found")

        # Try to attach a friendly voice name + preview by joining on the
        # voice table when the assistant's voice_id matches a stored voice.
        voice_name = ""
        voice_preview_url = ""
        if assistant.voice_id:
            for v in await repo.list_voices(ctx.session):
                if v.voice_id == assistant.voice_id:
                    voice_name = v.name
                    voice_preview_url = v.preview_url
                    break
            if not voice_preview_url:
                persona = await repo.get_persona_entity(ctx.session, assistant.persona_id)
                if persona and persona.voice_id == assistant.voice_id:
                    voice_preview_url = persona.voice_preview_url

        avatar_image_url = avatar.image_url

    room_name = f"assistant-{payload.assistant_id}-{uuid4().hex[:8]}"
    identity = f"user-{uuid4().hex[:8]}"
    room_config = build_agent_dispatch_room_config(
        room_name=room_name,
        assistant_id=payload.assistant_id,
        tenant_id=payload.tenant_id,
    )
    token = create_join_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        identity=identity,
        room_name=room_name,
        participant_name=f"User {identity[-4:]}",
        room_config=room_config,
    )

    return S.AssistantCallResponse(
        livekit=S.CallLivekit(
            url=settings.livekit_url,
            token=token,
            room=room_name,
            identity=identity,
        ),
        assistant=S.CallAssistant(
            id=assistant.id,
            name=assistant.name,
            first_message=assistant.first_message,
        ),
        avatar=S.CallAvatar(
            id=avatar.id,
            face_id=avatar.face_id,
            image_url=avatar_image_url,
        ),
        voice=S.CallVoice(
            provider=assistant.voice_provider,
            voice_id=assistant.voice_id,
            name=voice_name,
            preview_url=voice_preview_url,
        ),
    )


# ── Standalone voices ─────────────────────────────────────────────────────────

@app.post("/voices/list", response_model=list[S.VoiceEntity])
async def list_voices_endpoint(
    payload: S.TenantScoped,
    gender: str | None = Query(default=None),
    source: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    persona_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.VoiceEntity]:
    async with open_background_context(payload.tenant_id) as ctx:
        rows = await repo.list_voices(
            ctx.session,
            gender=gender,
            source=source,
            provider=provider,
            status=status,
            persona_id=persona_id,
            limit=limit,
            offset=offset,
        )
        return [_voice_row_to_model(v) for v in rows]


@app.post("/voices/{voice_id}/get", response_model=S.VoiceEntity)
async def get_voice_endpoint(voice_id: int, payload: S.TenantScoped) -> S.VoiceEntity:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_voice(ctx.session, voice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Voice not found")
        return _voice_row_to_model(row)


class _VoiceDesignForm(BaseModel):
    name: str
    description: str = ""
    voice_preset_id: str | None = None
    persona_id: int | None = None
    include_persona_traits: bool = False


@app.post(
    "/voices/design", response_model=S.VoiceEntity, status_code=201
)
async def design_voice_standalone(
    name: str = Form(...),
    description: str = Form(default=""),
    voice_preset_id: str | None = Form(default=None),
    persona_id: int | None = Form(default=None),
    include_persona_traits: bool = Form(default=False),
    gender: str | None = Form(
        default=None,
        description="Optional gender directive: 'male' or 'female'. "
        "Appended to the voice description so ElevenLabs biases the design.",
    ),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> S.VoiceEntity:
    if voice_preset_id:
        preset = VOICE_PRESETS.get(voice_preset_id)
        if not preset:
            raise HTTPException(
                status_code=400, detail=f"Unknown voice preset: {voice_preset_id}"
            )
        description = preset["desc"]

    requested_gender = _normalize_gender(gender) if gender else ""
    if gender and requested_gender not in {"male", "female"}:
        raise HTTPException(
            status_code=400,
            detail="gender must be 'male' or 'female' when supplied",
        )

    persona_voice_description = ""
    persona_image_url = ""
    persona_gender = "unknown"
    async with open_background_context(tenant_id) as ctx:
        if persona_id:
            persona = await repo.get_persona_entity(ctx.session, persona_id)
            if persona is not None:
                persona_image_url = persona.image_url
                persona_gender = persona.gender or "unknown"
                if include_persona_traits:
                    persona_voice_description = persona.voice_description or ""

        # When a persona is attached and its detected gender is known, it
        # wins — the persona is the authoritative source for a voice tied
        # to it. Explicit input is only used as a fallback (e.g. no persona,
        # or persona.gender still 'unknown').
        if persona_id and persona_gender in {"male", "female"}:
            effective_gender = persona_gender
        else:
            effective_gender = requested_gender or persona_gender or "unknown"

        initial_description = description.strip()
        if persona_voice_description.strip():
            initial_description = (
                initial_description
                + ("; " if initial_description else "")
                + persona_voice_description.strip()
            )
        row = await repo.insert_voice(
            ctx.session,
            name=name or "New Voice",
            source="designed",
            description=initial_description,
            status="processing",
            persona_id=persona_id,
            gender=effective_gender,
        )
        voice_id = row.id
        snapshot = _voice_row_to_model(row)

    asyncio.create_task(
        _run_standalone_voice_design(
            tenant_id=tenant_id,
            voice_id=voice_id,
            user_description=description,
            persona_voice_description=persona_voice_description,
            persona_image_url=persona_image_url,
            voice_name=name,
            gender=effective_gender,
        )
    )
    return snapshot


async def _run_standalone_voice_design(
    *,
    tenant_id: str,
    voice_id: int,
    user_description: str,
    persona_voice_description: str,
    persona_image_url: str,
    voice_name: str,
    gender: str = "unknown",
) -> None:
    description = user_description.strip()
    if persona_voice_description.strip():
        description = (
            description + ("; " if description else "") + persona_voice_description.strip()
        )
    # Append a gender directive so ElevenLabs's Voice Design produces a voice
    # of the right register. We persist the *raw* description; the prompt
    # sent to ElevenLabs is what gets the suffix.
    prompt_description = description + _gender_prompt_suffix(gender)
    async with open_background_context(tenant_id) as ctx:
        await repo.update_voice(
            ctx.session,
            voice_id,
            status="processing",
            description=description,
            last_error=None,
        )

    try:
        if len(description) < 20:
            raise RuntimeError(
                "Voice description too short — please describe the voice in more detail."
            )
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing.")

        el_voice_id = ""
        preview_bytes: bytes | None = None
        try:
            previews = await elevenlabs_client.create_voice_design_previews(
                settings.elevenlabs_api_key,
                voice_description=prompt_description,
                text=VOICE_PREVIEW_TEXT,
            )
            first = previews[0]
            gen_id = str(first.get("generated_voice_id") or "")
            if not gen_id:
                raise RuntimeError(f"No generated_voice_id returned: {first}")
            preview_bytes = elevenlabs_client.decode_preview_audio(first)
            el_voice_id = await elevenlabs_client.create_voice_from_preview(
                settings.elevenlabs_api_key,
                name=voice_name or f"voice-{voice_id}",
                description=description[:500],
                generated_voice_id=gen_id,
                labels=_labels_for_gender(gender),
            )
        except ElevenLabsError as design_err:
            if design_err.status_code != 403:
                raise
            # Voice Design requires a higher ElevenLabs plan — fall back to a
            # default voice + TTS preview.
            el_voice_id = settings.tts_voice_id or "EXAVITQu4vr4xnSDxMaL"
            preview_bytes = await _store_voice_preview(
                settings.elevenlabs_api_key, el_voice_id
            )

        async with open_background_context(tenant_id) as ctx:
            preview_url = ""
            if preview_bytes:
                preview_url = await _upload_audio(
                    ctx, f"voices/{voice_id}/preview", preview_bytes
                )
            await repo.update_voice(
                ctx.session,
                voice_id,
                voice_id=el_voice_id,
                source="designed",
                description=description,
                preview_url=preview_url,
                status="ready",
                last_error=None,
            )
    except Exception as exc:
        async with open_background_context(tenant_id) as ctx:
            await repo.update_voice(
                ctx.session, voice_id, status="failed", last_error=str(exc)
            )


@app.post(
    "/voices/clone", response_model=S.VoiceEntity, status_code=201
)
async def clone_voice_standalone(
    voice_sample: UploadFile = File(...),
    name: str = Form(default=""),
    persona_id: int | None = Form(default=None),
    tenant_id: str = Form(..., min_length=1, max_length=63),
) -> S.VoiceEntity:
    audio_bytes = await voice_sample.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Voice sample is empty")
    if len(audio_bytes) > 20 * 1024 * 1024:
        raise HTTPException(
            status_code=400, detail="Voice sample is too large (max 20 MB)"
        )
    mime = voice_sample.content_type or "audio/mpeg"
    filename = voice_sample.filename or f"sample-{uuid4().hex}.mp3"
    sample_ext = os.path.splitext(filename)[1].lower() or ".mp3"

    async with open_background_context(tenant_id) as ctx:
        persona_gender = "unknown"
        if persona_id is not None:
            persona = await repo.get_persona_entity(ctx.session, persona_id)
            if persona is not None:
                persona_gender = persona.gender or "unknown"
        row = await repo.insert_voice(
            ctx.session,
            name=name or "Cloned Voice",
            source="cloned",
            status="processing",
            persona_id=persona_id,
            gender=persona_gender,
        )
        voice_id = row.id
        snapshot = _voice_row_to_model(row)

    asyncio.create_task(
        _run_standalone_voice_clone(
            tenant_id=tenant_id,
            voice_id=voice_id,
            audio_bytes=audio_bytes,
            sample_filename=filename,
            sample_mime=mime,
            sample_ext=sample_ext,
            voice_name=name,
            fallback_gender=persona_gender,
        )
    )
    return snapshot


async def _run_standalone_voice_clone(
    *,
    tenant_id: str,
    voice_id: int,
    audio_bytes: bytes,
    sample_filename: str,
    sample_mime: str,
    sample_ext: str,
    voice_name: str,
    fallback_gender: str = "unknown",
) -> None:
    try:
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing.")
        async with open_background_context(tenant_id) as ctx:
            sample_url = await _upload_audio(
                ctx, f"voices/{voice_id}/sample", audio_bytes, ext=sample_ext
            )

        # ElevenLabs's Instant Voice Cloning does not detect gender from the
        # audio. We infer it with Gemini and forward it as a label so the
        # upstream voice row in ElevenLabs is also tagged.
        detected_gender = await ai_router.detect_gender_from_audio(
            audio_bytes, mime_type=sample_mime
        )
        final_gender = (
            detected_gender if detected_gender in {"male", "female"} else fallback_gender
        )

        el_voice_id = await elevenlabs_client.add_cloned_voice(
            settings.elevenlabs_api_key,
            name=voice_name or f"voice-{voice_id}",
            description="Cloned voice",
            audio_bytes=audio_bytes,
            filename=sample_filename,
            mime_type=sample_mime,
            labels=_labels_for_gender(final_gender),
        )
        preview_bytes = await _store_voice_preview(
            settings.elevenlabs_api_key, el_voice_id
        )

        async with open_background_context(tenant_id) as ctx:
            preview_url = ""
            if preview_bytes:
                preview_url = await _upload_audio(
                    ctx, f"voices/{voice_id}/preview", preview_bytes
                )
            await repo.update_voice(
                ctx.session,
                voice_id,
                voice_id=el_voice_id,
                source="cloned",
                sample_url=sample_url,
                preview_url=preview_url,
                gender=final_gender,
                status="ready",
                last_error=None,
            )
    except Exception as exc:
        async with open_background_context(tenant_id) as ctx:
            await repo.update_voice(
                ctx.session, voice_id, status="failed", last_error=str(exc)
            )


class VoiceSuggestDescriptionRequest(S.TenantScoped):
    persona_id: int
    user_hint: str = ""


@app.post("/voices/suggest-description")
async def suggest_voice_description(
    payload: VoiceSuggestDescriptionRequest,
) -> dict[str, str]:
    async with open_background_context(payload.tenant_id) as ctx:
        persona = await repo.get_persona_entity(ctx.session, payload.persona_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="Persona not found")
        image_url = persona.image_url
    try:
        image_bytes = await _fetch_url_bytes(image_url)
        description = await ai_router.describe_voice(
            image_bytes, user_prompt=payload.user_hint
        )
        return {"description": (description or "").strip()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# Tiny in-process cache for the ElevenLabs premade voice library.
_library_cache: list[dict] = []
_library_cache_ts: float = 0.0


@app.post("/voices/library")
async def voice_library(payload: S.TenantScoped) -> list[dict]:
    """Returns ElevenLabs's premade voices. App-level cache (5 min TTL).

    ``tenant_id`` in the body is required by convention; the library is the
    same for every tenant (it comes from the operator's ElevenLabs account).
    """
    _ = payload.tenant_id  # validated by the schema
    global _library_cache, _library_cache_ts
    if _library_cache and (time.monotonic() - _library_cache_ts) < 300:
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
        _library_cache_ts = time.monotonic()
    except Exception:
        _library_cache = []
    return _library_cache


class VoiceFromLibraryRequest(S.TenantScoped):
    voice_id: str
    name: str = ""
    preview_url: str = ""
    gender: str = ""  # optional; if omitted we look it up from the cached library


def _normalize_gender(raw: str | None) -> str:
    val = (raw or "").strip().lower()
    if val in {"male", "female"}:
        return val
    if val in {"non-binary", "nonbinary", "neutral"}:
        return "all"
    return "unknown"


def _gender_prompt_suffix(gender: str) -> str:
    """Append a clear gender directive to a voice description so ElevenLabs
    Voice Design biases toward the right vocal register."""
    if gender == "male":
        return " The voice should sound clearly male."
    if gender == "female":
        return " The voice should sound clearly female."
    return ""


def _labels_for_gender(gender: str) -> dict[str, str]:
    """Build the ``labels`` dict we forward to ElevenLabs at voice create
    time. Mirrors the convention used by their premade library voices."""
    if gender in {"male", "female"}:
        return {"gender": gender}
    return {}


@app.post(
    "/voices/from-library",
    response_model=S.VoiceEntity,
    status_code=201,
)
async def voice_from_library(payload: VoiceFromLibraryRequest) -> S.VoiceEntity:
    if not payload.voice_id:
        raise HTTPException(status_code=400, detail="voice_id required")

    gender = _normalize_gender(payload.gender)
    if gender == "unknown" and _library_cache:
        for entry in _library_cache:
            if entry.get("voice_id") == payload.voice_id:
                gender = _normalize_gender(
                    (entry.get("labels") or {}).get("gender")
                )
                break

    async with open_background_context(payload.tenant_id) as ctx:
        existing = [
            v
            for v in await repo.list_voices(ctx.session)
            if v.voice_id == payload.voice_id and v.source == "premade"
        ]
        if existing:
            return _voice_row_to_model(existing[0])
        row = await repo.insert_voice(
            ctx.session,
            name=payload.name or f"EL {payload.voice_id[:8]}",
            provider="elevenlabs",
            voice_id=payload.voice_id,
            source="premade",
            description="",
            preview_url=payload.preview_url,
            gender=gender,
            status="ready",
        )
        return _voice_row_to_model(row)


@app.post("/voices/preview-default")
async def voice_preview_default(payload: S.TenantScoped) -> Response:
    """Streams an MP3 preview of the env-configured default voice."""
    _ = payload.tenant_id  # validated by the schema
    voice_id = settings.tts_voice_id
    if not voice_id or not settings.elevenlabs_api_key:
        raise HTTPException(status_code=404, detail="No default voice configured")
    mp3 = await elevenlabs_client.synthesize(
        settings.elevenlabs_api_key,
        voice_id=voice_id,
        text=VOICE_PREVIEW_TEXT,
        model_id=settings.tts_model,
    )
    if not mp3:
        raise HTTPException(
            status_code=502, detail="ElevenLabs returned an empty preview"
        )
    return Response(content=mp3, media_type="audio/mpeg")


@app.post("/voices/{voice_id}/status", response_model=S.StatusResponse)
async def voice_status(voice_id: int, payload: S.TenantScoped) -> S.StatusResponse:
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_voice(ctx.session, voice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Voice not found")
        return S.StatusResponse(
            status=row.status,
            stage=None,
            progress=None,
            last_error=row.last_error,
        )


@app.delete("/voices/{voice_id}", status_code=200)
async def delete_voice(voice_id: int, payload: S.TenantScoped) -> dict[str, int]:
    """Delete a standalone voice row + its sample/preview blobs + the
    ElevenLabs voice. Any persona that referenced this voice via
    ``voice_ref_id`` has its voice fields cleared."""
    async with open_background_context(payload.tenant_id) as ctx:
        row = await repo.get_voice(ctx.session, voice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Voice not found")

        # Blob: sample + preview audio.
        if row.sample_url:
            await _delete_blob_by_url(ctx, row.sample_url)
        if row.preview_url:
            await _delete_blob_by_url(ctx, row.preview_url)

        # External: ElevenLabs voice.
        if row.voice_id and settings.elevenlabs_api_key:
            try:
                await elevenlabs_client.delete_voice(
                    settings.elevenlabs_api_key, row.voice_id
                )
            except ElevenLabsError:
                pass

        # Clear voice fields on every persona that referenced this voice.
        await repo.clear_persona_voice_ref(ctx.session, voice_id)

        await repo.delete_voice(ctx.session, voice_id)
        return {"deleted": 1}


@app.post("/voices/{voice_id}/retry")
async def retry_voice(voice_id: int, payload: S.TenantScoped) -> dict[str, str]:
    tenant_id = payload.tenant_id
    async with open_background_context(tenant_id) as ctx:
        row = await repo.get_voice(ctx.session, voice_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Voice not found")
        if row.status not in {"failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="Voice is not in a retryable state")

        source = row.source or "designed"
        if source == "cloned":
            if not row.sample_url:
                raise HTTPException(
                    status_code=400,
                    detail="Original sample is not stored — please re-upload.",
                )
            audio_bytes = await _fetch_url_bytes(row.sample_url)
            sample_ext = os.path.splitext(row.sample_url.split("?", 1)[0])[1].lower() or ".mp3"
            await repo.update_voice(
                ctx.session, voice_id, status="processing", last_error=None
            )
            asyncio.create_task(
                _run_standalone_voice_clone(
                    tenant_id=tenant_id,
                    voice_id=voice_id,
                    audio_bytes=audio_bytes,
                    sample_filename=f"voice-{voice_id}{sample_ext}",
                    sample_mime="audio/mpeg",
                    sample_ext=sample_ext,
                    voice_name=row.name or f"voice-{voice_id}",
                    fallback_gender=row.gender or "unknown",
                )
            )
        else:
            description = row.description or ""
            if not description:
                raise HTTPException(
                    status_code=400,
                    detail="No description stored — please recreate the voice.",
                )
            persona_image_url = ""
            if row.persona_id:
                persona = await repo.get_persona_entity(ctx.session, row.persona_id)
                if persona is not None:
                    persona_image_url = persona.image_url
            await repo.update_voice(
                ctx.session, voice_id, status="processing", last_error=None
            )
            asyncio.create_task(
                _run_standalone_voice_design(
                    tenant_id=tenant_id,
                    voice_id=voice_id,
                    user_description=description,
                    persona_voice_description="",
                    persona_image_url=persona_image_url,
                    voice_name=row.name or f"voice-{voice_id}",
                    gender=row.gender or "unknown",
                )
            )
    return {"status": "retrying"}


