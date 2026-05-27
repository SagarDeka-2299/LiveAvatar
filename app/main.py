"""Lili Studio — pure multi-tenant API.

This module exposes the full Studio API surface.

  * Every business route requires ``?tenant_id=<id>`` on the URL. The
    tenancy layer resolves the tenant's Postgres database and Azure
    Blob credentials via Key Vault; ``tenant_id=local_tenant`` triggers
    the local fallback (per-tenant SQLite + local-disk blobs).
  * All persistent media (persona source images, avatar previews, voice
    samples, voice previews) lives in Azure Blob Storage and is
    referenced by permanent, unsigned URLs. Local-fallback runs stamp
    ``file://...`` URLs — internal-only, browsers cannot resolve them.
  * The WebSocket hub is gone. Background-task progress is exposed via
    ``GET /{resource}/{id}/status`` for polling.
  * ``POST /calls`` returns a single plug-and-play payload with
    LiveKit credentials AND assistant/avatar/voice metadata.
  * No bundled front-end on the API surface. A reference UI is mounted
    at ``/demo`` for developers, but the API itself is headless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    start_auto_session,
    upload_face_image,
)
from app.tenancy import (
    TenantContext,
    open_background_context,
    shutdown_tenants,
    tenant_ctx,
)

import sys

# Configure root logging once, on import, so app-level ``logger.info`` calls
# (avatar / voice / persona pipeline progress) actually reach the container
# stdout. Without this the root logger defaults to WARNING and every INFO
# line is silently dropped. Level is overridable via ``LOG_LEVEL``.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lili")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.handlers.clear()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logger.addHandler(handler)
logger.propagate = False


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

# ── Preset library v2 ────────────────────────────────────────────────────────
#
# Three-level taxonomy shared by both modalities:
#
#     category → partition → subcategory  (with gender: male | female | unisex)
#
# Selection rules enforced by the UI AND validated server-side in the
# prompt builders below:
#
#   * the user can select chips from multiple categories simultaneously;
#   * within one category, they can pick across multiple partitions;
#   * within a single partition they can pick at most ONE subcategory
#     (the chips in a partition are mutually exclusive).
#
# Frontend additionally hides any subcategory whose ``gender`` is neither
# ``"unisex"`` nor the persona's detected gender.
#
# ``subcategory`` doubles as the UI chip label, ``prompt`` is the
# detailed paragraph appended to the model brief. Add / edit / re-order
# entries below to extend the library — no DB migration required.

AVATAR_PRESETS: dict[str, dict[str, str]] = {
    # ── Outfit ──
    # Partition: Base — one head-to-toe outfit; mutually exclusive.
    "outfit_base_casual_tee_jeans":      {"category": "Outfit", "partition": "Base", "subcategory": "Casual T-Shirt & Jeans",      "gender": "unisex", "prompt": "modern casual streetwear — well-fitted dark or indigo jeans, a clean cotton t-shirt or henley layered under an unbuttoned overshirt or denim jacket, fresh white or low-top sneakers; relaxed but intentional, contemporary and approachable, weekend-confident"},
    "outfit_base_smart_casual_m":        {"category": "Outfit", "partition": "Base", "subcategory": "Smart Casual Polo & Chinos",  "gender": "male",   "prompt": "a fitted short-sleeve polo or knit collared shirt tucked into slim chinos, leather belt and clean loafers or low-profile sneakers; modern smart-casual — polished but unfussy"},
    "outfit_base_formal_suit_m":         {"category": "Outfit", "partition": "Base", "subcategory": "Formal Two-Piece Suit",       "gender": "male",   "prompt": "a precision-tailored two-piece suit in charcoal or navy, crisp white or pale blue shirt, silk tie in a confident solid colour, pocket square, polished black or oxblood oxfords; boardroom-ready presence"},
    "outfit_base_tuxedo_m":              {"category": "Outfit", "partition": "Base", "subcategory": "Tuxedo / Black Tie",          "gender": "male",   "prompt": "a black-tie formal tuxedo with peak or shawl lapels in matte black, crisp white wing-collar shirt, silk bow tie, mother-of-pearl studs, cufflinks, patent oxfords; red-carpet elegance"},
    "outfit_base_blouse_skirt_f":        {"category": "Outfit", "partition": "Base", "subcategory": "Business Blouse + Pencil Skirt", "gender": "female", "prompt": "a tailored silk or crepe blouse in a soft neutral with a modest neckline, paired with a slim-fitted pencil skirt that lands just below the knee, sheer hose, low pumps; polished corporate confidence"},
    "outfit_base_pantsuit_f":            {"category": "Outfit", "partition": "Base", "subcategory": "Tailored Pantsuit",           "gender": "female", "prompt": "a precisely cut pantsuit in deep navy or charcoal with structured shoulders and a tucked-in silk shell, sharp slim trousers, pointed leather flats or low heels; understated executive presence"},
    "outfit_base_evening_dress_f":       {"category": "Outfit", "partition": "Base", "subcategory": "Elegant Evening Dress",       "gender": "female", "prompt": "an elegant floor-length evening dress with a flattering silhouette in a rich solid colour (jewel tones — emerald, sapphire, garnet), delicate satin or crepe with subtle sheen, refined neckline, minimal embellishment; gala-ready sophistication"},
    "outfit_base_cocktail_dress_f":      {"category": "Outfit", "partition": "Base", "subcategory": "Cocktail Dress",              "gender": "female", "prompt": "a knee-length cocktail dress in black, midnight blue, or wine red, structured bodice, subtle texture or delicate sequin work, paired with heels; evening-event confident"},
    "outfit_base_summer_dress_f":        {"category": "Outfit", "partition": "Base", "subcategory": "Casual Summer Dress",         "gender": "female", "prompt": "a breezy mid-length summer dress in a soft floral or pastel solid, light cotton or linen, simple cap or short sleeves, flat sandals; relaxed feminine warmth"},
    "outfit_base_hoodie_joggers":        {"category": "Outfit", "partition": "Base", "subcategory": "Hoodie & Joggers",            "gender": "unisex", "prompt": "a comfortable cotton hoodie in heathered grey or cream, slightly oversized fit, paired with matching joggers or dark sweats, clean low-top sneakers; relaxed creator-economy aesthetic"},
    "outfit_base_athletic":              {"category": "Outfit", "partition": "Base", "subcategory": "Athletic Performance Wear",   "gender": "unisex", "prompt": "fitted technical training wear — moisture-wicking shirt, running shorts or compression leggings, performance trainers; clean athleisure lines, ready-for-the-track energy"},
    "outfit_base_yoga":                  {"category": "Outfit", "partition": "Base", "subcategory": "Yoga / Activewear",           "gender": "unisex", "prompt": "soft fitted yoga-studio wear — a stretch tank or fitted long-sleeve over high-waisted leggings, barefoot or minimalist trainers; calm wellness aesthetic"},
    "outfit_base_streetwear":            {"category": "Outfit", "partition": "Base", "subcategory": "Streetwear (Oversized)",      "gender": "unisex", "prompt": "current streetwear — oversized graphic tee or hoodie, cargo trousers or wide-leg jeans, statement chunky trainers; downtown energy, slightly off-duty cool"},
    "outfit_base_kurta_m":               {"category": "Outfit", "partition": "Base", "subcategory": "Traditional Indian Kurta",    "gender": "male",   "prompt": "an elegant knee-length silk or cotton kurta in a rich solid (cream, deep maroon, navy) with subtle embroidery at the collar and cuffs, paired with slim churidar or pyjama pants and embroidered juti; refined traditional South Asian formal wear"},
    "outfit_base_saree_f":               {"category": "Outfit", "partition": "Base", "subcategory": "Traditional Saree",           "gender": "female", "prompt": "a richly draped silk saree in a deep solid jewel tone with a contrasting embroidered border, elegantly pleated and pinned at the shoulder, paired with a matching short-sleeved blouse and traditional jewelry; classical South Asian elegance"},
    "outfit_base_kimono":                {"category": "Outfit", "partition": "Base", "subcategory": "Kimono",                      "gender": "unisex", "prompt": "a flowing modern kimono robe in a deep solid colour with subtle pattern at the hem and cuffs, traditional obi belt at the waist, layered over a simple base garment; refined Japanese-influenced silhouette"},

    # Partition: Outer Layer — single jacket / coat over the base outfit.
    "outfit_outer_none":                 {"category": "Outfit", "partition": "Outer Layer", "subcategory": "No Outer Layer",       "gender": "unisex", "prompt": "no outer jacket or coat — the base outfit shown as-is, sleeves of the base garment visible"},
    "outfit_outer_blazer":               {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Tailored Blazer",      "gender": "unisex", "prompt": "a structured tailored blazer in a deep solid (charcoal, midnight blue, or camel), sharp shoulders, slim sleeves rolled or worn flat, a single fastened button at the waist; modern power dressing layered over the base outfit"},
    "outfit_outer_lab_coat":             {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Lab Coat",             "gender": "unisex", "prompt": "a crisp white knee-length lab coat layered over the base outfit, ID badge clipped at the chest pocket, sleeves at the wrist, a stethoscope draped at the collar if appropriate; clinical professionalism"},
    "outfit_outer_leather_jacket":       {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Leather Jacket",       "gender": "unisex", "prompt": "a fitted moto-style leather jacket in matte black with asymmetric zip and slim collar, layered over the base outfit, sleeves at the wrist; edgy, charismatic, slightly rebellious"},
    "outfit_outer_trench":               {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Trench Coat",          "gender": "unisex", "prompt": "a classic camel or stone-coloured trench coat with double-breasted lapels and a tied waist belt, knee-length, worn open over the base outfit; timeless cinematic silhouette"},
    "outfit_outer_cardigan":             {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Cardigan",             "gender": "unisex", "prompt": "a fine-knit open-front cardigan in a soft neutral (oatmeal, charcoal, soft grey), worn relaxed over the base outfit; quiet intellectual warmth"},
    "outfit_outer_denim_jacket":         {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Denim Jacket",         "gender": "unisex", "prompt": "a classic-cut blue denim jacket with chest pockets, worn open over the base outfit, sleeves at the wrist; casual, ageless, all-American"},
    "outfit_outer_long_coat":            {"category": "Outfit", "partition": "Outer Layer", "subcategory": "Long Wool Coat",       "gender": "unisex", "prompt": "a long single-breasted wool coat in camel, charcoal, or black, hitting mid-calf, clean notch lapels, structured shoulders, worn open over the base outfit; refined cold-weather elegance"},

    # ── Hair ──
    # Partition: Style — one silhouette / cut.
    "hair_style_updo_f":                 {"category": "Hair", "partition": "Style", "subcategory": "Sleek Updo",               "gender": "female", "prompt": "an elegant low chignon or French twist, hair smoothed back with refined polish, a few delicate face-framing strands left loose around the temples; formal yet soft"},
    "hair_style_loose_bun_f":            {"category": "Hair", "partition": "Style", "subcategory": "Loose Bun",                "gender": "female", "prompt": "a relaxed messy bun at the nape or crown, soft tendrils escaping naturally, slight volume at the crown; effortless feminine charm"},
    "hair_style_braided_crown_f":        {"category": "Hair", "partition": "Style", "subcategory": "Braided Crown",            "gender": "female", "prompt": "a halo braid wrapping around the crown of the head, smooth and tightly woven, a few soft face-framing strands left out; romantic and intricate"},
    "hair_style_box_braids_f":           {"category": "Hair", "partition": "Style", "subcategory": "Box Braids",               "gender": "female", "prompt": "long, evenly sized box braids worn loose past the shoulders or gathered into a high tie, clean parted scalp, healthy sheen; bold protective styling"},
    "hair_style_long_flowing_f":         {"category": "Hair", "partition": "Style", "subcategory": "Long Flowing",             "gender": "female", "prompt": "long straight or gently waved hair flowing past the shoulders, glossy and well-conditioned, subtle layers framing the face; classic feminine softness"},
    "hair_style_bob_f":                  {"category": "Hair", "partition": "Style", "subcategory": "Bob Cut",                  "gender": "female", "prompt": "a sleek chin-length bob with a clean blunt cut, smooth finish, sharp lines, slight side-part; modern minimalist haircut"},
    "hair_style_pixie_f":                {"category": "Hair", "partition": "Style", "subcategory": "Pixie Cut",                "gender": "female", "prompt": "a short cropped pixie cut with textured layers on top and clean tapered sides, swept slightly to one side; bold and architectural"},
    "hair_style_wavy_shoulder":          {"category": "Hair", "partition": "Style", "subcategory": "Wavy Shoulder-Length",     "gender": "unisex", "prompt": "shoulder-length hair with natural-looking soft waves, easy middle or side parting, healthy texture, slight volume at the roots; relaxed contemporary cut"},
    "hair_style_high_ponytail":          {"category": "Hair", "partition": "Style", "subcategory": "High Ponytail",            "gender": "unisex", "prompt": "a sleek high ponytail tied tightly at the crown with a discreet band, hair smoothed back from the face; clean, athletic, polished"},
    "hair_style_slick_back_m":           {"category": "Hair", "partition": "Style", "subcategory": "Slick Back",               "gender": "male",   "prompt": "hair combed straight back with subtle product hold and a slight gloss, neat tapered sides and a defined hairline; classic gentleman finish"},
    "hair_style_crew_cut_m":             {"category": "Hair", "partition": "Style", "subcategory": "Crew Cut",                 "gender": "male",   "prompt": "a short crew cut with cleanly tapered sides and slightly longer length on top, sharp hairline, low-maintenance polish; military-adjacent refinement"},
    "hair_style_buzz":                   {"category": "Hair", "partition": "Style", "subcategory": "Buzz Cut",                 "gender": "unisex", "prompt": "a uniform short buzz cut close to the scalp, sharp clean hairline, minimal texture; bold and architectural simplicity"},
    "hair_style_curly_natural":          {"category": "Hair", "partition": "Style", "subcategory": "Curly Natural",            "gender": "unisex", "prompt": "natural voluminous curls or tight coils, well-defined and moisturised, falling in a healthy halo around the face; embraced texture and shine"},
    "hair_style_side_swept_m":           {"category": "Hair", "partition": "Style", "subcategory": "Side-Swept",               "gender": "male",   "prompt": "medium-length hair swept to one side with a deep part, smooth finish, soft natural movement at the ends; classic masculine versatility"},
    "hair_style_top_knot_m":             {"category": "Hair", "partition": "Style", "subcategory": "Man-Bun / Top Knot",       "gender": "male",   "prompt": "longer hair pulled tightly into a man-bun or top knot at the crown, neat tapered or undercut sides, clean hairline; modern downtown look"},
    "hair_style_undercut":               {"category": "Hair", "partition": "Style", "subcategory": "Shaved Sides + Top",       "gender": "unisex", "prompt": "very short shaved sides with a sharp fade, longer styled length on top swept or textured upward, defined contrast; bold modern cut"},
    "hair_style_bald":                   {"category": "Hair", "partition": "Style", "subcategory": "Bald",                     "gender": "unisex", "prompt": "a fully shaved head with a clean smooth scalp, sharp hairline if any peach fuzz remains, healthy skin tone showing; bold and confident"},

    # Partition: Color — one hair colour.
    "hair_color_black":                  {"category": "Hair", "partition": "Color", "subcategory": "Natural Black",            "gender": "unisex", "prompt": "rich glossy natural black hair colour with healthy dimensional shine and subtle warm undertones"},
    "hair_color_dark_brown":             {"category": "Hair", "partition": "Color", "subcategory": "Dark Brown",               "gender": "unisex", "prompt": "deep chestnut dark-brown hair colour with subtle warm sheen and natural depth"},
    "hair_color_light_brown":            {"category": "Hair", "partition": "Color", "subcategory": "Light Brown / Caramel",    "gender": "unisex", "prompt": "warm light-brown to caramel hair colour with soft golden highlights and a glossy finish"},
    "hair_color_blonde":                 {"category": "Hair", "partition": "Color", "subcategory": "Blonde",                   "gender": "unisex", "prompt": "natural golden-blonde hair colour with sun-kissed depth at the roots fading to lighter tips"},
    "hair_color_platinum":               {"category": "Hair", "partition": "Color", "subcategory": "Platinum Blonde",          "gender": "unisex", "prompt": "icy platinum-blonde hair colour with a clean cool tone, uniform pale finish, smooth shine"},
    "hair_color_red":                    {"category": "Hair", "partition": "Color", "subcategory": "Auburn / Red",             "gender": "unisex", "prompt": "rich auburn-red hair colour with deep copper undertones and natural-looking variation"},
    "hair_color_grey":                   {"category": "Hair", "partition": "Color", "subcategory": "Silver / Grey",            "gender": "unisex", "prompt": "natural or stylised silver-grey hair colour with cool steel undertones and a polished finish"},
    "hair_color_highlights":             {"category": "Hair", "partition": "Color", "subcategory": "Highlighted",              "gender": "unisex", "prompt": "natural base hair colour with sun-kissed face-framing highlights and subtle dimensional contrast"},
    "hair_color_ombre":                  {"category": "Hair", "partition": "Color", "subcategory": "Ombre",                    "gender": "unisex", "prompt": "ombre hair colour with darker roots gradually fading to lighter caramel or copper tips, blended smoothly"},
    "hair_color_vibrant":                {"category": "Hair", "partition": "Color", "subcategory": "Vibrant Dyed",             "gender": "unisex", "prompt": "vibrant fashion-dyed hair colour — pastel pink, electric blue, or jewel-tone purple — saturated and confident, healthy gloss"},

    # ── Facial Hair ── (gendered)
    "fhair_beard_clean":                 {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Clean Shaven",   "gender": "unisex", "prompt": "completely clean-shaven cheeks and jaw, no beard or stubble visible, smooth skin"},
    "fhair_beard_light_stubble":         {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Light Stubble",  "gender": "male",   "prompt": "a fine even layer of light five-o'clock-shadow stubble across the jawline and chin, neatly maintained but not yet a beard"},
    "fhair_beard_heavy_stubble":         {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Heavy Stubble",  "gender": "male",   "prompt": "a thicker even layer of heavy stubble, edges loosely defined along the cheekbones and neck, deliberate but rough texture"},
    "fhair_beard_short":                 {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Short Beard",    "gender": "male",   "prompt": "a closely trimmed short beard a few millimetres long, evenly shaped along the jaw and chin, sharp cheek and neck lines"},
    "fhair_beard_full":                  {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Full Beard",     "gender": "male",   "prompt": "a well-groomed full beard, dense even growth shaped along the jawline, trimmed cleanly, mature and distinguished"},
    "fhair_beard_long":                  {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Long Bushy Beard","gender": "male",   "prompt": "a long bushy beard reaching mid-chest, full volume, slight natural texture, neatly groomed at the edges; rugged and characterful"},
    "fhair_beard_goatee":                {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Goatee",         "gender": "male",   "prompt": "a goatee — beard limited to the chin and immediately around the mouth, cheeks clean-shaven; sharp defined edges"},
    "fhair_beard_vandyke":               {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Van Dyke",       "gender": "male",   "prompt": "a Van Dyke combo of pointed goatee and disconnected mustache, cheeks clean-shaven, sculpted classic-rogue silhouette"},
    "fhair_beard_mutton":                {"category": "Facial Hair", "partition": "Beard",    "subcategory": "Mutton Chops",   "gender": "male",   "prompt": "thick sideburns extending down the cheek and joining at the moustache while the chin is clean-shaven; bold vintage statement"},
    "fhair_mustache_none":               {"category": "Facial Hair", "partition": "Mustache", "subcategory": "No Mustache",    "gender": "unisex", "prompt": "no mustache, the upper lip cleanly shaved"},
    "fhair_mustache_trimmed":            {"category": "Facial Hair", "partition": "Mustache", "subcategory": "Trimmed Mustache","gender": "male",   "prompt": "a neatly trimmed natural mustache, sharp upper-lip line, no surrounding scruff; clean retro-modern grooming"},
    "fhair_mustache_handlebar":          {"category": "Facial Hair", "partition": "Mustache", "subcategory": "Handlebar",      "gender": "male",   "prompt": "a curled handlebar mustache, twisted at the ends with a slight upward flourish; theatrical vintage character"},
    "fhair_mustache_chevron":            {"category": "Facial Hair", "partition": "Mustache", "subcategory": "Chevron",        "gender": "male",   "prompt": "a thick straight chevron mustache covering the entire upper lip, no curl at the ends, evenly trimmed"},
    "fhair_mustache_pencil":             {"category": "Facial Hair", "partition": "Mustache", "subcategory": "Pencil",         "gender": "male",   "prompt": "a thin pencil-line mustache traced along the upper lip, sharp and precise, classic film-noir feel"},

    # ── Makeup ──
    "mk_lips_bare":                      {"category": "Makeup", "partition": "Lips",   "subcategory": "Bare / Balm",            "gender": "unisex", "prompt": "bare natural lips with only a subtle moisturising lip-balm sheen, no color"},
    "mk_lips_nude":                      {"category": "Makeup", "partition": "Lips",   "subcategory": "Nude Lipstick",          "gender": "female", "prompt": "a soft nude lipstick a touch deeper than the natural lip tone, satin finish, gently defined lip line"},
    "mk_lips_red":                       {"category": "Makeup", "partition": "Lips",   "subcategory": "Red Matte",              "gender": "female", "prompt": "vibrant matte red lipstick as the focal point, crisply defined lip line, slight blue undertone for a classic high-contrast finish"},
    "mk_lips_berry":                     {"category": "Makeup", "partition": "Lips",   "subcategory": "Berry Stain",            "gender": "female", "prompt": "a deep berry-stained lip in raspberry or wine, slightly diffused edges for a just-bitten finish"},
    "mk_lips_glossy_pink":               {"category": "Makeup", "partition": "Lips",   "subcategory": "Glossy Pink",            "gender": "female", "prompt": "a soft glossy pink lip with high-shine finish, fresh and youthful, lightly defined edges"},
    "mk_lips_bold_plum":                 {"category": "Makeup", "partition": "Lips",   "subcategory": "Bold Plum",              "gender": "female", "prompt": "a saturated plum lip in matte or satin finish, deep and dramatic, sharply defined lip line"},
    "mk_lips_coral":                     {"category": "Makeup", "partition": "Lips",   "subcategory": "Coral",                  "gender": "female", "prompt": "a warm coral lip with light satin finish, sun-kissed and lively"},

    "mk_eyes_natural":                   {"category": "Makeup", "partition": "Eyes",   "subcategory": "Natural / Mascara Only", "gender": "female", "prompt": "natural lashes lightly defined with mascara only, no shadow or liner, brightened by a touch of inner-corner highlight"},
    "mk_eyes_soft_smoky":                {"category": "Makeup", "partition": "Eyes",   "subcategory": "Soft Brown Smoky",       "gender": "female", "prompt": "soft warm-brown smoky eye blended diffusely along the lash line, smudged kohl, gently smoked outer corners"},
    "mk_eyes_dramatic_smoky":            {"category": "Makeup", "partition": "Eyes",   "subcategory": "Dramatic Smoky Black",   "gender": "female", "prompt": "dramatic deep-black and charcoal smoky eye, intensely blended around the lash line, defined outer corner, lash extensions for drama"},
    "mk_eyes_winged_liner":              {"category": "Makeup", "partition": "Eyes",   "subcategory": "Winged Liner",           "gender": "female", "prompt": "crisp black winged liner with a precise upward flick at the outer corner, otherwise minimal shadow"},
    "mk_eyes_cat_eye":                   {"category": "Makeup", "partition": "Eyes",   "subcategory": "Cat Eye",                "gender": "female", "prompt": "a bold cat-eye combining winged liner with subtly contoured outer-corner shadow, lifted feline shape"},
    "mk_eyes_bold_liner":                {"category": "Makeup", "partition": "Eyes",   "subcategory": "Bold Liner w/ Color",    "gender": "female", "prompt": "bold liner in an unexpected colour (jewel emerald, sapphire, or amber), graphic line shape, otherwise muted complexion"},
    "mk_eyes_glitter":                   {"category": "Makeup", "partition": "Eyes",   "subcategory": "Glitter Accent",         "gender": "female", "prompt": "a fine glitter shimmer pressed onto the centre of the eyelid, light catching subtly, otherwise soft natural shadow"},
    "mk_eyes_bronzy":                    {"category": "Makeup", "partition": "Eyes",   "subcategory": "Bronzy Shimmer",         "gender": "female", "prompt": "warm bronze shimmer washed across the lid, defined lash line, glossy lower lash brightener for radiance"},

    "mk_cheeks_bare":                    {"category": "Makeup", "partition": "Cheeks", "subcategory": "Bare",                   "gender": "unisex", "prompt": "natural skin on the cheeks with no blush or contour"},
    "mk_cheeks_rosy":                    {"category": "Makeup", "partition": "Cheeks", "subcategory": "Rosy Blush",             "gender": "female", "prompt": "a soft rosy blush diffused high on the cheekbones, gives a healthy flush"},
    "mk_cheeks_bronzed":                 {"category": "Makeup", "partition": "Cheeks", "subcategory": "Bronzed Contour",        "gender": "female", "prompt": "warm bronzer swept along the hollows of the cheekbones for a soft sun-kissed sculpt"},
    "mk_cheeks_dewy":                    {"category": "Makeup", "partition": "Cheeks", "subcategory": "Dewy Highlighter",       "gender": "female", "prompt": "wet-look highlighter on the tops of the cheekbones, brow bone, and cupid's bow for radiant dewy reflection"},
    "mk_cheeks_sculpted":                {"category": "Makeup", "partition": "Cheeks", "subcategory": "Sculpted Contour",       "gender": "female", "prompt": "precise sculpted contour beneath the cheekbones in a cool deeper tone, sharply blended, structured face shape"},

    "mk_brows_natural":                  {"category": "Makeup", "partition": "Brows",  "subcategory": "Natural Brows",          "gender": "unisex", "prompt": "natural brows brushed into shape, no fill, soft naturalistic finish"},
    "mk_brows_defined":                  {"category": "Makeup", "partition": "Brows",  "subcategory": "Defined Brows",          "gender": "female", "prompt": "neatly defined brows with light pencil or powder fill, clean tapered tail, slight arch"},
    "mk_brows_bold":                     {"category": "Makeup", "partition": "Brows",  "subcategory": "Bold Statement Brows",   "gender": "female", "prompt": "bold full statement brows, evenly filled in a rich saturated tone, structured shape with a high arch"},
    "mk_brows_soft_arch":                {"category": "Makeup", "partition": "Brows",  "subcategory": "Soft Arched",            "gender": "female", "prompt": "softly arched brows brushed up at the front, gently feathered, naturally elevated"},

    "mk_base_bare":                      {"category": "Makeup", "partition": "Base",   "subcategory": "Bare Skin",              "gender": "unisex", "prompt": "completely bare skin with no makeup base, natural texture visible, healthy complexion"},
    "mk_base_tinted":                    {"category": "Makeup", "partition": "Base",   "subcategory": "Tinted Moisturizer",     "gender": "unisex", "prompt": "light tinted moisturiser evening the complexion, freckles still visible, natural finish"},
    "mk_base_dewy_glow":                 {"category": "Makeup", "partition": "Base",   "subcategory": "Dewy Glow Finish",       "gender": "female", "prompt": "luminous dewy-glow foundation with a wet finish, hydrated reflective skin, minimal powder"},
    "mk_base_matte":                     {"category": "Makeup", "partition": "Base",   "subcategory": "Matte Full Coverage",    "gender": "female", "prompt": "full-coverage matte foundation, even velvet finish, no shine, structured polish"},
    "mk_base_soft_natural":              {"category": "Makeup", "partition": "Base",   "subcategory": "Soft Natural Finish",    "gender": "female", "prompt": "medium-coverage soft-natural foundation, lightly powdered, balanced satin finish that respects skin texture"},

    # ── Accessories ──
    "acc_eyewear_none":                  {"category": "Accessories", "partition": "Eyewear",     "subcategory": "No Eyewear",            "gender": "unisex", "prompt": "no glasses or sunglasses; eyes fully visible"},
    "acc_eyewear_aviator":               {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Aviator Sunglasses",    "gender": "unisex", "prompt": "classic aviator sunglasses with a thin metallic frame and slight teardrop lenses, smooth lens reflection"},
    "acc_eyewear_wayfarer":              {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Wayfarer Sunglasses",   "gender": "unisex", "prompt": "classic black wayfarer sunglasses with a chunky acetate frame, dark uniform lenses"},
    "acc_eyewear_cat_eye_f":             {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Cat-Eye Sunglasses",    "gender": "female", "prompt": "oversized cat-eye sunglasses with an upswept outer corner and tortoiseshell or solid black acetate frame; confident and cinematic"},
    "acc_eyewear_prescription":          {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Prescription Acetate",  "gender": "unisex", "prompt": "stylish prescription eyeglasses with thin tortoiseshell or matte-black acetate frames, subtly intellectual"},
    "acc_eyewear_round":                 {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Round Wire-Frame",      "gender": "unisex", "prompt": "delicate round wire-frame glasses in gold or gunmetal, vintage-modern feel"},
    "acc_eyewear_reading":               {"category": "Accessories", "partition": "Eyewear",     "subcategory": "Half-Frame Reading",    "gender": "unisex", "prompt": "half-frame reading glasses worn low on the nose, slim metallic frame, scholarly air"},

    "acc_headwear_none":                 {"category": "Accessories", "partition": "Headwear",    "subcategory": "No Headwear",           "gender": "unisex", "prompt": "no hat or head covering"},
    "acc_headwear_cap":                  {"category": "Accessories", "partition": "Headwear",    "subcategory": "Baseball Cap",          "gender": "unisex", "prompt": "a fitted baseball cap in a solid colour, brim shaped naturally, worn straight or slightly tipped; casual streetwear energy"},
    "acc_headwear_beanie":               {"category": "Accessories", "partition": "Headwear",    "subcategory": "Beanie",                "gender": "unisex", "prompt": "a snug-fitting knitted beanie in a neutral colour, slight fold at the brim; cosy contemporary cool"},
    "acc_headwear_fedora":               {"category": "Accessories", "partition": "Headwear",    "subcategory": "Fedora",                "gender": "unisex", "prompt": "a wool felt fedora with a creased crown and a slim grosgrain band; sophisticated retro-modern silhouette"},
    "acc_headwear_widebrim_f":           {"category": "Accessories", "partition": "Headwear",    "subcategory": "Wide-Brim Hat",         "gender": "female", "prompt": "a wide-brim felt or straw hat with a structured crown, brim slightly downturned to frame the face; elegant editorial silhouette"},
    "acc_headwear_headband_f":           {"category": "Accessories", "partition": "Headwear",    "subcategory": "Headband",              "gender": "female", "prompt": "a fabric or velvet padded headband in a coordinating colour, smoothing the hair back from the face"},
    "acc_headwear_hijab_f":              {"category": "Accessories", "partition": "Headwear",    "subcategory": "Hijab / Headscarf",     "gender": "female", "prompt": "a softly draped silk or modal hijab in a flattering solid tone, neatly pinned, framing the face cleanly with a modest layered finish"},

    "acc_watch_none":                    {"category": "Accessories", "partition": "Watch",       "subcategory": "No Watch",              "gender": "unisex", "prompt": "no wristwatch visible"},
    "acc_watch_leather":                 {"category": "Accessories", "partition": "Watch",       "subcategory": "Leather Strap Dress",   "gender": "unisex", "prompt": "a refined dress wristwatch with a slim leather strap (black or cognac) and a minimalist dial; signals taste and timekeeping"},
    "acc_watch_steel":                   {"category": "Accessories", "partition": "Watch",       "subcategory": "Steel Bracelet",        "gender": "unisex", "prompt": "a polished stainless-steel link bracelet wristwatch with a clean dial; understated luxury"},
    "acc_watch_smart":                   {"category": "Accessories", "partition": "Watch",       "subcategory": "Sport Smartwatch",      "gender": "unisex", "prompt": "a modern fitness smartwatch with a silicone or woven sport strap and an active digital dial; tech-forward and functional"},
    "acc_watch_gold":                    {"category": "Accessories", "partition": "Watch",       "subcategory": "Gold Luxury",           "gender": "unisex", "prompt": "a luxury gold wristwatch with a sculpted bracelet and detailed dial; confident wealth signal"},

    "acc_tie_none":                      {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "No Tie / Scarf",        "gender": "unisex", "prompt": "no tie, scarf, or pocket square"},
    "acc_tie_slim_m":                    {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "Slim Necktie",          "gender": "male",   "prompt": "a slim silk necktie in a deep solid (navy, burgundy, forest), tied in a small neat knot"},
    "acc_tie_classic_m":                 {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "Classic Necktie",       "gender": "male",   "prompt": "a classic-width silk necktie in a confident colour or restrained pattern, tied in a clean Windsor knot"},
    "acc_tie_bow_m":                     {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "Bow Tie",               "gender": "male",   "prompt": "a black or deep-coloured silk bow tie, hand-tied, slightly asymmetrical; formal evening polish"},
    "acc_tie_silk_scarf_f":              {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "Silk Scarf",            "gender": "female", "prompt": "a square silk scarf knotted softly at the side of the neck, refined pattern, elegant editorial touch"},
    "acc_tie_pocket_sq_m":               {"category": "Accessories", "partition": "Tie / Scarf", "subcategory": "Pocket Square",         "gender": "male",   "prompt": "a folded silk pocket square peeking from the breast pocket of the jacket, coordinated tone-on-tone"},

    # ── Jewelry ──
    "jw_earrings_none":                  {"category": "Jewelry", "partition": "Earrings",  "subcategory": "No Earrings",            "gender": "unisex", "prompt": "no earrings"},
    "jw_earrings_studs":                 {"category": "Jewelry", "partition": "Earrings",  "subcategory": "Studs",                  "gender": "unisex", "prompt": "small refined stud earrings in gold or silver, single stones or simple polished discs"},
    "jw_earrings_drops_f":               {"category": "Jewelry", "partition": "Earrings",  "subcategory": "Drop Earrings",          "gender": "female", "prompt": "elegant drop earrings catching the light, scaled to the face; refined statement piece"},
    "jw_earrings_hoops_f":               {"category": "Jewelry", "partition": "Earrings",  "subcategory": "Hoop Earrings",          "gender": "female", "prompt": "polished mid-size hoop earrings in gold or silver, clean circular shape"},
    "jw_earrings_pearl_f":               {"category": "Jewelry", "partition": "Earrings",  "subcategory": "Pearl Earrings",         "gender": "female", "prompt": "classic pearl stud earrings with a soft natural lustre; timeless elegance"},
    "jw_earrings_single_m":              {"category": "Jewelry", "partition": "Earrings",  "subcategory": "Single Stud",            "gender": "male",   "prompt": "a single small stud earring in the lobe, polished metal or single dark stone; subtle confident accent"},

    "jw_necklace_none":                  {"category": "Jewelry", "partition": "Necklace",  "subcategory": "No Necklace",            "gender": "unisex", "prompt": "no necklace"},
    "jw_necklace_pendant_f":             {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Delicate Pendant",       "gender": "female", "prompt": "a delicate pendant necklace sitting just below the collarbone, fine chain, single small stone or simple charm"},
    "jw_necklace_choker_f":              {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Choker",                 "gender": "female", "prompt": "a fitted choker sitting high on the neck, slim metal or velvet, single small accent"},
    "jw_necklace_layered_f":             {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Layered Chains",         "gender": "female", "prompt": "two or three delicate gold or silver chains layered at different lengths, mixing simple pendants"},
    "jw_necklace_statement_f":           {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Statement Necklace",     "gender": "female", "prompt": "a bold statement necklace — sculptural shapes or coloured stones — as the focal point of the look"},
    "jw_necklace_chain_m":               {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Single Chain",           "gender": "male",   "prompt": "a single masculine chain in gold or silver, slim to medium thickness, resting on the collarbone"},
    "jw_necklace_pearls_f":              {"category": "Jewelry", "partition": "Necklace",  "subcategory": "Pearl Strand",           "gender": "female", "prompt": "a classic single-strand pearl necklace at the collarbone, soft lustre, refined formal touch"},

    "jw_bracelet_none":                  {"category": "Jewelry", "partition": "Bracelet",  "subcategory": "No Bracelet",            "gender": "unisex", "prompt": "no bracelet on the wrist"},
    "jw_bracelet_slim":                  {"category": "Jewelry", "partition": "Bracelet",  "subcategory": "Slim Chain Bracelet",    "gender": "unisex", "prompt": "a single delicate chain bracelet around the wrist, slim and refined"},
    "jw_bracelet_bangles_f":             {"category": "Jewelry", "partition": "Bracelet",  "subcategory": "Bangle Stack",           "gender": "female", "prompt": "a stack of slim metal bangles on one wrist, mixing gold and silver tones for movement and shine"},
    "jw_bracelet_leather_m":             {"category": "Jewelry", "partition": "Bracelet",  "subcategory": "Leather Cuff",           "gender": "male",   "prompt": "a brown or black braided leather cuff bracelet, slightly worn, masculine and grounded"},
    "jw_bracelet_gold_cuff_f":           {"category": "Jewelry", "partition": "Bracelet",  "subcategory": "Wide Gold Cuff",         "gender": "female", "prompt": "a wide polished gold cuff bracelet on one wrist, sculptural statement piece"},

    "jw_rings_none":                     {"category": "Jewelry", "partition": "Rings",     "subcategory": "No Visible Rings",       "gender": "unisex", "prompt": "no rings visible on the fingers"},
    "jw_rings_statement":                {"category": "Jewelry", "partition": "Rings",     "subcategory": "Single Statement Ring",  "gender": "unisex", "prompt": "a single sculptural statement ring on one finger, bold metal shape or coloured stone"},
    "jw_rings_stacked_f":                {"category": "Jewelry", "partition": "Rings",     "subcategory": "Stacked Bands",          "gender": "female", "prompt": "two or three delicate stacked bands on one finger in mixed metals, fine and contemporary"},
    "jw_rings_band":                     {"category": "Jewelry", "partition": "Rings",     "subcategory": "Wedding Band",           "gender": "unisex", "prompt": "a single simple wedding band on the ring finger, polished plain metal, restrained classic"},

    # ── Background ──
    "bg_setting_studio":                 {"category": "Background", "partition": "Setting",  "subcategory": "Clean Neutral Studio",  "gender": "unisex", "prompt": "a clean neutral studio backdrop in soft beige, grey, or off-white, smooth gradient lighting from one side, no distractions"},
    "bg_setting_office":                 {"category": "Background", "partition": "Setting",  "subcategory": "Modern Office",         "gender": "unisex", "prompt": "a modern corporate office interior — out-of-focus desks, glass-walled meeting rooms, warm ambient light suggesting upmarket professionalism"},
    "bg_setting_outdoor":                {"category": "Background", "partition": "Setting",  "subcategory": "Soft Outdoor Greenery", "gender": "unisex", "prompt": "a soft-focus outdoor environment of trees and greenery, dappled sunlight filtering through leaves, gentle bokeh"},
    "bg_setting_library":                {"category": "Background", "partition": "Setting",  "subcategory": "Warm Library",          "gender": "unisex", "prompt": "a warm wood-panelled library with floor-to-ceiling bookshelves, brass reading lamps, golden ambient light; scholarly atmosphere"},
    "bg_setting_gym":                    {"category": "Background", "partition": "Setting",  "subcategory": "Modern Gym",            "gender": "unisex", "prompt": "a clean modern gym interior with equipment subtly visible in the background, polished concrete or wood floor; motivating but uncluttered"},
    "bg_setting_cityscape":              {"category": "Background", "partition": "Setting",  "subcategory": "Urban Cityscape",       "gender": "unisex", "prompt": "a blurred urban cityscape seen through floor-to-ceiling glass, skyscrapers visible, golden-hour light spilling across the room"},
    "bg_setting_medical":                {"category": "Background", "partition": "Setting",  "subcategory": "Medical Office",        "gender": "unisex", "prompt": "a calm medical office interior — soft whites and pale blues, abstract clinical decor, a hint of equipment out of focus; reassuring atmosphere"},
    "bg_setting_gradient":               {"category": "Background", "partition": "Setting",  "subcategory": "Abstract Gradient",     "gender": "unisex", "prompt": "a smooth abstract gradient backdrop in calm dual tones (e.g. deep blue to soft purple), soft and aesthetic; suits creative branding"},
    "bg_setting_cafe":                   {"category": "Background", "partition": "Setting",  "subcategory": "Cafe / Coffee Shop",    "gender": "unisex", "prompt": "a warm cafe interior with wood tables, hanging pendant lights, baristas and patrons softly out of focus; intimate creative-class energy"},
    "bg_setting_home":                   {"category": "Background", "partition": "Setting",  "subcategory": "Home Living Room",      "gender": "unisex", "prompt": "a tasteful home interior with a soft armchair or sofa, framed art on a feature wall, warm side-lamp lighting, plants in the corners; relaxed personal warmth"},
    "bg_setting_conference":             {"category": "Background", "partition": "Setting",  "subcategory": "Conference Room",       "gender": "unisex", "prompt": "a sleek conference-room background — long polished table, leather chairs softly out of focus, glass walls, large screen; corporate gravitas"},
    "bg_setting_street":                 {"category": "Background", "partition": "Setting",  "subcategory": "Outdoor City Street",   "gender": "unisex", "prompt": "an out-of-focus city street background — passersby and storefronts blurred, soft daylight, urban energy"},
    "bg_setting_beach":                  {"category": "Background", "partition": "Setting",  "subcategory": "Beach / Coastal",       "gender": "unisex", "prompt": "a soft-focus coastal background — pale sand, gentle waves, hazy horizon, golden afternoon light; calm and elemental"},

    "bg_light_daylight":                 {"category": "Background", "partition": "Lighting", "subcategory": "Soft Daylight",         "gender": "unisex", "prompt": "soft diffused daylight overall lighting, even tones across the face, gentle shadows"},
    "bg_light_golden":                   {"category": "Background", "partition": "Lighting", "subcategory": "Golden Hour",           "gender": "unisex", "prompt": "warm golden-hour lighting from a low angle, amber highlights along the cheekbones, soft long shadows"},
    "bg_light_cool":                     {"category": "Background", "partition": "Lighting", "subcategory": "Cool Studio Light",     "gender": "unisex", "prompt": "cool neutral studio lighting from a softbox, even illumination, controlled shadows for a clean editorial finish"},
    "bg_light_dramatic":                 {"category": "Background", "partition": "Lighting", "subcategory": "Dramatic Side-Lit",     "gender": "unisex", "prompt": "dramatic high-contrast side lighting, one side of the face brightly lit while the opposite drops into shadow; cinematic mood"},
    "bg_light_tungsten":                 {"category": "Background", "partition": "Lighting", "subcategory": "Warm Tungsten",         "gender": "unisex", "prompt": "warm tungsten interior lighting, amber-orange cast, slightly hazy quality; intimate evening atmosphere"},
}

VOICE_PRESETS: dict[str, dict[str, str]] = {
    # ── Tone ──
    "tone_reg_deep_bass_m":     {"category": "Tone", "partition": "Register", "subcategory": "Deep Bass",         "gender": "male",   "prompt": "very deep chest-voice bass with powerful resonant low-frequency fundamentals, every word sitting heavily under a low ceiling; the floor of a movie-trailer voice"},
    "tone_reg_baritone_m":      {"category": "Tone", "partition": "Register", "subcategory": "Baritone",          "gender": "male",   "prompt": "rich baritone register sitting in the warm lower-mid frequencies, full chest support, deliberate weight in every syllable, classic radio-host gravitas"},
    "tone_reg_mid_m":           {"category": "Tone", "partition": "Register", "subcategory": "Mid-range Male",    "gender": "male",   "prompt": "neutral mid-range male voice, comfortably central pitch, no extreme depth or brightness, broadly approachable and easy to listen to"},
    "tone_reg_tenor_m":         {"category": "Tone", "partition": "Register", "subcategory": "Light Tenor",       "gender": "male",   "prompt": "light tenor register with bright forward placement, agile and youthful, a touch of natural air on the top notes"},
    "tone_reg_alto_f":          {"category": "Tone", "partition": "Register", "subcategory": "Alto",              "gender": "female", "prompt": "lower-pitched alto register with warm chest resonance, grounded and confident, slight smoky depth on sustained vowels"},
    "tone_reg_mid_f":           {"category": "Tone", "partition": "Register", "subcategory": "Mid-range Female",  "gender": "female", "prompt": "neutral mid-range female voice, balanced pitch with comfortable centre, neither bright nor husky, broadly approachable"},
    "tone_reg_soprano_f":       {"category": "Tone", "partition": "Register", "subcategory": "Bright Soprano",    "gender": "female", "prompt": "bright soprano register with airy forward placement, lift on the upper notes, youthful clarity and natural sparkle"},
    "tone_reg_androgynous":     {"category": "Tone", "partition": "Register", "subcategory": "Androgynous Mid",   "gender": "unisex", "prompt": "ambiguous mid-range register that reads as neither distinctly male nor female, balanced timbre, modern neutral feel"},

    "tone_warmth_warm":         {"category": "Tone", "partition": "Warmth",   "subcategory": "Warm",              "gender": "unisex", "prompt": "warm tonal quality with rounded vowels and full-bodied chest resonance, a slight vocal smile threaded through phrases, inviting and human"},
    "tone_warmth_crisp":        {"category": "Tone", "partition": "Warmth",   "subcategory": "Crisp & Clear",     "gender": "unisex", "prompt": "crisp clear tonal quality with sharply defined consonants, no breathiness or softness, broadcast-precise articulation"},
    "tone_warmth_soothing":     {"category": "Tone", "partition": "Warmth",   "subcategory": "Soothing",          "gender": "unisex", "prompt": "soothing tonal quality with gentle low overtones and softened consonants, breath-supported smoothness, designed to lower listener pulse"},
    "tone_warmth_authoritative":{"category": "Tone", "partition": "Warmth",   "subcategory": "Authoritative",     "gender": "unisex", "prompt": "authoritative tonal quality with weighted delivery and downward inflection on key phrases, no upspeak, commands attention without volume"},
    "tone_warmth_friendly":     {"category": "Tone", "partition": "Warmth",   "subcategory": "Friendly",          "gender": "unisex", "prompt": "friendly approachable tonal quality with frequent vocal smiles, slight bounce in pitch, warm conversational openness"},
    "tone_warmth_cool":         {"category": "Tone", "partition": "Warmth",   "subcategory": "Cool & Detached",   "gender": "unisex", "prompt": "cool detached tonal quality, even pitch with minimal warmth, slightly clinical neutrality; controlled and observational"},

    "tone_pace_slow":            {"category": "Tone", "partition": "Pace",    "subcategory": "Slow Deliberate",   "gender": "unisex", "prompt": "slow deliberate speaking pace with elongated vowels and long inter-phrase pauses, every word given full weight"},
    "tone_pace_measured":        {"category": "Tone", "partition": "Pace",    "subcategory": "Measured",          "gender": "unisex", "prompt": "measured pace, slightly slower than conversational, with controlled rhythm and intentional pauses at sentence breaks"},
    "tone_pace_conversational":  {"category": "Tone", "partition": "Pace",    "subcategory": "Conversational",    "gender": "unisex", "prompt": "natural conversational pace as if talking to one person in a room, fluid rhythm, gentle micro-pauses for breath"},
    "tone_pace_energetic":       {"category": "Tone", "partition": "Pace",    "subcategory": "Energetic / Quick", "gender": "unisex", "prompt": "energetic quick pace with forward-leaning syllables, minimal pauses, momentum that pulls the listener along"},
    "tone_pace_rapid":           {"category": "Tone", "partition": "Pace",    "subcategory": "Rapid-Fire",        "gender": "unisex", "prompt": "rapid-fire delivery with dense syllable packing, sharp consonants, almost auctioneer-fast in moments of excitement"},

    "tone_emotion_confident":    {"category": "Tone", "partition": "Emotion", "subcategory": "Confident",         "gender": "unisex", "prompt": "confident emotional colour — downward sentence endings, no hedging, posture-tall delivery, occasional brief assertive pauses"},
    "tone_emotion_empathetic":   {"category": "Tone", "partition": "Emotion", "subcategory": "Empathetic",        "gender": "unisex", "prompt": "empathetic emotional colour — softer vowels on tender words, gentle micro-pauses, slight downward melody that signals listening"},
    "tone_emotion_playful":      {"category": "Tone", "partition": "Emotion", "subcategory": "Playful",           "gender": "unisex", "prompt": "playful emotional colour — bright pitch variation, gentle laughs in the breath, occasional cheeky lifts on punchlines"},
    "tone_emotion_stern":        {"category": "Tone", "partition": "Emotion", "subcategory": "Stern",             "gender": "unisex", "prompt": "stern emotional colour — controlled flat melody, weighted consonants, no smile in the tone, no-nonsense gravity"},
    "tone_emotion_joyful":       {"category": "Tone", "partition": "Emotion", "subcategory": "Joyful",            "gender": "unisex", "prompt": "joyful emotional colour — bright melodic lifts, audible smiles, light energetic bounce on emphasised syllables"},
    "tone_emotion_contemplative":{"category": "Tone", "partition": "Emotion", "subcategory": "Contemplative",     "gender": "unisex", "prompt": "contemplative emotional colour — slow reflective pacing, soft uncertain rises mid-sentence, thoughtful trailing endings"},

    # ── Profession ──
    "prof_news_anchor":         {"category": "Profession", "partition": "Domain", "subcategory": "News Anchor",          "gender": "unisex", "prompt": "broadcast-trained news-anchor delivery — surgical diction, even broadcast rhythm, neutral regional coloring, precise short pauses for emphasis"},
    "prof_tech_presenter":      {"category": "Profession", "partition": "Domain", "subcategory": "Tech Presenter",       "gender": "unisex", "prompt": "tech-presenter delivery — clear modern intelligent pacing, fluid with technical jargon without sounding rehearsed, confident downward inflections at the end of statements, Apple-keynote energy"},
    "prof_healthcare":          {"category": "Profession", "partition": "Domain", "subcategory": "Healthcare Pro",       "gender": "unisex", "prompt": "healthcare-professional delivery — calm precise voice with reassuring softness on emotionally charged words, slow enough to let medical information land, gentle authority that defuses anxiety"},
    "prof_legal":               {"category": "Profession", "partition": "Domain", "subcategory": "Lawyer",               "gender": "unisex", "prompt": "lawyerly delivery — formal measured cadence with precise consonants and full vowels, gravity on every statement, no upspeak, no filler; every sentence sounds drafted before spoken"},
    "prof_finance":             {"category": "Profession", "partition": "Domain", "subcategory": "Financial Advisor",    "gender": "unisex", "prompt": "financial-advisor delivery — composed trustworthy voice with controlled energy, light analytical edge, never alarmist, always grounded; explains complex numbers without making the listener panic"},
    "prof_educator":            {"category": "Profession", "partition": "Domain", "subcategory": "Educator",             "gender": "unisex", "prompt": "educator delivery — patient articulate cadence (sentence, pause, recap), gentle warmth, slight rising tone on questions to invite the listener in, pronounced consonants for note-taking clarity"},
    "prof_customer_service":    {"category": "Profession", "partition": "Domain", "subcategory": "Customer Service",     "gender": "unisex", "prompt": "customer-service delivery — bright polite infinitely patient voice with a clear smile in delivery, scripted-but-natural rhythm, never rushed; the voice of someone genuinely trying to help"},
    "prof_retail":              {"category": "Profession", "partition": "Domain", "subcategory": "Retail Sales",         "gender": "unisex", "prompt": "retail-sales delivery — warm welcoming voice with persuasive forward motion, helpful but not pushy, gentle enthusiasm on product details, conversational rhythm"},
    "prof_ai_assistant":        {"category": "Profession", "partition": "Domain", "subcategory": "AI Assistant",         "gender": "unisex", "prompt": "AI-assistant delivery — neutral clear modern synthetic-friendly voice, uncluttered articulation, balanced pitch, no strong regional flavour; helpful without sounding eager"},
    "prof_coach":               {"category": "Profession", "partition": "Domain", "subcategory": "Coach / Trainer",      "gender": "unisex", "prompt": "coach delivery — energetic motivating voice with crisp athletic edge, quick syllables on counts, encouraging warmth between sets, push-when-needed firmness"},
    "prof_documentary":         {"category": "Profession", "partition": "Domain", "subcategory": "Documentary Narrator", "gender": "unisex", "prompt": "documentary-narrator delivery — cinematic measured baritone or alto, slow rich resonant pacing, pauses are part of the performance, observational gravity"},
    "prof_tour_guide":          {"category": "Profession", "partition": "Domain", "subcategory": "Tour Guide",           "gender": "unisex", "prompt": "tour-guide delivery — engaging conversational voice with enthusiastic emphasis on interesting details, easy storytelling rhythm, welcoming inclusive 'we' framing"},
    "prof_radio_host":          {"category": "Profession", "partition": "Domain", "subcategory": "Radio Host",           "gender": "unisex", "prompt": "radio-host delivery — warm intimate one-on-one tone, fluid scripted-but-natural flow, smooth segues between topics, occasional vocal smiles"},
    "prof_podcaster":           {"category": "Profession", "partition": "Domain", "subcategory": "Podcaster",            "gender": "unisex", "prompt": "podcaster delivery — relaxed conversational voice, intimate close-mic feel, natural ums and breath kept in, leaning into curiosity and pause"},

    # ── Persona ──
    "persona_archetype_storyteller":   {"category": "Persona", "partition": "Archetype", "subcategory": "Storyteller",          "gender": "unisex", "prompt": "storyteller archetype — expressive dramatic voice that paints scenes with pace and dynamics, whispers when intimate and lifts when revelatory, trusts silences; could narrate a fairytale or a true-crime cold open with equal command"},
    "persona_archetype_best_friend":   {"category": "Persona", "partition": "Archetype", "subcategory": "Best Friend",          "gender": "unisex", "prompt": "best-friend archetype — warm familiar voice that feels like a close friend on a phone call, frequent gentle laughs, conversational shortcuts, low judgement"},
    "persona_archetype_wise_mentor":   {"category": "Persona", "partition": "Archetype", "subcategory": "Wise Mentor",          "gender": "unisex", "prompt": "wise-mentor archetype — calm grounded voice carrying earned authority, asks gently probing questions, refrains from prescribing; trust through patience"},
    "persona_archetype_stern_auth":    {"category": "Persona", "partition": "Archetype", "subcategory": "Stern Authority",      "gender": "unisex", "prompt": "stern-authority archetype — controlled severe voice with no warmth in the tone, weighted statements, expectation of compliance; courtroom or principal energy"},
    "persona_archetype_therapist":     {"category": "Persona", "partition": "Archetype", "subcategory": "Therapist / Counselor","gender": "unisex", "prompt": "therapist archetype — soft measured empathetic voice with mindful pauses, never crosses into clinical detachment; lets silence do its work and reflects feelings back gently"},
    "persona_archetype_wise_elder":    {"category": "Persona", "partition": "Archetype", "subcategory": "Wise Elder",           "gender": "unisex", "prompt": "wise-elder archetype — slow thoughtful voice with gentle gravel, like a grandparent telling a story by the fire, warm humour occasionally lifting the pace; every sentence carries lived experience"},
    "persona_archetype_executive":     {"category": "Persona", "partition": "Archetype", "subcategory": "Executive",            "gender": "unisex", "prompt": "executive archetype — polished commanding voice with composed deliberate sentences, pauses that signal thought rather than hesitation, boardroom presence even on a hands-free"},
    "persona_archetype_trickster":     {"category": "Persona", "partition": "Archetype", "subcategory": "Trickster / Comedian", "gender": "unisex", "prompt": "trickster archetype — quick-witted voice with playful misdirections, sudden pitch lifts on punchlines, mischievous timing, charismatic unpredictability"},
    "persona_archetype_hero":          {"category": "Persona", "partition": "Archetype", "subcategory": "Hero / Inspirational", "gender": "unisex", "prompt": "hero archetype — uplifting confident voice with rising melodic shape on aspirational phrases, full chest support, deliberate weight on motivational keywords"},
    "persona_archetype_romantic":      {"category": "Persona", "partition": "Archetype", "subcategory": "Romantic Lead",        "gender": "unisex", "prompt": "romantic-lead archetype — intimate breath-supported voice with slow lingering vowels, slightly lowered volume that pulls the listener closer, charm laced into the consonants"},

    "persona_energy_calm":             {"category": "Persona", "partition": "Energy",    "subcategory": "Calm & Centered",      "gender": "unisex", "prompt": "calm centred energy — low arousal, steady breath, evenly distributed weight across syllables; the listener feels held"},
    "persona_energy_steady":           {"category": "Persona", "partition": "Energy",    "subcategory": "Steady",               "gender": "unisex", "prompt": "steady mid-energy — neither sleepy nor amped, predictable rhythm, reliable delivery without sudden spikes"},
    "persona_energy_energetic":        {"category": "Persona", "partition": "Energy",    "subcategory": "Energetic",            "gender": "unisex", "prompt": "energetic delivery — quick forward syllables, occasional bursts of enthusiasm, slight upward melodic motion that signals momentum"},
    "persona_energy_high_octane":      {"category": "Persona", "partition": "Energy",    "subcategory": "High-Octane",          "gender": "unisex", "prompt": "high-octane delivery — maximum energy, rapid syllables, big dynamic swings, occasional vocal extension on climactic words; sports-commentator intensity"},
    "persona_energy_whispery":         {"category": "Persona", "partition": "Energy",    "subcategory": "Whispery & Intimate",  "gender": "unisex", "prompt": "whispery intimate delivery — close-mic breathiness, very low volume floor, slow lingering vowels; ASMR-adjacent closeness"},
    "persona_energy_dramatic":         {"category": "Persona", "partition": "Energy",    "subcategory": "Dramatic",             "gender": "unisex", "prompt": "dramatic delivery — wide dynamic range from whisper to swell, theatrical timing, long meaningful pauses, conscious of every breath"},

    # ── Accent ──
    "acc_region_american":          {"category": "Accent", "partition": "Region", "subcategory": "American Standard",        "gender": "unisex", "prompt": "neutral mid-Atlantic American English accent, no strong regional flavour, broad broadcast-friendly pronunciation"},
    "acc_region_british_rp":        {"category": "Accent", "partition": "Region", "subcategory": "British RP",               "gender": "unisex", "prompt": "received-pronunciation British accent, crisp dental consonants, refined non-rhotic vowels, BBC-presenter polish"},
    "acc_region_cockney":           {"category": "Accent", "partition": "Region", "subcategory": "British Cockney",          "gender": "unisex", "prompt": "London Cockney accent, glottal stops, dropped t's and h's, working-class East-End rhythm, lively and characterful"},
    "acc_region_scottish":          {"category": "Accent", "partition": "Region", "subcategory": "Scottish",                 "gender": "unisex", "prompt": "Scottish accent with rolled r's, lilted melodic intonation, distinct vowel substitutions, warm Edinburgh / Glasgow flavour"},
    "acc_region_irish":             {"category": "Accent", "partition": "Region", "subcategory": "Irish",                    "gender": "unisex", "prompt": "Irish accent with musical lilt, rounded vowels, light rolling r's, warm conversational openness; Dublin or rural Ireland"},
    "acc_region_australian":        {"category": "Accent", "partition": "Region", "subcategory": "Australian",               "gender": "unisex", "prompt": "Australian English accent, rising terminal intonation on statements, broad open vowels, relaxed casual feel"},
    "acc_region_nz":                {"category": "Accent", "partition": "Region", "subcategory": "New Zealand",              "gender": "unisex", "prompt": "New Zealand English accent, slightly clipped vowels (esp. short i shifted toward 'u'), polite measured pacing"},
    "acc_region_indian":            {"category": "Accent", "partition": "Region", "subcategory": "Indian English",           "gender": "unisex", "prompt": "Indian English accent with retroflex consonants, syllable-timed rhythm, formal melodic intonation, slight Hindi-influenced vowel colouring"},
    "acc_region_south_african":     {"category": "Accent", "partition": "Region", "subcategory": "South African",            "gender": "unisex", "prompt": "South African English accent, distinctive vowel shifts, slightly clipped consonants, warm narrative cadence"},
    "acc_region_southern_us":       {"category": "Accent", "partition": "Region", "subcategory": "Southern US",              "gender": "unisex", "prompt": "Southern US accent with drawled vowels, dropped final r's, warm conversational pace, gentle melodic rises"},
    "acc_region_new_york":          {"category": "Accent", "partition": "Region", "subcategory": "New York",                 "gender": "unisex", "prompt": "New York accent with assertive forward placement, dropped post-vocalic r's in some words, quick urban rhythm, classic Brooklyn or Bronx feel"},
    "acc_region_california":        {"category": "Accent", "partition": "Region", "subcategory": "California Surfer",        "gender": "unisex", "prompt": "Californian surfer / valley accent, relaxed elongated vowels, frequent 'like' and 'totally' rhythm, light upward inflection at sentence endings"},
    "acc_region_spanish":           {"category": "Accent", "partition": "Region", "subcategory": "Spanish-Accented English", "gender": "unisex", "prompt": "Spanish-accented English with rolled r's, pure vowels (not diphthongised), syllable-timed rhythm, warm Latin musicality"},
    "acc_region_french":            {"category": "Accent", "partition": "Region", "subcategory": "French-Accented English",  "gender": "unisex", "prompt": "French-accented English with uvular r's, soft nasal vowels, occasional 'h' dropping, melodic Parisian phrasing"},
    "acc_region_german":            {"category": "Accent", "partition": "Region", "subcategory": "German-Accented English",  "gender": "unisex", "prompt": "German-accented English with hard precise consonants, occasional 'v' for 'w' substitutions, measured rhythmic delivery"},
    "acc_region_russian":           {"category": "Accent", "partition": "Region", "subcategory": "Russian-Accented English", "gender": "unisex", "prompt": "Russian-accented English with low-set voice, rolled or trilled r's, harder consonants, dropped articles, weighted deliberate pacing"},
    "acc_region_japanese":          {"category": "Accent", "partition": "Region", "subcategory": "Japanese-Accented English","gender": "unisex", "prompt": "Japanese-accented English with rhythmic mora-timed syllables, soft 'l/r' merging, occasional vowel insertion at word ends, polite measured intonation"},

    # ── Delivery ──
    "del_diction_crisp":            {"category": "Delivery", "partition": "Diction",       "subcategory": "Crisp Articulation",   "gender": "unisex", "prompt": "crisp precise articulation — sharply defined consonants, fully pronounced word endings, no shortcuts; the audio is unmistakably clear"},
    "del_diction_relaxed":          {"category": "Delivery", "partition": "Diction",       "subcategory": "Relaxed Conversational","gender": "unisex", "prompt": "relaxed natural articulation — softened consonants, easily flowing into the next word, the way someone speaks comfortably across a kitchen table"},
    "del_diction_hyper":            {"category": "Delivery", "partition": "Diction",       "subcategory": "Hyper-Articulate",     "gender": "unisex", "prompt": "hyper-articulate diction — every consonant overstated, every vowel cleanly opened, near-theatrical clarity"},
    "del_diction_casual":           {"category": "Delivery", "partition": "Diction",       "subcategory": "Casual Slangy",        "gender": "unisex", "prompt": "casual slangy diction — dropped g's on -ing words, contractions everywhere, occasional swallowed syllables; conversational and unpretentious"},

    "del_inflect_flat":             {"category": "Delivery", "partition": "Inflection",    "subcategory": "Flat / Even",          "gender": "unisex", "prompt": "flat even inflection with minimal pitch variation, controlled deadpan-adjacent melody; deliberate and unsentimental"},
    "del_inflect_melodic":          {"category": "Delivery", "partition": "Inflection",    "subcategory": "Melodic Rise-Fall",    "gender": "unisex", "prompt": "natural melodic rise-fall inflection across each phrase, gentle musical contour, easy to follow and emotionally legible"},
    "del_inflect_dramatic":         {"category": "Delivery", "partition": "Inflection",    "subcategory": "Dramatic Range",       "gender": "unisex", "prompt": "dramatic wide-range inflection swinging from low whisper to lifted high notes, theatrical melodic shape, expressive performance"},
    "del_inflect_subtle":           {"category": "Delivery", "partition": "Inflection",    "subcategory": "Subtle Variation",     "gender": "unisex", "prompt": "subtle restrained inflection with small intentional pitch shifts on emphasised words, otherwise even melody"},
    "del_inflect_upspeak":          {"category": "Delivery", "partition": "Inflection",    "subcategory": "Upspeak Pattern",      "gender": "unisex", "prompt": "habitual upspeak — terminal upward rise at the end of statements as if asking permission or checking in; reads as young / collaborative"},

    "del_texture_smooth":           {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Smooth Clear",         "gender": "unisex", "prompt": "smooth clear vocal texture — no breathiness, no rasp, evenly produced tone across the register, polished radio-clean finish"},
    "del_texture_breathy":          {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Breathy",              "gender": "unisex", "prompt": "breathy vocal texture with audible airflow under each phrase, soft intimate close-mic feel; gentle and exposed"},
    "del_texture_gravelly":         {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Gravelly / Raspy",     "gender": "unisex", "prompt": "gravelly raspy vocal texture with low rumble in sustained tones, weathered character, slight grit at phrase endings"},
    "del_texture_fry":              {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Slight Vocal Fry",     "gender": "unisex", "prompt": "subtle vocal fry at the ends of phrases — that low creaky register young-millennial voices often drop into; conversational and current"},
    "del_texture_smile":            {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Vocal Smile",          "gender": "unisex", "prompt": "audible smile threaded through the entire delivery — slightly stretched vowels and lifted resonance that signal warmth even when the words are neutral"},
    "del_texture_husky":            {"category": "Delivery", "partition": "Vocal Texture", "subcategory": "Husky",                "gender": "unisex", "prompt": "husky lower-register vocal texture with a thicker resonant quality, slight smokiness, intimate confidential weight"},
}

# Avatar category order for the prompt builder — keeps assembled prompts
# stable regardless of selection order.
AVATAR_CATEGORY_ORDER: tuple[str, ...] = (
    "Outfit", "Hair", "Facial Hair", "Makeup", "Accessories", "Jewelry", "Background",
)


def _resolve_presets(
    preset_ids: list[str], library: dict[str, dict[str, str]], kind: str
) -> list[dict[str, str]]:
    """Validate a list of preset ids against ``library`` and return their
    resolved rows in input order.

    Raises ``HTTPException(400)`` if any id is unknown OR if two ids
    target the same ``(category, partition)`` slot (mutual-exclusivity
    rule). The ``kind`` argument ("avatar" / "voice") only affects the
    error message.
    """
    rows: list[dict[str, str]] = []
    seen_slots: dict[tuple[str, str], str] = {}
    for pid in preset_ids:
        p = library.get(pid)
        if p is None:
            raise HTTPException(
                status_code=400, detail=f"Unknown {kind} preset: {pid}"
            )
        slot = (p["category"], p["partition"])
        if slot in seen_slots:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Conflicting {kind} presets in partition "
                    f"'{p['category']} / {p['partition']}': "
                    f"{seen_slots[slot]!r} and {pid!r} cannot both be selected."
                ),
            )
        seen_slots[slot] = pid
        rows.append(p)
    return rows


def build_avatar_edit_prompt(preset_ids: list[str], custom_prompt: str = "") -> str:
    """Assemble the avatar-edit instruction sent to the image LLM.

    Validates the selection (at most one preset per ``(category,
    partition)`` slot — see :func:`_resolve_presets`), then groups the
    resulting prompts by category. Categories are emitted in
    :data:`AVATAR_CATEGORY_ORDER` so the assembled prompt is stable
    regardless of the order the user clicked the chips.
    """
    if not preset_ids and not custom_prompt.strip():
        return ""

    rows = _resolve_presets(preset_ids, AVATAR_PRESETS, kind="avatar")

    grouped: dict[str, list[str]] = {}
    for p in rows:
        grouped.setdefault(p["category"], []).append(p["prompt"])

    preset_sections: list[str] = []
    for cat in AVATAR_CATEGORY_ORDER:
        if cat in grouped:
            preset_sections.append(f"- {cat}: {', '.join(grouped[cat])}")
    custom = custom_prompt.strip()
    if not preset_sections and not custom:
        return ""

    has_background = "Background" in grouped
    parts: list[str] = []

    # Top-of-prompt framing.
    parts.append(
        "Edit the provided portrait of a real person. Treat the source "
        "image as the ground truth identity — every change below is a "
        "wardrobe / styling / setting overlay on top of the SAME person."
    )

    # Identity preservation — non-negotiable, applied first.
    parts.append(
        "IDENTITY LOCK — the following must remain pixel-faithful to the "
        "source image and may NOT be altered by any preset or custom "
        "prompt below:\n"
        "- Facial features: face shape, eyes (colour, shape, gaze), nose, "
        "mouth, lips, chin, jawline, ears, brow ridge, and overall "
        "expression must match the source\n"
        "- Skin tone, complexion, freckles, moles, scars, and natural "
        "skin texture\n"
        "- Age appearance and bone structure / facial proportions\n"
        "- Ethnic and cultural presentation\n"
        "- Inherent head shape, hairline position, and ear placement\n"
        + ("" if has_background else "- The original background and environment (preserve unchanged)\n")
        + "If a styling instruction below would require changing any of "
        "the above, reinterpret it as a non-identity styling change "
        "instead. The person in the output must be unmistakably the same "
        "person as in the input."
    )

    # Styling instructions.
    if preset_sections:
        parts.append("STYLING CHANGES — apply only what is listed:\n" + "\n".join(preset_sections))

    # Custom prompt with explicit override authority over presets.
    if custom:
        parts.append(
            "CUSTOM USER DIRECTION (HIGHEST PRIORITY) — this instruction "
            "supersedes any styling above on conflict:\n"
            f"  {custom}\n"
            "If the user direction contradicts a preset line (e.g. user "
            "asks for 'no hat' but a preset listed a baseball cap), drop "
            "the conflicting preset line and follow the user direction. "
            "The IDENTITY LOCK above still cannot be overridden."
        )

    # Output-format constraints.
    parts.append(
        "OUTPUT RULES:\n"
        "- Photorealistic output only — no illustration, painting, or "
        "cartoon style\n"
        "- Maintain natural lighting consistent with the source\n"
        "- Keep the original portrait framing, aspect ratio, and "
        "head-to-shoulder crop\n"
        "- Apply ONLY the styling changes listed above; everything not "
        "explicitly changed stays identical to the source"
    )

    return "\n\n".join(parts)


# Voice category order — keeps assembled voice briefs stable regardless of
# the order the user clicked the chips.
VOICE_CATEGORY_ORDER: tuple[str, ...] = (
    "Tone", "Profession", "Persona", "Accent", "Delivery",
)


# ElevenLabs Voice Design caps ``voice_description`` at 1000 chars. We
# stay under that with headroom so the wire layer's defence-in-depth
# truncation never has to kick in for legitimate selections.
_VOICE_DESIGN_LIMIT = 990


def build_voice_design_brief(
    preset_ids: list[str], user_prompt: str = ""
) -> str:
    """Assemble the voice-design brief sent to ElevenLabs.

    Validates partition exclusivity (see :func:`_resolve_presets`), then
    concatenates the selected prompts in :data:`VOICE_CATEGORY_ORDER`,
    semicolon-joined, with the optional ``user_prompt`` appended last.

    ElevenLabs accepts at most 1000 characters of voice description, so:

      1. Try the **full assembly** first — every preset's complete
         flavour paragraph.
      2. If that overflows, **compress** by taking only the lead clause
         of each preset prompt (everything before the first ``;``).
         Each preset's prompt is intentionally written so the lead
         clause carries the essential descriptor.
      3. If even the compressed assembly overflows (very long user
         prompt + many presets), hard-truncate at the last ``;``
         boundary under the limit.

    The compression is transparent to the caller — same return type, same
    contract; ElevenLabs is just guaranteed to receive a valid request.
    """
    if not preset_ids and not user_prompt.strip():
        return user_prompt.strip()

    rows = _resolve_presets(preset_ids, VOICE_PRESETS, kind="voice")
    grouped: dict[str, list[str]] = {}
    for p in rows:
        grouped.setdefault(p["category"], []).append(p["prompt"])

    def assemble(reduce_one) -> str:
        parts: list[str] = []
        for cat in VOICE_CATEGORY_ORDER:
            if cat in grouped:
                parts.extend(reduce_one(prompt) for prompt in grouped[cat])
        if user_prompt.strip():
            parts.append(user_prompt.strip())
        return "; ".join(parts)

    full = assemble(lambda p: p)
    if len(full) <= _VOICE_DESIGN_LIMIT:
        return full

    compressed = assemble(lambda p: p.split(";", 1)[0].strip())
    if len(compressed) <= _VOICE_DESIGN_LIMIT:
        return compressed

    # Last resort: chop at the last ``;`` under the limit so we don't
    # mid-sentence-truncate a clause.
    cut = compressed.rfind(";", 0, _VOICE_DESIGN_LIMIT)
    if cut > 0:
        return compressed[:cut].rstrip()
    return compressed[:_VOICE_DESIGN_LIMIT].rstrip()


# Generic fallback used by the default-voice preview route and any
# synthesis call where no voice description is available. Per-voice
# previews are driven by ``ai_router.generate_voice_preview_text`` which
# produces a custom showcase line from the voice description.
VOICE_PREVIEW_TEXT = (
    "Hello — it's nice to meet you."
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


def _local_blob_to_base64_url(url: str) -> str:
    if not url or "/local-blob/" not in url:
        return url
    try:
        import base64
        from app.local_storage import LocalBlobStore
        key = url.split("/local-blob/", 1)[1].split("?", 1)[0]
        store = LocalBlobStore(tenant_id=settings.local_tenant_id)
        data, content_type = store.read_local(key)
        encoded = base64.b64encode(data).decode("utf-8")
        return f"data:{content_type};base64,{encoded}"
    except Exception:
        return url


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
        image_url=_local_blob_to_base64_url(row.image_url),
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
        created_at=row.created_at,
    )


def _persona_row_to_model(
    row: PersonaRow, avatars: list[S.PersonaAvatar]
) -> S.PersonaEntity:
    return S.PersonaEntity(
        id=row.id,
        name=row.name,
        image_url=_local_blob_to_base64_url(row.image_url),
        gender=row.gender,
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
        voice_ref_id=row.voice_ref_id,
        voice_provider=row.voice_provider,
        voice_id=row.voice_id,
        voice_source=row.voice_source,
        voice_description=row.voice_description,
        voice_sample_url=_local_blob_to_base64_url(row.voice_sample_url),
        voice_preview_url=_local_blob_to_base64_url(row.voice_preview_url),
        voice_status=row.voice_status,
        voice_last_error=row.voice_last_error,
        avatars=avatars,
        created_at=row.created_at,
    )


def _voice_row_to_model(row: VoiceRow) -> S.VoiceEntity:
    return S.VoiceEntity(
        id=row.id,
        name=row.name,
        provider=row.provider,
        voice_id=row.voice_id,
        source=row.source,
        description=row.description,
        sample_url=_local_blob_to_base64_url(row.sample_url),
        preview_url=_local_blob_to_base64_url(row.preview_url),
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
        created_at=row.created_at,
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
      ``http://host/local-blob/{key}`` (no tenant id in the path; the
      local fallback only ever uses one tenant).
    * **Azure** — URLs look like
      ``https://{account}.blob.core.windows.net/{container}/{key}``,
      reconstructed from the tenant's secrets.

    Returns ``None`` when the URL doesn't match either pattern (defensive
    — e.g. an externally-hosted preview URL stored on a row).
    """
    if not url:
        return None
    marker = "/local-blob/"
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


@app.get("/presets/avatar", response_model=list[S.Preset])
def get_avatar_presets() -> list[S.Preset]:
    """Curated avatar presets library."""
    return [S.Preset(id=pid, **meta) for pid, meta in AVATAR_PRESETS.items()]


@app.get("/presets/voice", response_model=list[S.Preset])
def get_voice_presets() -> list[S.Preset]:
    """Curated voice presets library."""
    return [S.Preset(id=pid, **meta) for pid, meta in VOICE_PRESETS.items()]


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
        logger.error("❌ [Avatar %s] Simli face generation failed: %s", avatar.id, status_response)
        avatar.status = "failed"
        avatar.last_error = str(status_response)
    else:
        logger.info("🎉 [Avatar %s] Simli face generation completed successfully! Face ID %s is now ready for use.", avatar.id, ready_face_id)
        avatar.status = "ready"
        avatar.face_id = ready_face_id
        avatar.last_error = None
        await repo.update_assistants_face_id(ctx.session, avatar.id, ready_face_id)
    await ctx.session.flush()
    return avatar


async def _poll_avatar_until_ready(tenant_id: str, avatar_id: int) -> None:
    """Background poller: hits Simli every 7s until the face generation finishes."""
    while True:
        try:
            async with open_background_context(tenant_id) as ctx:
                avatar = await repo.get_persona_avatar(ctx.session, avatar_id)
                if avatar is None:
                    logger.warning("⚠️ [Avatar %s] Polling stopped: Avatar not found in database", avatar_id)
                    return
                if avatar.status == "cancelled":
                    logger.warning("⚠️ [Avatar %s] Polling stopped: Avatar has been cancelled", avatar_id)
                    return
                logger.info("⏳ [Avatar %s] Polling Simli face generation status for face_id %s...", avatar_id, avatar.face_id)
                avatar = await _refresh_avatar_status(ctx, avatar)
                if avatar.status != "processing":
                    return
        except Exception as exc:
            logger.warning("⏳ [Avatar %s] Transient poller failure (retrying in 7s): %s", avatar_id, exc)
            pass
        await asyncio.sleep(7)


# ── Personas ──────────────────────────────────────────────────────────────────

@app.get("/{tenant_id}/personas", response_model=list[S.PersonaListEntity])
async def list_personas(
    ctx: TenantContext = Depends(tenant_ctx),
    gender: str | None = Query(default=None),
    status: str | None = Query(default=None),
    voice_status: str | None = Query(default=None),
    voice_provider: str | None = Query(default=None),
    voice_ref_id: int | None = Query(default=None),
    has_avatars: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.PersonaListEntity]:
    personas = await repo.list_persona_entities(
        ctx.session,
        gender=gender,
        status=status,
        voice_status=voice_status,
        voice_provider=voice_provider,
        voice_ref_id=voice_ref_id,
        limit=limit,
        offset=offset,
    )
    res = []
    for p in personas:
        cnt = await repo.count_avatars_for_persona(ctx.session, p.id)
        if has_avatars is not None:
            if has_avatars and cnt == 0:
                continue
            if not has_avatars and cnt > 0:
                continue
        res.append(
            S.PersonaListEntity(
                id=p.id,
                name=p.name,
                image_url=_local_blob_to_base64_url(p.image_url),
                created_at=p.created_at,
                status=p.status,
                gender=p.gender or "unknown",
                avatar_count=cnt,
            )
        )
    return res


@app.get("/{tenant_id}/personas/{persona_id}", response_model=S.PersonaEntity)
async def get_persona(
    persona_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.PersonaEntity:
    persona = await repo.get_persona_entity(ctx.session, persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    avatars = await repo.list_persona_avatars(ctx.session, persona_id=persona_id)
    for av in avatars:
        if av.status == "processing":
            await _refresh_avatar_status(ctx, av)
    return _persona_row_to_model(persona, [_avatar_row_to_model(av) for av in avatars])


@app.post(
    "/{tenant_id}/personas", response_model=S.PersonaEntity, status_code=201
)
async def create_persona(
    persona_image: UploadFile = File(...),
    name: str = Form("Persona"),
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
) -> S.PersonaEntity:
    image_bytes = await persona_image.read()
    upload_filename = persona_image.filename or "persona.png"
    upload_content_type = persona_image.content_type
    # Re-construct a thin shim so the validator sees the same shape it expects.
    class _Shim:
        def __init__(self, content_type: str | None) -> None:
            self.content_type = content_type
    _validate_image_upload(_Shim(upload_content_type), image_bytes)  # type: ignore[arg-type]

    logger.info("📸 [Persona] Inserting persona entity into database with name '%s'...", name)
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
        logger.info("📸 [Persona %s] Uploading source image (size: %d bytes)...", persona_id, len(image_bytes))
        image_url = await _upload_image(
            ctx,
            f"personas/{persona_id}/source",
            image_bytes,
            ext=os.path.splitext(upload_filename)[1].lower() or None,
        )
        await repo.update_persona_entity(
            ctx.session, persona_id, image_url=image_url
        )
        logger.info("📸 [Persona %s] Source image successfully uploaded! URL: %s", persona_id, image_url)

    logger.info("📸 [Persona %s] Launching background analysis task...", persona_id)
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
    """Background task — one strict-structured-output vision call.

    On success the persona flips to ``ready`` with the analysed gender +
    voice description. On any failure (transport, refusal, schema
    violation) the persona flips to ``failed`` with the error message
    captured in ``last_error`` — callers can delete and re-create.
    """
    logger.info("📸 [Persona %s] Start portrait analysis using %s provider...", persona_id, settings.gender_provider)
    try:
        analysis = await ai_router.analyse_persona(image_bytes)
    except Exception as exc:
        logger.error("❌ [Persona %s] Portrait analysis failed: %s", persona_id, exc)
        async with open_background_context(tenant_id) as ctx:
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                status="failed",
                stage="failed",
                last_error=f"persona analysis failed: {exc}",
            )
        return

    async with open_background_context(tenant_id) as ctx:
        await repo.update_persona_entity(
            ctx.session,
            persona_id,
            gender=analysis.gender,
            voice_description=analysis.voice_description.strip(),
            status="ready",
            stage="ready",
            progress=100,
            last_error=None,
        )
    logger.info("✅ [Persona %s] Portrait analysis succeeded! Gender determined: %s", persona_id, analysis.gender)


@app.get("/{tenant_id}/personas/{persona_id}/status", response_model=S.StatusResponse)
async def persona_status(
    persona_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.StatusResponse:
    row = await repo.get_persona_entity(ctx.session, persona_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    return S.StatusResponse(
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        last_error=row.last_error,
    )


@app.get("/{tenant_id}/personas/{persona_id}/cascade-count")
async def persona_cascade_count(
    persona_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    return {
        "avatars": await repo.count_avatars_for_persona(ctx.session, persona_id),
        "assistants": await repo.count_assistants_for_persona(ctx.session, persona_id),
    }


@app.patch("/{tenant_id}/personas/{persona_id}", response_model=S.PersonaEntity)
async def patch_persona(
    persona_id: int,
    payload: S.PersonaPatchRequest,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.PersonaEntity:
    persona = await repo.get_persona_entity(ctx.session, persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="Persona not found")
    fields: dict[str, Any] = {}
    if payload.name is not None:
        fields["name"] = payload.name
    if "voice_ref_id" in payload.model_fields_set:
        if payload.voice_ref_id is None or payload.voice_ref_id == 0:
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


@app.delete("/{tenant_id}/personas/{persona_id}", status_code=200)
async def delete_persona(
    persona_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    """Delete a persona row + its source portrait blob.

    The only asset uniquely owned by a persona is the source image
    uploaded when the persona was created. Voice, avatar, and assistant
    rows that referenced this persona survive — FK
    ``ON DELETE SET NULL`` nulls their ``persona_id`` link. Voice
    resources (ElevenLabs voice, sample/preview blobs) are NOT touched;
    they're independent and only removed via ``DELETE /voices/{id}``.
    """
    persona = await repo.get_persona_entity(ctx.session, persona_id)
    if persona is None:
        logger.error("❌ [Persona %s] Deletion failed: Persona not found", persona_id)
        raise HTTPException(status_code=404, detail="Persona not found")
    logger.info("📸 [Persona %s] Deleting persona '%s'...", persona_id, persona.name)
    if persona.image_url:
        logger.info("📸 [Persona %s] Deleting source portrait blob: %s...", persona_id, persona.image_url)
        await _delete_blob_by_url(ctx, persona.image_url)
    deleted = await repo.delete_persona_entity(ctx.session, persona_id)
    logger.info("✅ [Persona %s] Persona successfully deleted!", persona_id)
    return {"deleted": deleted}


# ── Persona voice (design / clone / clear) ────────────────────────────────────


async def _store_voice_preview(
    api_key: str,
    voice_id: str,
    *,
    text: str = "",
) -> bytes | None:
    """Synthesize an MP3 preview clip of a finished ElevenLabs voice.

    ``text`` is the line the voice will say in the preview — pass a
    per-voice line generated by
    ``ai_router.generate_voice_preview_text`` so the showcase line fits
    the voice's character. Falls back to ``VOICE_PREVIEW_TEXT`` when no
    line is supplied (e.g. the env-default preview route).
    """
    try:
        mp3 = await elevenlabs_client.synthesize(
            api_key,
            voice_id=voice_id,
            text=text.strip() or VOICE_PREVIEW_TEXT,
            model_id=settings.tts_model,
        )
    except ElevenLabsError:
        return None
    return mp3 or None





# ── Avatars ───────────────────────────────────────────────────────────────────

@app.get("/{tenant_id}/avatars", response_model=list[S.AvatarListEntity])
async def list_avatars(
    ctx: TenantContext = Depends(tenant_ctx),
    persona_id: int | None = Query(default=None),
    gender: str | None = Query(default=None),
    voice_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assistant_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.AvatarListEntity]:
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
    if assistant_id is not None:
        asst = await repo.get_assistant(ctx.session, assistant_id)
        if asst is not None:
            avatars = [av for av in avatars if av.id == asst.avatar_id]
        else:
            avatars = []
    return [
        S.AvatarListEntity(
            id=av.id,
            name=av.name,
            image_url=_local_blob_to_base64_url(av.image_url),
            created_at=av.created_at,
            status=av.status,
        )
        for av in avatars
    ]


@app.get("/{tenant_id}/avatars/{avatar_id}", response_model=S.PersonaAvatarDetail)
async def get_avatar(
    avatar_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.PersonaAvatarDetail:
    row = await repo.get_persona_avatar(ctx.session, avatar_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if row.status == "processing":
        row = await _refresh_avatar_status(ctx, row)
    
    persona_model = None
    if row.persona_id is not None:
        persona = await repo.get_persona_entity(ctx.session, row.persona_id)
        if persona is not None:
            persona_model = S.PersonaListEntity(
                id=persona.id,
                name=persona.name,
                image_url=_local_blob_to_base64_url(persona.image_url),
                created_at=persona.created_at,
                status=persona.status,
                gender=persona.gender or "unknown",
                avatar_count=await repo.count_avatars_for_persona(ctx.session, persona.id),
            )
            
    return S.PersonaAvatarDetail(
        id=row.id,
        persona_id=row.persona_id,
        name=row.name,
        decoration=row.decoration,
        theme_prompt=row.theme_prompt,
        preset_ids=_parse_preset_ids(row.preset_ids),
        face_id=row.face_id,
        image_url=_local_blob_to_base64_url(row.image_url),
        status=row.status,
        progress=row.progress,
        stage=row.stage,
        last_error=row.last_error,
        created_at=row.created_at,
        persona=persona_model,
    )


@app.post("/{tenant_id}/avatars/preview")
async def avatar_preview(
    persona_id: int = Form(...),
    name: str = Form(...),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    custom_prompt: str = Form(default=""),
    skip_style: bool = Form(default=False),
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
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
            logger.info(
                "🎨 [Avatar %s] Starting avatar variant preview image generation using %s provider (%d presets)...",
                avatar_id, settings.image_provider, len(preset_ids),
            )
            edited_bytes = await ai_router.generate_avatar(
                persona_image_bytes,
                prompt_chain=[prompt],
                filename=f"{name}.png",
            )
        else:
            logger.info("🎨 [Avatar %s] Reusing base portrait directly (skip styling turned on).", avatar_id)
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
        logger.info("✅ [Avatar %s] Variant preview image generated successfully! Preview URL: %s", avatar_id, preview_url)
    except Exception as exc:
        logger.error("❌ [Avatar %s] Variant preview image generation failed: %s", avatar_id, exc)
        async with open_background_context(tenant_id) as ctx:
            await repo.update_persona_avatar(
                ctx.session, avatar_id, status="failed", last_error=str(exc)
            )


@app.post(
    "/{tenant_id}/avatars", response_model=S.PersonaAvatar, status_code=201
)
async def save_avatar(
    persona_id: int = Form(...),
    name: str = Form(...),
    decoration: str = Form(default=""),
    theme_prompt: str = Form(default=""),
    preset_ids: str = Form(default="[]"),
    draft_avatar_id: int | None = Form(default=None),
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
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

    logger.info("💾 [Avatar %s] Starting save_avatar pipeline for '%s'...", draft_avatar_id or "new", name)
    logger.info("📥 [Avatar %s] Fetching avatar image bytes from URL: %s...", draft_avatar_id or "new", source_image_url)
    image_bytes = await _fetch_url_bytes(source_image_url)

    try:
        logger.info("📤 [Avatar %s] Uploading image (%d bytes) to Simli API...", draft_avatar_id or "new", len(image_bytes))
        upload_response = await upload_face_image(
            api_key=settings.simli_api_key,
            image_bytes=image_bytes,
            filename=f"{name}.png",
            face_name=name,
        )
        logger.info("✅ [Avatar %s] Simli upload complete! Response: %s", draft_avatar_id or "new", upload_response)
    except SimliError as exc:
        logger.error("❌ [Avatar %s] Simli image upload failed: %s", draft_avatar_id or "new", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    status = normalize_generation_status(upload_response)
    if status in {"processing", "queued", "pending"}:
        face_id = extract_generation_id(upload_response)
        avatar_status = "processing"
    else:
        face_id = extract_face_id(upload_response)
        avatar_status = "ready"
    if not face_id:
        logger.error("❌ [Avatar %s] Simli did not return a usable avatar id: %s", draft_avatar_id or "new", upload_response)
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
            await repo.update_assistants_face_id(ctx.session, draft_avatar_id, face_id)
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

    logger.info("✅ [Avatar %s] Successfully uploaded to Simli! Face ID: %s (Status: %s)", model.id, face_id, avatar_status)
    if model.status == "processing":
        logger.info("⏳ [Avatar %s] Face is processing on Simli. Starting background polling task...", model.id)
        asyncio.create_task(_poll_avatar_until_ready(tenant_id, model.id))

    return model


@app.get("/{tenant_id}/avatars/{avatar_id}/status", response_model=S.StatusResponse)
async def avatar_status(
    avatar_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.StatusResponse:
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


@app.get("/{tenant_id}/avatars/{avatar_id}/cascade-count")
async def avatar_cascade_count(
    avatar_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    return {
        "assistants": await repo.count_assistants_for_avatar(ctx.session, avatar_id),
    }


@app.delete("/{tenant_id}/avatars/{avatar_id}", status_code=200)
async def delete_avatar(
    avatar_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    """Delete an avatar row + its preview blob + the Simli face. Assistants
    that reference this avatar survive; the FK nulls their ``avatar_id`` and
    we additionally clear their stale ``face_id`` string."""
    row = await repo.get_persona_avatar(ctx.session, avatar_id)
    if row is None:
        logger.error("❌ [Avatar %s] Deletion failed: Avatar not found", avatar_id)
        raise HTTPException(status_code=404, detail="Avatar not found")
    logger.info("🎨 [Avatar %s] Deleting avatar '%s' (Face ID: %s)...", avatar_id, row.name, row.face_id)
    if row.image_url:
        logger.info("🎨 [Avatar %s] Deleting image blob: %s...", avatar_id, row.image_url)
        await _delete_blob_by_url(ctx, row.image_url)
    if row.face_id and settings.simli_api_key:
        try:
            logger.info("🎨 [Avatar %s] Deleting face from Simli backend API...", avatar_id)
            await delete_face(settings.simli_api_key, row.face_id)
        except SimliError as exc:
            logger.warning("⚠️ [Avatar %s] Failed to delete face from Simli backend (ignoring): %s", avatar_id, exc)
            pass
    # face_id on assistants is a free-form string (not an FK) — clear it
    # explicitly before the FK SET NULL nulls their avatar_id.
    await repo.clear_face_id_for_avatar(ctx.session, avatar_id)
    deleted = await repo.delete_persona_avatar(ctx.session, avatar_id)
    logger.info("✅ [Avatar %s] Avatar successfully deleted!", avatar_id)
    return {"deleted": deleted}





@app.post("/{tenant_id}/avatars/{avatar_id}/retry")
async def retry_avatar(
    avatar_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, str]:
    tenant_id = ctx.tenant_id
    row = await repo.get_persona_avatar(ctx.session, avatar_id)
    if row is None:
        logger.error("❌ [Avatar %s] Retry failed: Avatar not found", avatar_id)
        raise HTTPException(status_code=404, detail="Avatar not found")
    if row.status not in {"failed", "cancelled"}:
        logger.error("❌ [Avatar %s] Retry failed: Avatar is not in a retryable state (%s)", avatar_id, row.status)
        raise HTTPException(
            status_code=409, detail="Avatar is not in a retryable state"
        )

    logger.info("🎨 [Avatar %s] Retrying avatar generation pipeline (Status: %s, Face ID: %s)...", avatar_id, row.status, row.face_id)
    if not row.face_id:
        persona = await repo.get_persona_entity(ctx.session, row.persona_id)
        if persona is None:
            logger.error("❌ [Avatar %s] Retry failed: Persona %s not found", avatar_id, row.persona_id)
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
        logger.info("🎨 [Avatar %s] Fetching base persona portrait bytes to re-generate avatar...", avatar_id)
        persona_image_bytes = await _fetch_url_bytes(persona_image_url)
        logger.info("🎨 [Avatar %s] Launching background image generation task...", avatar_id)
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
    logger.info("🎨 [Avatar %s] Avatar already has Face ID on Simli. Starting background polling task...", avatar_id)
    asyncio.create_task(_poll_avatar_until_ready(tenant_id, avatar_id))
    return {"status": "retrying"}


# ── Assistants ────────────────────────────────────────────────────────────────

@app.get("/{tenant_id}/assistants", response_model=list[S.AssistantListEntity])
async def list_assistants_endpoint(
    ctx: TenantContext = Depends(tenant_ctx),
    persona_id: int | None = Query(default=None),
    avatar_id: int | None = Query(default=None),
    voice_id: str | None = Query(default=None),
    llm_provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.AssistantListEntity]:
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
    res = []
    for a in rows:
        avatar_image_url = ""
        persona_name = ""
        avatar_name = ""
        if a.avatar_id is not None:
            av = await repo.get_persona_avatar(ctx.session, a.avatar_id)
            if av is not None:
                avatar_image_url = _local_blob_to_base64_url(av.image_url)
                avatar_name = av.name
        if a.persona_id is not None:
            p = await repo.get_persona_entity(ctx.session, a.persona_id)
            if p is not None:
                persona_name = p.name
        res.append(
            S.AssistantListEntity(
                id=a.id,
                name=a.name,
                avatar_image_url=avatar_image_url,
                created_at=a.created_at,
                status=a.status,
                persona_id=a.persona_id,
                avatar_id=a.avatar_id,
                persona_name=persona_name,
                avatar_name=avatar_name,
            )
        )
    return res


@app.get("/{tenant_id}/assistants/{assistant_id}", response_model=S.AssistantDetail)
async def get_assistant_endpoint(
    assistant_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.AssistantDetail:
    row = await repo.get_assistant(ctx.session, assistant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    
    avatar_model = None
    if row.avatar_id is not None:
        avatar = await repo.get_persona_avatar(ctx.session, row.avatar_id)
        if avatar is not None:
            avatar_model = S.AvatarListEntity(
                id=avatar.id,
                name=avatar.name,
                image_url=_local_blob_to_base64_url(avatar.image_url),
                created_at=avatar.created_at,
                status=avatar.status,
            )
            
    return S.AssistantDetail(
        id=row.id,
        name=row.name,
        prompt=row.prompt,
        first_message=row.first_message,
        persona_id=row.persona_id,
        avatar_id=row.avatar_id,
        avatar=avatar_model,
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
        created_at=row.created_at,
    )


@app.post(
    "/{tenant_id}/assistants", response_model=S.Assistant, status_code=201
)
async def create_assistant(
    payload: S.AssistantCreate,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.Assistant:
    logger.info("📦 [Assistant] Creating assistant '%s' (Persona: %s, Avatar: %s)...", payload.name, payload.persona_id, payload.avatar_id)
    persona = await repo.get_persona_entity(ctx.session, payload.persona_id)
    if persona is None:
        logger.error("❌ [Assistant] Assistant creation failed: Persona %s not found", payload.persona_id)
        raise HTTPException(status_code=404, detail="Persona not found")
    avatar = await repo.get_persona_avatar(ctx.session, payload.avatar_id)
    if avatar is None or avatar.persona_id != payload.persona_id:
        logger.error("❌ [Assistant] Assistant creation failed: Avatar %s not found for Persona %s", payload.avatar_id, payload.persona_id)
        raise HTTPException(
            status_code=404, detail="Avatar not found for persona"
        )
    avatar = await _refresh_avatar_status(ctx, avatar)
    if avatar.status != "ready":
        logger.error("❌ [Assistant] Assistant creation failed: Avatar %s is still processing", payload.avatar_id)
        raise HTTPException(
            status_code=409, detail="Avatar is still processing"
        )

    llm_provider = (payload.llm_provider or settings.call_llm_provider).lower()
    if llm_provider == "gemini":
        llm_model = payload.llm_model or settings.call_llm_model_gemini
    elif llm_provider == "openai":
        llm_model = payload.llm_model or settings.call_llm_model_openai
    elif llm_provider == "azure_openai":
        llm_model = payload.llm_model or settings.call_llm_model_azure_openai
    else:
        logger.error("❌ [Assistant] Assistant creation failed: Unsupported LLM provider %s", llm_provider)
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
    logger.info("✅ [Assistant] Assistant '%s' created successfully! (ID: %s, Status: ready)", row.name, row.id)
    return _assistant_row_to_model(row)


@app.patch("/{tenant_id}/assistants/{assistant_id}", response_model=S.AssistantDetail)
async def patch_assistant(
    assistant_id: int,
    payload: S.AssistantPatch,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.AssistantDetail:
    assistant = await repo.get_assistant(ctx.session, assistant_id)
    if assistant is None:
        raise HTTPException(status_code=404, detail="Assistant not found")

    fields: dict[str, Any] = {}
    if payload.name is not None:
        fields["name"] = payload.name
    if payload.prompt is not None:
        fields["prompt"] = payload.prompt
    if payload.first_message is not None:
        fields["first_message"] = payload.first_message
    if payload.llm_provider is not None:
        fields["llm_provider"] = payload.llm_provider.lower()
    if payload.llm_model is not None:
        fields["llm_model"] = payload.llm_model

    if payload.avatar_id is not None:
        avatar = await repo.get_persona_avatar(ctx.session, payload.avatar_id)
        if avatar is None:
            raise HTTPException(status_code=404, detail="Avatar not found")
        avatar = await _refresh_avatar_status(ctx, avatar)
        if avatar.status != "ready":
            raise HTTPException(
                status_code=409, detail="Avatar is still processing"
            )
        fields["avatar_id"] = payload.avatar_id
        fields["face_id"] = avatar.face_id
        if avatar.persona_id is not None:
            fields["persona_id"] = avatar.persona_id
            persona = await repo.get_persona_entity(ctx.session, avatar.persona_id)
            if persona is not None:
                if persona.voice_id and persona.voice_provider:
                    fields["voice_provider"] = persona.voice_provider
                    fields["voice_id"] = persona.voice_id
                    fields["voice_model"] = (
                        settings.tts_model
                        if persona.voice_provider == "elevenlabs"
                        else settings.default_simli_voice_model
                    )
                else:
                    fields["voice_provider"] = settings.default_simli_voice_provider
                    fields["voice_id"] = settings.default_simli_voice_id or None
                    fields["voice_model"] = settings.default_simli_voice_model

    if fields:
        await repo.update_assistant(ctx.session, assistant_id, **fields)

    updated = await repo.get_assistant(ctx.session, assistant_id)
    assert updated is not None

    avatar_model = None
    if updated.avatar_id is not None:
        av = await repo.get_persona_avatar(ctx.session, updated.avatar_id)
        if av is not None:
            avatar_model = S.AvatarListEntity(
                id=av.id,
                name=av.name,
                image_url=av.image_url,
                created_at=av.created_at,
                status=av.status,
            )

    return S.AssistantDetail(
        id=updated.id,
        name=updated.name,
        prompt=updated.prompt,
        first_message=updated.first_message,
        persona_id=updated.persona_id,
        avatar_id=updated.avatar_id,
        avatar=avatar_model,
        face_id=updated.face_id,
        simli_agent_id=updated.simli_agent_id,
        voice_provider=updated.voice_provider,
        voice_id=updated.voice_id,
        voice_model=updated.voice_model,
        language=updated.language,
        llm_provider=updated.llm_provider,
        llm_model=updated.llm_model,
        status=updated.status,
        progress=updated.progress,
        stage=updated.stage,
        last_error=updated.last_error,
        created_at=updated.created_at,
    )


@app.get(
    "/{tenant_id}/assistants/{assistant_id}/status",
    response_model=S.StatusResponse,
)
async def assistant_status(
    assistant_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.StatusResponse:
    row = await repo.get_assistant(ctx.session, assistant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Assistant not found")
    return S.StatusResponse(
        status=row.status,
        stage=row.stage,
        progress=row.progress,
        last_error=row.last_error,
    )


@app.delete("/{tenant_id}/assistants/{assistant_id}", status_code=200)
async def delete_assistant(
    assistant_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    row = await repo.get_assistant(ctx.session, assistant_id)
    if row is None:
        logger.error("❌ [Assistant %s] Deletion failed: Assistant not found", assistant_id)
        raise HTTPException(status_code=404, detail="Assistant not found")
    logger.info("📦 [Assistant %s] Deleting assistant '%s'...", assistant_id, row.name)
    deleted = await repo.delete_assistant(ctx.session, assistant_id)
    logger.info("✅ [Assistant %s] Assistant successfully deleted!", assistant_id)
    return {"deleted": deleted}


# ── Calls (plug-and-play LiveKit + face/voice metadata) ───────────────────────

# Simli Auto only accepts ElevenLabs / Cartesia / PlayHT for TTS. Anything
# else falls back to the system ElevenLabs default (Sarah).
_SIMLI_AUTO_TTS_PROVIDERS = {"elevenlabs": "ElevenLabs", "cartesia": "Cartesia", "playht": "PlayHT"}
_FALLBACK_ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"

# Mirror the LiveKit worker's language lock so the Auto path responds
# in the user's language too (worker.py has the same suffix).
_AUTO_LANGUAGE_LOCK = (
    "\n\nLANGUAGE RULE — this overrides everything else: "
    "Always detect the language of the user's most recent message and respond in that exact language. "
    "If the user switches language mid-conversation, you switch immediately and fully — no lag, no mixing. "
    "Every single word of your response must be in the user's current language. "
    "Never insert English words or phrases when the user is speaking a non-English language. "
    "Follow the user's language naturally like a fluent bilingual speaker would."
)


def _build_simli_auto_payload(
    assistant: AssistantRow,
    face_id: str,
) -> dict[str, object]:
    """Translate an Assistant row into Simli's ConfigurableSessionRequest."""

    # ── TTS ──
    provider_lower = (assistant.voice_provider or "").lower()
    tts_provider_simli = _SIMLI_AUTO_TTS_PROVIDERS.get(provider_lower)
    if tts_provider_simli:
        tts_voice_id = assistant.voice_id or _FALLBACK_ELEVENLABS_VOICE_ID
        tts_api_key = settings.elevenlabs_api_key if provider_lower == "elevenlabs" else ""
    else:
        # OpenAI / Google TTS aren't supported by Simli Auto — fall back to
        # the system ElevenLabs default per the user's chosen policy.
        tts_provider_simli = "ElevenLabs"
        tts_voice_id = _FALLBACK_ELEVENLABS_VOICE_ID
        tts_api_key = settings.elevenlabs_api_key

    # ── LLM ──
    llm_provider_lower = (assistant.llm_provider or settings.call_llm_provider).lower()
    llm_model = assistant.llm_model or ""
    llm_config: dict[str, object] = {}
    if llm_provider_lower == "gemini":
        llm_config = {
            "model": llm_model or settings.call_llm_model_gemini,
            "provider": "User",
            "apiKey": settings.gemini_api_key,
            "baseURL": "https://generativelanguage.googleapis.com/v1beta/openai",
        }
    elif llm_provider_lower == "azure_openai":
        if settings.simli_azure_proxy_url:
            deployment = assistant.llm_model or settings.call_llm_model_azure_openai or "gpt-4o"
            llm_config = {
                "model": deployment,
                "provider": "User",
                "apiKey": settings.azure_openai_api_key,
                "baseURL": f"{settings.simli_azure_proxy_url.rstrip('/')}/azure-openai-proxy/{deployment}",
            }
        else:
            # Simli Auto has no native Azure OpenAI support due to custom header requirements
            # (Azure requires `api-key` instead of `Authorization: Bearer`).
            # Therefore, we automatically fall back to standard OpenAI or Google Gemini.
            if settings.openai_api_key:
                llm_config = {
                    "model": "gpt-4o-mini",
                    "provider": "User",
                    "apiKey": settings.openai_api_key,
                    "baseURL": "https://api.openai.com/v1",
                }
            elif settings.gemini_api_key:
                llm_config = {
                    "model": "gemini-1.5-flash",
                    "provider": "User",
                    "apiKey": settings.gemini_api_key,
                    "baseURL": "https://generativelanguage.googleapis.com/v1beta/openai",
                }
            else:
                llm_config = {
                    "model": settings.call_llm_model_openai,
                    "provider": "User",
                    "apiKey": settings.openai_api_key,
                    "baseURL": "https://api.openai.com/v1",
                }
    else:
        llm_config = {
            "model": llm_model or settings.call_llm_model_openai,
            "provider": "User",
            "apiKey": settings.openai_api_key,
            "baseURL": "https://api.openai.com/v1",
        }

    system_prompt = (assistant.prompt or "").rstrip() + _AUTO_LANGUAGE_LOCK

    return {
        "faceId": face_id,
        "ttsProvider": tts_provider_simli,
        "ttsAPIKey": tts_api_key,
        "voiceId": tts_voice_id,
        "systemPrompt": system_prompt,
        "firstMessage": assistant.first_message or "",
        "maxSessionLength": 3600,
        "maxIdleTime": 90,
        "language": (assistant.language or "en"),
        "llmConfig": llm_config,
        "createTranscript": False,
        # Intentionally omitting "model" so Simli picks its current
        # default lipsync engine — always rides the latest.
    }


@app.post("/{tenant_id}/calls", response_model=S.AssistantCallResponse)
async def start_call(
    payload: S.AssistantCallCreate,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.AssistantCallResponse:
    assistant = await repo.get_assistant(ctx.session, payload.assistant_id)
    if assistant is None:
        logger.error("❌ [Call] Call initiation failed: Assistant %s not found", payload.assistant_id)
        raise HTTPException(status_code=404, detail="Assistant not found")
    if assistant.avatar_id is None:
        logger.error("❌ [Call] Call initiation failed: Assistant %s has no avatar attached", payload.assistant_id)
        raise HTTPException(
            status_code=409,
            detail="Assistant has no avatar attached — re-attach an avatar first.",
        )
    avatar = await repo.get_persona_avatar(ctx.session, assistant.avatar_id)
    if avatar is None:
        logger.error("❌ [Call] Call initiation failed: Avatar %s not found", assistant.avatar_id)
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
        if not voice_preview_url and assistant.persona_id is not None:
            persona = await repo.get_persona_entity(ctx.session, assistant.persona_id)
            if persona and persona.voice_id == assistant.voice_id:
                voice_preview_url = persona.voice_preview_url

    avatar_image_url = avatar.image_url

    common_response = dict(
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

    # ── Simli Auto (no LiveKit) ──
    if settings.simli_transport == "auto":
        logger.info("🚀 [Call] Starting auto-transport (non-LiveKit) call for Assistant %s ('%s') using Face ID %s...", assistant.id, assistant.name, avatar.face_id)
        if not settings.simli_api_key:
            logger.error("❌ [Call] Auto-transport call failed: SIMLI_API_KEY is missing")
            raise HTTPException(status_code=500, detail="SIMLI_API_KEY is missing")
        if not avatar.face_id:
            logger.error("❌ [Call] Auto-transport call failed: Avatar has no face_id")
            raise HTTPException(status_code=409, detail="Avatar has no face_id")

        auto_payload = _build_simli_auto_payload(assistant, avatar.face_id)
        try:
            result = await start_auto_session(settings.simli_api_key, auto_payload)
        except SimliError as exc:
            logger.error("❌ [Call] Auto-transport call failed: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        room_url = str(result.get("roomUrl") or "")
        session_id = str(result.get("sessionId") or "")
        if not room_url:
            logger.error("❌ [Call] Auto-transport call failed: Simli Auto returned no roomUrl: %s", result)
            raise HTTPException(status_code=502, detail=f"Simli Auto returned no roomUrl: {result}")

        logger.info("✅ [Call] Auto-transport call started successfully! Simli Room URL: %s, Session ID: %s", room_url, session_id)
        return S.AssistantCallResponse(
            transport="auto",
            simli_auto=S.CallSimliAuto(room_url=room_url, session_id=session_id),
            **common_response,
        )

    # ── LiveKit (default) ──
    logger.info("🚀 [Call] Starting LiveKit call for Assistant %s ('%s')...", assistant.id, assistant.name)
    if not (
        settings.livekit_url
        and settings.livekit_api_key
        and settings.livekit_api_secret
    ):
        logger.error("❌ [Call] LiveKit call failed: LiveKit credentials are missing")
        raise HTTPException(status_code=500, detail="LiveKit credentials are missing")

    room_name = f"assistant-{payload.assistant_id}-{uuid4().hex[:8]}"
    identity = f"user-{uuid4().hex[:8]}"
    room_config = build_agent_dispatch_room_config(
        room_name=room_name,
        assistant_id=payload.assistant_id,
        tenant_id=ctx.tenant_id,
    )
    token = create_join_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
        identity=identity,
        room_name=room_name,
        participant_name=f"User {identity[-4:]}",
        room_config=room_config,
    )

    logger.info("✅ [Call] LiveKit call token generated successfully! Room: %s, Identity: %s", room_name, identity)
    return S.AssistantCallResponse(
        transport="livekit",
        livekit=S.CallLivekit(
            url=settings.livekit_url,
            token=token,
            room=room_name,
            identity=identity,
        ),
        **common_response,
    )


# ── Standalone voices ─────────────────────────────────────────────────────────

@app.get("/{tenant_id}/voices", response_model=list[S.VoiceListEntity])
async def list_voices_endpoint(
    ctx: TenantContext = Depends(tenant_ctx),
    gender: str | None = Query(default=None),
    source: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    status: str | None = Query(default=None),
    persona_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[S.VoiceListEntity]:
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
    return [
        S.VoiceListEntity(
            id=v.id,
            name=v.name,
            created_at=v.created_at,
            status=v.status,
            source=v.source,
            gender=v.gender,
            description=v.description,
            persona_id=v.persona_id,
        )
        for v in rows
    ]


# NOTE: ``GET /{tenant_id}/voices/{voice_id}`` is declared *after* the
# specific-name routes (``/voices/library``, ``/voices/preview-default``,
# ``/voices/suggest-description``) so FastAPI's longest-prefix matching
# doesn't swallow ``library`` / ``preview-default`` as a ``voice_id``.
# The same applies to ``/voices/{voice_id}/status``, ``/voices/{voice_id}/retry``,
# and ``DELETE /voices/{voice_id}`` — all declared further down.


@app.post(
    "/{tenant_id}/voices/design", response_model=S.VoiceEntity, status_code=201
)
async def design_voice_standalone(
    name: str = Form(...),
    description: str = Form(default=""),
    # Multi-select: the client sends one repeated multipart field per
    # preset id (e.g. ``voice_preset_ids=tone_reg_baritone_m
    # &voice_preset_ids=tone_pace_measured&voice_preset_ids=prof_documentary``).
    # Server-side ``build_voice_design_brief`` then enforces the
    # one-preset-per-(category, partition) rule.
    voice_preset_ids: list[str] = Form(default_factory=list),
    persona_id: int | None = Form(default=None),
    include_persona_traits: bool = Form(default=False),
    gender: str | None = Form(
        default=None,
        description="Optional gender directive: 'male' or 'female'. "
        "Appended to the voice description so ElevenLabs biases the design.",
    ),
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
) -> S.VoiceEntity:
    if voice_preset_ids:
        # Assembled brief replaces the description when none was supplied,
        # otherwise gets appended after the user's free-form text.
        preset_brief = build_voice_design_brief(voice_preset_ids)
        description = (
            f"{description.strip()}; {preset_brief}"
            if description.strip()
            else preset_brief
        )

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
        if persona_id:
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_ref_id=voice_id,
                voice_provider=settings.default_simli_voice_provider,
                voice_id="",
                voice_source="designed",
                voice_description=initial_description,
                voice_sample_url="",
                voice_preview_url="",
                voice_status="processing",
                voice_last_error=None,
            )
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

    logger.info("🎙 [Voice Design %s] Starting AI voice design pipeline for voice '%s' (Gender: %s)...", voice_id, voice_name, gender)
    try:
        if len(description) < 20:
            raise RuntimeError(
                "Voice description too short — please describe the voice in more detail."
            )
        if not settings.elevenlabs_api_key:
            raise RuntimeError("ELEVENLABS_API_KEY is missing.")

        el_voice_id = ""
        preview_bytes: bytes | None = None
        # Per-voice showcase line generated by the LLM from the description.
        logger.info("🎙 [Voice Design %s] Calling LLM to generate custom voice showcase preview audition text...", voice_id)
        preview_text = await ai_router.generate_voice_preview_text(prompt_description)
        try:
            logger.info("🎙 [Voice Design %s] Requesting ElevenLabs text-to-voice design previews...", voice_id)
            previews = await elevenlabs_client.create_voice_design_previews(
                settings.elevenlabs_api_key,
                voice_description=prompt_description,
                text=preview_text,
            )
            first = previews[0]
            gen_id = str(first.get("generated_voice_id") or "")
            if not gen_id:
                raise RuntimeError(f"No generated_voice_id returned: {first}")
            preview_bytes = elevenlabs_client.decode_preview_audio(first)
            logger.info("🎙 [Voice Design %s] ElevenLabs design previews fetched! Committing the best preview to a persistent voice...", voice_id)
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
            # default voice + TTS preview rendered with the same showcase line.
            logger.warning("⚠️ [Voice Design %s] ElevenLabs tier doesn't support Voice Design. Falling back to default pre-made voice: %s", voice_id, settings.tts_voice_id or "Sarah")
            el_voice_id = settings.tts_voice_id or "EXAVITQu4vr4xnSDxMaL"
            preview_bytes = await _store_voice_preview(
                settings.elevenlabs_api_key, el_voice_id, text=preview_text
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
            logger.info("✅ [Voice Design %s] Successfully designed voice! ElevenLabs Voice ID: %s (Status: ready)", voice_id, el_voice_id)
            # Propagate newly generated voice details to any persona and its assistants linked to this voice
            personas = await repo.list_persona_entities(ctx.session)
            for p in personas:
                if p.voice_ref_id == voice_id:
                    await repo.update_persona_entity(
                        ctx.session,
                        p.id,
                        voice_id=el_voice_id,
                        voice_provider=settings.default_simli_voice_provider,
                        voice_source="designed",
                        voice_description=description,
                        voice_preview_url=preview_url,
                        voice_status="ready",
                        voice_last_error=None,
                    )
                    # Propagate to all assistants for this persona.
                    assistants = await repo.list_assistants(ctx.session)
                    for asst in assistants:
                        if asst.persona_id == p.id:
                            await repo.update_assistant(
                                ctx.session,
                                asst.id,
                                voice_provider=settings.default_simli_voice_provider,
                                voice_id=el_voice_id,
                                voice_model=settings.tts_model,
                            )
    except Exception as exc:
        logger.error("❌ [Voice Design %s] Voice design process failed: %s", voice_id, exc)
        async with open_background_context(tenant_id) as ctx:
            await repo.update_voice(
                ctx.session, voice_id, status="failed", last_error=str(exc)
            )


@app.post(
    "/{tenant_id}/voices/clone", response_model=S.VoiceEntity, status_code=201
)
async def clone_voice_standalone(
    voice_sample: UploadFile = File(...),
    name: str = Form(default=""),
    persona_id: int | None = Form(default=None),
    tenant_id: str = PathParam(..., min_length=1, max_length=63),
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
        if persona_id:
            await repo.update_persona_entity(
                ctx.session,
                persona_id,
                voice_ref_id=voice_id,
                voice_provider=settings.default_simli_voice_provider,
                voice_id="",
                voice_source="cloned",
                voice_description="",
                voice_sample_url="",
                voice_preview_url="",
                voice_status="processing",
                voice_last_error=None,
            )
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
    logger.info("🎙 [Voice Clone %s] Starting voice clone pipeline for voice '%s' using sample: %s...", voice_id, voice_name, sample_filename)
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
        logger.info("🎙 [Voice Clone %s] Calling Gemini to analyze and detect gender from the audio sample...", voice_id)
        detected_gender = await ai_router.detect_gender_from_audio(
            audio_bytes, mime_type=sample_mime
        )
        final_gender = (
            detected_gender if detected_gender in {"male", "female"} else fallback_gender
        )

        logger.info("🎙 [Voice Clone %s] Requesting ElevenLabs Instant Voice Cloning with final gender: %s...", voice_id, final_gender)
        el_voice_id = await elevenlabs_client.add_cloned_voice(
            settings.elevenlabs_api_key,
            name=voice_name or f"voice-{voice_id}",
            description="Cloned voice",
            audio_bytes=audio_bytes,
            filename=sample_filename,
            mime_type=sample_mime,
            labels=_labels_for_gender(final_gender),
        )
        # Standalone clone — minimal description; use gender + name as the
        # hint so the LLM still tailors the showcase line.
        preview_text = await ai_router.generate_voice_preview_text(
            f"a cloned {final_gender or 'unknown'} voice named '{voice_name}'"
        )
        preview_bytes = await _store_voice_preview(
            settings.elevenlabs_api_key, el_voice_id, text=preview_text
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
            logger.info("✅ [Voice Clone %s] Successfully cloned voice! ElevenLabs Voice ID: %s (Status: ready)", voice_id, el_voice_id)
            # Propagate newly cloned voice details to any persona and its assistants linked to this voice
            personas = await repo.list_persona_entities(ctx.session)
            for p in personas:
                if p.voice_ref_id == voice_id:
                    await repo.update_persona_entity(
                        ctx.session,
                        p.id,
                        voice_id=el_voice_id,
                        voice_provider=settings.default_simli_voice_provider,
                        voice_source="cloned",
                        voice_description="Cloned voice",
                        voice_preview_url=preview_url,
                        voice_status="ready",
                        voice_last_error=None,
                    )
                    # Propagate to all assistants for this persona.
                    assistants = await repo.list_assistants(ctx.session)
                    for asst in assistants:
                        if asst.persona_id == p.id:
                            await repo.update_assistant(
                                ctx.session,
                                asst.id,
                                voice_provider=settings.default_simli_voice_provider,
                                voice_id=el_voice_id,
                                voice_model=settings.tts_model,
                            )
    except Exception as exc:
        logger.error("❌ [Voice Clone %s] Voice cloning process failed: %s", voice_id, exc)
        async with open_background_context(tenant_id) as ctx:
            await repo.update_voice(
                ctx.session, voice_id, status="failed", last_error=str(exc)
            )


class VoiceSuggestDescriptionRequest(BaseModel):
    persona_id: int
    user_hint: str = ""


@app.post("/{tenant_id}/voices/suggest-description")
async def suggest_voice_description(
    payload: VoiceSuggestDescriptionRequest,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, str]:
    persona = await repo.get_persona_entity(ctx.session, payload.persona_id)
    if persona is None:
        logger.error("❌ [Voice Suggestion] Suggest failed: Persona %s not found", payload.persona_id)
        raise HTTPException(status_code=404, detail="Persona not found")
    image_url = persona.image_url
    logger.info("🎙 [Voice Suggestion] Requesting AI-suggested voice description for Persona %s (Hint: '%s')...", payload.persona_id, payload.user_hint)
    try:
        logger.info("🎙 [Voice Suggestion] Downloading persona portrait bytes: %s...", image_url)
        image_bytes = await _fetch_url_bytes(image_url)
        logger.info("🎙 [Voice Suggestion] Requesting voice analysis suggestion from vision LLM (%s)...", settings.gender_provider)
        description = await ai_router.describe_voice(
            image_bytes, user_prompt=payload.user_hint
        )
        logger.info("✅ [Voice Suggestion] Voice description generated successfully!")
        return {"description": (description or "").strip()}
    except Exception as exc:
        logger.error("❌ [Voice Suggestion] Suggestion failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# Tiny in-process cache for the ElevenLabs premade voice library.
_library_cache: list[dict] = []
_library_cache_ts: float = 0.0


@app.get("/{tenant_id}/voices/library")
async def voice_library(
    ctx: TenantContext = Depends(tenant_ctx),
) -> list[dict]:
    """Returns ElevenLabs's premade voices. Cached process-wide for 5 min.

    The catalogue is the same for every tenant (it comes from the
    operator's ElevenLabs account); the ``?tenant_id=`` query param is
    enforced here purely for contract symmetry — it gates access via the
    standard tenant-resolution dependency.
    """
    _ = ctx  # validated via the dependency
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


class VoiceFromLibraryRequest(BaseModel):
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
    "/{tenant_id}/voices/from-library",
    response_model=S.VoiceEntity,
    status_code=201,
)
async def voice_from_library(
    payload: VoiceFromLibraryRequest,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.VoiceEntity:
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

    logger.info("🎙 [Voice] Creating voice from library entry using EL Voice ID: %s (Name: '%s', Gender: %s)...", payload.voice_id, payload.name, gender)
    existing = [
        v
        for v in await repo.list_voices(ctx.session)
        if v.voice_id == payload.voice_id and v.source == "premade"
    ]
    if existing:
        logger.info("🎙 [Voice] Reusing existing voice in DB with ID: %s", existing[0].id)
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
    logger.info("✅ [Voice %s] Premade library voice successfully imported and registered!", row.id)
    return _voice_row_to_model(row)


@app.get("/{tenant_id}/voices/preview-default")
async def voice_preview_default(
    ctx: TenantContext = Depends(tenant_ctx),
) -> Response:
    """Streams an MP3 preview of the env-configured default voice."""
    _ = ctx  # validated via the dependency
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


# Declared here so the literal-name routes above ("library",
# "preview-default", "suggest-description") match before this catch-all
# integer-id route.
@app.get("/{tenant_id}/voices/{voice_id}", response_model=S.VoiceEntity)
async def get_voice_endpoint(
    voice_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.VoiceEntity:
    row = await repo.get_voice(ctx.session, voice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Voice not found")
    return _voice_row_to_model(row)


@app.get("/{tenant_id}/voices/{voice_id}/status", response_model=S.StatusResponse)
async def voice_status(
    voice_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> S.StatusResponse:
    row = await repo.get_voice(ctx.session, voice_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Voice not found")
    return S.StatusResponse(
        status=row.status,
        stage=None,
        progress=None,
        last_error=row.last_error,
    )


@app.delete("/{tenant_id}/voices/{voice_id}", status_code=200)
async def delete_voice(
    voice_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, int]:
    """Delete a standalone voice row + its sample/preview blobs + the
    ElevenLabs voice. Any persona that referenced this voice via
    ``voice_ref_id`` has its voice fields cleared."""
    row = await repo.get_voice(ctx.session, voice_id)
    if row is None:
        logger.error("❌ [Voice %s] Deletion failed: Voice not found", voice_id)
        raise HTTPException(status_code=404, detail="Voice not found")

    logger.info("🎙 [Voice %s] Deleting voice '%s' (Source: %s, EL ID: %s)...", voice_id, row.name, row.source, row.voice_id)
    if row.sample_url:
        logger.info("🎙 [Voice %s] Deleting voice sample audio blob: %s...", voice_id, row.sample_url)
        await _delete_blob_by_url(ctx, row.sample_url)
    if row.preview_url:
        logger.info("🎙 [Voice %s] Deleting voice preview audio blob: %s...", voice_id, row.preview_url)
        await _delete_blob_by_url(ctx, row.preview_url)

    # External: ElevenLabs voice.
    if row.voice_id and settings.elevenlabs_api_key:
        try:
            logger.info("🎙 [Voice %s] Deleting voice from ElevenLabs backend account...", voice_id)
            await elevenlabs_client.delete_voice(
                settings.elevenlabs_api_key, row.voice_id
            )
        except ElevenLabsError as exc:
            logger.warning("⚠️ [Voice %s] ElevenLabs deletion failed (ignoring): %s", voice_id, exc)
            pass

    # Clear voice fields on every persona that referenced this voice.
    await repo.clear_persona_voice_ref(ctx.session, voice_id)

    await repo.delete_voice(ctx.session, voice_id)
    logger.info("✅ [Voice %s] Voice successfully deleted from DB and storage!", voice_id)
    return {"deleted": 1}


@app.post("/{tenant_id}/voices/{voice_id}/retry")
async def retry_voice(
    voice_id: int,
    ctx: TenantContext = Depends(tenant_ctx),
) -> dict[str, str]:
    tenant_id = ctx.tenant_id
    row = await repo.get_voice(ctx.session, voice_id)
    if row is None:
        logger.error("❌ [Voice %s] Retry failed: Voice not found", voice_id)
        raise HTTPException(status_code=404, detail="Voice not found")
    if row.status not in {"failed", "cancelled"}:
        logger.error("❌ [Voice %s] Retry failed: Voice is not in a retryable state (%s)", voice_id, row.status)
        raise HTTPException(status_code=409, detail="Voice is not in a retryable state")

    source = row.source or "designed"
    logger.info("🎙 [Voice %s] Retrying voice creation pipeline (Source: %s, Name: '%s')...", voice_id, source, row.name)
    if source == "cloned":
        if not row.sample_url:
            logger.error("❌ [Voice %s] Retry failed: Original audio sample URL is missing", voice_id)
            raise HTTPException(
                status_code=400,
                detail="Original sample is not stored — please re-upload.",
            )
        logger.info("🎙 [Voice %s] Fetching original sample bytes to retry clone...", voice_id)
        audio_bytes = await _fetch_url_bytes(row.sample_url)
        sample_ext = os.path.splitext(row.sample_url.split("?", 1)[0])[1].lower() or ".mp3"
        await repo.update_voice(
            ctx.session, voice_id, status="processing", last_error=None
        )
        logger.info("🎙 [Voice %s] Launching background voice cloning task...", voice_id)
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
            logger.error("❌ [Voice %s] Retry failed: Voice has no description text", voice_id)
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
        logger.info("🎙 [Voice %s] Launching background voice design task...", voice_id)
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


@app.post("/azure-openai-proxy/{deployment}/chat/completions")
async def azure_openai_proxy(
    deployment: str,
    request: Request,
):
    """Proxy route to forward standard OpenAI-compatible requests to Azure OpenAI.

    This translates the bearer token authentication used by Simli Auto to Azure's
    custom api-key header, maintaining standard SSE streaming formats seamlessly.
    """
    if not settings.azure_openai_endpoint:
        logger.error("Proxy request failed: AZURE_OPENAI_ENDPOINT is not configured")
        raise HTTPException(
            status_code=500,
            detail="AZURE_OPENAI_ENDPOINT is not configured on the backend.",
        )

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Proxy request failed to parse body as JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    endpoint_base = settings.azure_openai_endpoint.rstrip("/")
    api_version = settings.azure_openai_api_version or "2025-01-01-preview"
    target_url = f"{endpoint_base}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    # Extract api-key from Authorization header or default to server setting
    auth_header = request.headers.get("Authorization", "")
    api_key = settings.azure_openai_api_key
    if auth_header.startswith("Bearer "):
        extracted_key = auth_header.split("Bearer ", 1)[1].strip()
        if extracted_key:
            api_key = extracted_key

    if not api_key:
        logger.error("Proxy request failed: No API key provided or configured")
        raise HTTPException(
            status_code=401,
            detail="Azure OpenAI API key is missing. Configure AZURE_OPENAI_API_KEY.",
        )

    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }

    async def response_streamer():
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST",
                    target_url,
                    headers=headers,
                    json=body,
                    timeout=60.0,
                ) as response:
                    if response.status_code >= 400:
                        err_body = await response.aread()
                        err_msg = err_body.decode("utf-8", errors="ignore")
                        logger.error(f"Azure OpenAI returned error status {response.status_code}: {err_msg}")
                        raise RuntimeError(f"Azure OpenAI error: {err_msg}")
                    
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except Exception as e:
            logger.exception(f"Error in Azure OpenAI proxy stream: {e}")
            raise

    return StreamingResponse(response_streamer(), media_type="text/event-stream")
