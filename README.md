# Lili Studio

> **AI Avatar Studio** — design a persona, generate a photorealistic face, design or clone a voice, then conduct a real-time video conversation with your assistant.

Lili Studio is a full-stack workbench that takes you from **concept → photoreal avatar → voice-acted assistant → live video call** in minutes. It unifies best-in-class providers (Simli, LiveKit, ElevenLabs, Gemini, Deepgram) behind a single drag-and-drop interface.

![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-embedded-003B57?logo=sqlite&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)
![LiveKit](https://img.shields.io/badge/LiveKit-WebRTC-orange)

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [End-to-End Workflow](#end-to-end-workflow)
- [User Flow](#user-flow)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running Locally](#running-locally)
- [Project Structure](#project-structure)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [How Calls Work Under the Hood](#how-calls-work-under-the-hood)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

---

## Features

| Feature | Description |
|:---|:---|
| **Personas** | Upload a portrait, auto-detect gender, store identity and system prompt. |
| **Avatar Generator** | AI-edit the portrait (background, outfit, lighting presets) using Gemini `nano-banana` / OpenAI `gpt-image-1`. Register the result with Simli to obtain a `face_id` for live animation. |
| **Voice Design** | Describe a voice in natural language — ElevenLabs Voice Design synthesises an AI voice tailored to the persona's portrait. |
| **Voice Cloning** | Upload a 30-second audio sample — ElevenLabs Voice Clone produces a faithful reproduction. |
| **Voice Library** | Browse the ElevenLabs Voice Library and add any voice to the studio in a single click. |
| **Assistants** | Bind one persona + one avatar + one voice + one LLM into a callable assistant. |
| **Live Video Calls** | Click **Start Call** for an instant WebRTC session. The avatar lip-syncs to TTS audio in real time. |
| **Streaming Updates** | A WebSocket channel delivers live progress events (avatar generating, voice processing, Simli registering) directly to the UI. |

---

## Tech Stack

### Core

| Component | Technology |
|:---|:---|
| HTTP + WebSocket server | **FastAPI** (async) |
| Dependency management | **uv** (fast, reproducible) |
| Persistence | **SQLite** (personas, voices, avatars, assistants) |
| Frontend | **Vanilla JS + HTML + CSS** — zero-framework single-page UI |
| Deployment | **Docker Compose** — `flow` API + `worker` LiveKit agent |

### Realtime Stack

| Component | Technology |
|:---|:---|
| WebRTC SFU | **LiveKit Cloud** — room management and agent dispatch |
| Agent orchestration | **LiveKit Agents (Python)** — STT → LLM → TTS → avatar pipeline |
| Face animation | **Simli** — photoreal lip-sync as a virtual LiveKit participant |

### AI Providers

All providers are hot-swappable via environment variables — no code changes required.

| Task | Default | Alternative |
|:---|:---|:---|
| Gender detection | Gemini 3 Flash | OpenAI GPT-5-nano |
| Avatar image edit | Gemini 3.1 Flash Image | OpenAI `gpt-image-1` |
| In-call LLM | Gemini 3 Flash | OpenAI GPT-5-mini |
| Speech-to-text | Deepgram Nova-3 | OpenAI Whisper |
| Text-to-speech | ElevenLabs Flash v2.5 | OpenAI / Google |
| Voice design / clone | ElevenLabs | — |

---

## Architecture

```
                         ┌──────────────────────────────────────────────────────┐
                         │                      Browser                         │
                         │  Studio UI (drag-drop persona/voice/avatar)          │
                         │  LiveKit JS SDK (WebRTC client during calls)         │
                         └───────────────┬────────────────────┬─────────────────┘
                                         │ HTTP/WS            │ WebRTC
                                         ▼                    ▼
       ┌───────────────────────────────────────┐   ┌────────────────────────────┐
       │       avatar-agent-flow (FastAPI)     │   │       LiveKit Cloud        │
       │  • REST API for personas/voices/...   │   │  • Room SFU                │
       │  • Orchestrates avatar generation     │   │  • Agent dispatch          │
       │  • Issues LiveKit join tokens         │   │                            │
       │  • SQLite + uploads volume            │   └─────────────┬──────────────┘
       └────────┬───────────────┬────────┬─────┘                 │
                │               │        │                       │ dispatches job
                ▼               ▼        ▼                       ▼
       ┌─────────────┐ ┌──────────────┐ ┌─────────────┐  ┌──────────────────────┐
       │  ElevenLabs │ │    Gemini    │ │   Simli     │  │ avatar-agent-worker  │
       │  voices/TTS │ │  image+LLM   │ │  face API   │  │  (LiveKit agent)     │
       └─────────────┘ └──────────────┘ └─────────────┘  │  • STT (Deepgram)    │
                                                         │  • LLM (Gemini/GPT)  │
                                                         │  • TTS (ElevenLabs)  │
                                                         │  • Simli avatar      │
                                                         └──────────┬───────────┘
                                                                    │ DataStream (PCM)
                                                                    ▼
                                                         ┌──────────────────────┐
                                                         │  Simli render server │
                                                         │  (face + voice)      │
                                                         └──────────┬───────────┘
                                                                    │ audio+video RTC
                                                                    ▼
                                                              [back to room]
```

Two containers share a single SQLite volume:

| Service | Role |
|:---|:---|
| `avatar-agent-flow` | FastAPI application on `:8000`. Serves the studio UI, REST API, and WebSocket. |
| `avatar-agent-worker` | LiveKit agent. Remains idle until a call arrives, then spawns a per-call subprocess. |

---

## End-to-End Workflow

### 1. Create a Persona

```
POST /api/studio/personas    (multipart: image + prompt + name)
        │
        ├─► Save uploaded portrait
        ├─► Gemini Vision → detect apparent gender
        └─► Insert persona row, return entity
```

### 2. Generate an Avatar

```
POST /api/studio/avatars/preview        ←  fast preview (no Simli upload)
        │
        ├─► Gemini Image Edit (nano-banana)
        │     • prompt-chain: outfit → background → lighting
        │     • returns edited PNG/JPEG
        └─► Save preview, push WS event "avatar.preview"

POST /api/studio/avatars                ←  commit + register with Simli
        │
        ├─► Upload preview to Simli /faces/legacy
        │     • 3× retry with backoff on transient SSL/network errors
        ├─► Poll Simli for face generation status
        └─► Persist face_id, push WS events
```

### 3. Design or Clone a Voice

```
POST /api/studio/voices/design          ←  AI voice design
        │
        ├─► Gemini Vision → describe ideal voice from portrait
        ├─► ElevenLabs Voice Design → 3 candidates
        ├─► User picks one → persisted in voices table
        └─► Optionally link to a persona (voice_ref_id)

POST /api/studio/voices/clone           ←  voice cloning
POST /api/studio/voices/from-library    ←  pick a stock ElevenLabs voice
```

### 4. Build an Assistant

```
POST /api/studio/assistants
        │
        ├─► Snapshot persona's voice + face_id
        ├─► Set LLM provider/model
        └─► Insert assistant row (status=ready)
```

### 5. Start a Call

```
POST /api/studio/calls   { assistant_id }
        │
        ├─► Create unique LiveKit room
        ├─► Configure agent dispatch with assistant_id metadata
        ├─► Mint join token for the browser
        └─► Return { livekit_url, participant_token, room_name }

Browser → room.connect(url, token) → publishes microphone

LiveKit dispatches job → worker.entrypoint(ctx)
        │
        ├─► Load assistant from DB
        ├─► Read voice from PERSONA (authoritative — fresh on every call)
        ├─► Build STT (Deepgram), LLM (Gemini), TTS (ElevenLabs)
        ├─► Create AgentSession
        ├─► simli.AvatarSession.start(session, room)   ← redirects TTS audio
        │     to Simli via LiveKit DataStream (16 kHz PCM)
        ├─► Retry up to 3× if Simli rate-limits, verifying participant joined
        └─► session.start(agent, room) → conversation begins

Simli server
        │
        ├─► Receives PCM via DataStream
        ├─► Renders face animation locked to incoming audio
        └─► Publishes synced audio + video as RTC tracks → browser
```

---

## User Flow

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌─────────┐
│  Upload  │──►│ Generate │──►│  Design  │──►│   Build    │──►│  Call   │
│ portrait │   │  avatar  │   │  voice   │   │ assistant  │   │  live   │
└──────────┘   └──────────┘   └──────────┘   └────────────┘   └─────────┘
   persona       face_id      voice_id /         binds         WebRTC
   created       from Simli   ref_id          all together     conversation
```

The studio is organised around a **drag-and-drop** sidebar:

1. **Voices panel (top-left)** — designed/cloned voices and the ElevenLabs library.
2. **Personas panel** — each persona is a card; click to edit on the right.
3. **Avatars panel** — variant faces generated from a persona; each carries its own Simli `face_id`.
4. **Contacts panel** — completed assistants, ready to call.

To associate a voice with a persona, **drag the voice card onto the persona's voice slot**. Saving the persona propagates the new voice to every linked assistant — the next call uses the updated voice immediately, with no manual rebuild required.

---

## Setup

### Prerequisites

- **Docker** + **Docker Compose** (recommended) **or** Python 3.12 + [`uv`](https://docs.astral.sh/uv/)
- API keys for: LiveKit Cloud, Simli, ElevenLabs, Gemini (or OpenAI), Deepgram
- A modern Chromium-based browser (microphone permission required for calls)

### Installation

```bash
git clone https://github.com/SagarDeka-2299/LiveAvatar.git
cd LiveAvatar
cp .env.example .env
# populate your API keys in .env
```

For local development without Docker:

```bash
uv sync
```

---

## Configuration

All provider selection and credentials are controlled through `.env`. The complete variable set:

```bash
# ── Realtime infrastructure ──────────────────────────────────────────────────
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
SIMLI_API_KEY=...

# ── Provider API keys ─────────────────────────────────────────────────────────
OPENAI_API_KEY=...
GEMINI_API_KEY=...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...

# ── Task 1: gender detection (vision) ────────────────────────────────────────
GENDER_PROVIDER=gemini                       # openai | gemini
GENDER_MODEL_GEMINI=gemini-3-flash-preview
GENDER_MODEL_OPENAI=gpt-5-nano

# ── Task 2: avatar image edit ─────────────────────────────────────────────────
IMAGE_PROVIDER=gemini                        # openai | gemini
IMAGE_MODEL_GEMINI=gemini-3.1-flash-image-preview
IMAGE_MODEL_OPENAI=gpt-image-1

# ── Task 3: in-call LLM ───────────────────────────────────────────────────────
LLM_PROVIDER=gemini                          # openai | gemini
LLM_MODEL_OPENAI=gpt-5-mini
LLM_MODEL_GEMINI=gemini-3-flash-preview

# ── Task 4: speech-to-text ───────────────────────────────────────────────────
STT_PROVIDER=deepgram                        # deepgram | openai
STT_MODEL=nova-3-general

# ── Task 5: text-to-speech ───────────────────────────────────────────────────
TTS_PROVIDER=elevenlabs                      # elevenlabs | openai | google
TTS_MODEL=eleven_flash_v2_5
TTS_VOICE_ID=                                # leave blank → fallback Sarah voice

# ── Simli defaults ────────────────────────────────────────────────────────────
DEFAULT_SIMLI_FACE_ID=
DEFAULT_SIMLI_VOICE_PROVIDER=elevenlabs
DEFAULT_SIMLI_VOICE_MODEL=eleven_flash_v2_5
DEFAULT_SIMLI_VOICE_ID=
```

Every `*_PROVIDER` variable is hot-swappable — update the value and restart the service. There is no provider lock-in.

---

## Running Locally

### Docker (Recommended)

```bash
docker compose up --build -d
```

Navigate to `http://localhost:8000` to open the studio. Two containers start:

```
NAME                    STATUS    PORTS
avatar-agent-flow       Up        0.0.0.0:8000->8000/tcp
avatar-agent-worker     Up
```

**View logs:**

```bash
docker compose logs -f avatar-agent-flow      # API logs
docker compose logs -f avatar-agent-worker    # call/agent logs
```

**Stop:**

```bash
docker compose down
```

### Without Docker

Open two terminals — one per process:

```bash
# Terminal 1 — API + UI
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2 — LiveKit agent worker
uv run python livekit_agent/worker.py dev
```

---

## Project Structure

```
.
├── app/                          FastAPI backend
│   ├── main.py                   All HTTP + WebSocket routes
│   ├── config.py                 Settings (loaded from .env)
│   ├── db.py                     SQLite schema + CRUD
│   ├── schemas.py                Pydantic request/response models
│   ├── ai_router.py              Picks provider per task
│   ├── gemini_client.py          Gender detect, voice describe, image edit
│   ├── openai_client.py          Same surface for OpenAI
│   ├── elevenlabs_client.py      Voice design / clone / TTS / library
│   ├── simli_client.py           Face upload, status, agent / token APIs
│   ├── livekit_tokens.py         JWT minting + agent dispatch config
│   └── ws.py                     UpdateHub (WebSocket fan-out)
│
├── livekit_agent/
│   └── worker.py                 LiveKit agent entrypoint (per-call process)
│
├── static/                       Single-page studio UI
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── uploads/                  (gitignored) user portraits & generated frames
│
├── docker-compose.yml            Two-service deployment
├── Dockerfile                    Single image shared by both services
├── pyproject.toml                Python dependencies (uv)
└── .env / .env.example
```

---

## Database Schema

The application uses a single SQLite file (`app.db`, configurable via `AVATAR_DB_PATH`). Foreign key enforcement is enabled at connection time (`PRAGMA foreign_keys = ON`). All tables use `INTEGER PRIMARY KEY AUTOINCREMENT` and include a `created_at TEXT` column defaulting to `CURRENT_TIMESTAMP`.

### Entity-Relationship Overview

```
persona_entities
    │ 1
    │ has many
    ├──────────────► persona_avatars ──────────────────────┐
    │                    │ 1                               │
    │                    │ has many                        │
    └──────────────┐     └────────────► assistants ◄───────┘
                   │                     (persona_id FK +
                   │                      avatar_id FK,
                   └─────────────────────  both CASCADE DELETE)

voices  (standalone — optionally linked to a persona via persona_id,
         not a hard FK — persona can be deleted independently)
```

---

### `persona_entities`

Stores uploaded portraits and all voice data associated with a persona.

| Column | Type | Constraints | Default | Description |
|:---|:---:|:---|:---:|:---|
| `id` | INTEGER | PK AUTOINCREMENT | — | |
| `name` | TEXT | NOT NULL | — | Display name |
| `image_path` | TEXT | NOT NULL | — | Relative path to the uploaded portrait |
| `gender` | TEXT | NOT NULL | `'unknown'` | `male` \| `female` \| `unknown` — detected by Gemini/OpenAI |
| `status` | TEXT | NOT NULL | `'processing'` | `processing` \| `ready` \| `failed` \| `cancelled` |
| `progress` | INTEGER | NOT NULL | `0` | Completion percentage (0–100) |
| `stage` | TEXT | NOT NULL | `'queued'` | Human-readable pipeline stage label |
| `last_error` | TEXT | nullable | NULL | Most recent failure message |
| `voice_provider` | TEXT | NOT NULL | `''` | `elevenlabs` or empty |
| `voice_id` | TEXT | NOT NULL | `''` | ElevenLabs voice identifier |
| `voice_source` | TEXT | NOT NULL | `''` | `design` \| `clone` \| `library` |
| `voice_description` | TEXT | NOT NULL | `''` | Natural-language description used during voice design |
| `voice_sample_path` | TEXT | NOT NULL | `''` | Path to the uploaded clone sample |
| `voice_preview_path` | TEXT | NOT NULL | `''` | Path to the generated preview MP3 |
| `voice_status` | TEXT | NOT NULL | `''` | `processing` \| `ready` \| `failed` |
| `voice_last_error` | TEXT | nullable | NULL | Most recent voice job failure message |
| `voice_ref_id` | INTEGER | nullable | NULL | Soft link → `voices.id` (not enforced at DB level) |
| `created_at` | TEXT | — | `CURRENT_TIMESTAMP` | ISO-8601 timestamp |

> **Cascade:** deleting a `persona_entities` row cascades to `persona_avatars`, which in turn cascades to `assistants`.

---

### `persona_avatars`

One row per generated avatar variant. Stores the Simli `face_id` once registration completes.

| Column | Type | Constraints | Default | Description |
|:---|:---:|:---|:---:|:---|
| `id` | INTEGER | PK AUTOINCREMENT | — | |
| `persona_id` | INTEGER | NOT NULL, FK → `persona_entities(id)` ON DELETE CASCADE | — | |
| `name` | TEXT | NOT NULL | — | Display name |
| `decoration` | TEXT | NOT NULL | `''` | Internal decoration label |
| `theme_prompt` | TEXT | NOT NULL | `''` | Free-text style description used for image editing |
| `preset_ids` | TEXT | NOT NULL | `'[]'` | JSON array string of applied preset IDs |
| `face_id` | TEXT | NOT NULL | `''` | Simli face UUID; empty until registration completes |
| `image_path` | TEXT | NOT NULL | — | Relative path to the generated image |
| `status` | TEXT | NOT NULL | `'processing'` | `processing` \| `ready` \| `failed` \| `cancelled` |
| `progress` | INTEGER | NOT NULL | `0` | Completion percentage (0–100) |
| `stage` | TEXT | NOT NULL | `'queued'` | Pipeline stage label |
| `last_error` | TEXT | nullable | NULL | Most recent failure message |
| `created_at` | TEXT | — | `CURRENT_TIMESTAMP` | |

> **Cascade:** deleting a `persona_avatars` row cascades to `assistants`.

---

### `assistants`

A callable assistant — binding a persona's voice, a specific avatar's `face_id`, and an LLM configuration.

| Column | Type | Constraints | Default | Description |
|:---|:---:|:---|:---:|:---|
| `id` | INTEGER | PK AUTOINCREMENT | — | |
| `name` | TEXT | NOT NULL | — | Display name |
| `prompt` | TEXT | NOT NULL | — | LLM system prompt |
| `first_message` | TEXT | NOT NULL | — | Opening line spoken when a call starts |
| `persona_id` | INTEGER | NOT NULL, FK → `persona_entities(id)` ON DELETE CASCADE | — | |
| `avatar_id` | INTEGER | NOT NULL, FK → `persona_avatars(id)` ON DELETE CASCADE | — | |
| `face_id` | TEXT | NOT NULL | `''` | Simli face UUID — copied from the avatar at creation time |
| `simli_agent_id` | TEXT | NOT NULL | `''` | Simli agent ID (reserved; unused in the LiveKit-mediated path) |
| `voice_provider` | TEXT | NOT NULL | `''` | `elevenlabs` |
| `voice_id` | TEXT | nullable | NULL | ElevenLabs voice ID — re-read from the persona on every call |
| `voice_model` | TEXT | NOT NULL | `''` | e.g. `eleven_flash_v2_5` |
| `language` | TEXT | NOT NULL | `'en'` | BCP-47 language code |
| `llm_provider` | TEXT | NOT NULL | `''` | `gemini` \| `openai` |
| `llm_model` | TEXT | NOT NULL | `''` | e.g. `gemini-3-flash-preview` |
| `status` | TEXT | NOT NULL | `'processing'` | `processing` \| `ready` \| `failed` |
| `progress` | INTEGER | NOT NULL | `0` | Completion percentage (0–100) |
| `stage` | TEXT | NOT NULL | `'queued'` | Pipeline stage label |
| `last_error` | TEXT | nullable | NULL | Most recent failure message |
| `created_at` | TEXT | — | `CURRENT_TIMESTAMP` | |

> `voice_id` is stored here for reference, but the **worker always re-reads it from the persona** at call-start. A voice change in the UI takes effect on the very next call without rebuilding the assistant.

---

### `voices`

Standalone voice library — designed, cloned, or imported from ElevenLabs. Optionally associated with a persona via a soft link (no FK constraint), so deleting a persona does not remove its voices.

| Column | Type | Constraints | Default | Description |
|:---|:---:|:---|:---:|:---|
| `id` | INTEGER | PK AUTOINCREMENT | — | |
| `name` | TEXT | NOT NULL | — | Display name |
| `provider` | TEXT | NOT NULL | `'elevenlabs'` | Always `elevenlabs` currently |
| `voice_id` | TEXT | NOT NULL | `''` | ElevenLabs voice identifier |
| `source` | TEXT | NOT NULL | `''` | `design` \| `clone` \| `library` |
| `description` | TEXT | NOT NULL | `''` | Natural-language voice description |
| `sample_path` | TEXT | NOT NULL | `''` | Path to the uploaded clone sample (if cloned) |
| `preview_path` | TEXT | NOT NULL | `''` | Path to the preview MP3 |
| `persona_id` | INTEGER | nullable | NULL | Soft link to `persona_entities.id` (no FK constraint) |
| `status` | TEXT | NOT NULL | `'ready'` | `processing` \| `ready` \| `failed` |
| `last_error` | TEXT | nullable | NULL | Most recent failure message |
| `created_at` | TEXT | — | `CURRENT_TIMESTAMP` | |

---

### Cascade Delete Summary

| Deleted Row | Cascades To |
|:---|:---|
| `persona_entities` | All its `persona_avatars` rows → all `assistants` rows referencing those avatars |
| `persona_avatars` | All `assistants` rows referencing that avatar |
| `assistants` | Nothing (leaf table) |
| `voices` | Nothing — `persona_entities.voice_ref_id` is a soft link, not a foreign key |

---

## API Reference

Complete request/response schemas for every endpoint.

---

### Data Models

The following Pydantic models are reused across multiple endpoints.

#### `PersonaAvatar`

```jsonc
{
  "id": 1,                          // int
  "persona_id": 1,                  // int
  "name": "My Avatar",              // str
  "decoration": "",                 // str
  "theme_prompt": "",               // str
  "preset_ids": ["preset_id"],      // list[str]
  "face_id": "simli-face-uuid",     // str
  "image_path": "uploads/img.png",  // str
  "status": "ready",                // str: queued | processing | ready | failed
  "progress": 100,                  // int 0–100
  "stage": "done",                  // str
  "last_error": null                // str | null
}
```

#### `PersonaEntity`

```jsonc
{
  "id": 1,
  "name": "Lili",
  "image_path": "uploads/portrait.png",
  "gender": "female",               // str: male | female | unknown
  "status": "ready",
  "progress": 100,
  "stage": "done",
  "last_error": null,
  "voice_provider": "elevenlabs",   // str
  "voice_id": "el-voice-id",        // str
  "voice_source": "design",         // str: design | clone | library
  "voice_description": "Warm ...",  // str
  "voice_sample_path": "",          // str
  "voice_preview_path": "uploads/preview.mp3",
  "voice_status": "ready",          // str
  "voice_last_error": null,
  "avatars": [ /* PersonaAvatar[] */ ]
}
```

#### `VoiceEntity`

```jsonc
{
  "id": 1,
  "name": "Sarah Clone",
  "provider": "elevenlabs",         // str
  "voice_id": "el-voice-id",        // str
  "source": "clone",                // str: design | clone | library
  "description": "Clear, warm...",  // str
  "sample_path": "",                // str
  "preview_path": "uploads/v.mp3",  // str
  "persona_id": null,               // int | null
  "status": "ready",                // str: processing | ready | failed
  "last_error": null,
  "created_at": "2024-01-01T00:00:00"
}
```

#### `Assistant`

```jsonc
{
  "id": 1,
  "name": "Lili Assistant",
  "prompt": "You are Lili...",       // str — system prompt
  "first_message": "Hi, how can I help?",
  "persona_id": 1,
  "avatar_id": 2,
  "face_id": "simli-face-uuid",
  "simli_agent_id": "",             // str
  "voice_provider": "elevenlabs",
  "voice_id": "el-voice-id",        // str | null
  "voice_model": "eleven_flash_v2_5",
  "language": "en",
  "llm_provider": "gemini",
  "llm_model": "gemini-3-flash-preview",
  "status": "ready",
  "progress": 100,
  "stage": "done",
  "last_error": null
}
```

#### `AssistantCallResponse`

```jsonc
{
  "assistant_id": 1,
  "assistant_name": "Lili Assistant",
  "room_name": "room-uuid",
  "livekit_url": "wss://project.livekit.cloud",
  "participant_token": "<livekit-jwt>",
  "participant_identity": "user-uuid"
}
```

---

### System Endpoints

#### `GET /api/health`

Health check.

**Response `200`**
```json
{ "status": "ok" }
```

---

#### `GET /api/config`

Client bootstrap configuration. Safe to expose — contains no secret values.

**Response `200`**
```jsonc
{
  "livekit_url": "wss://project.livekit.cloud",
  "has_livekit_creds": true,
  "has_simli_api_key": true,
  "simli_widget_script": "https://cdn.simli.ai/..."
}
```

---

#### `GET /api/studio/presets`

Returns all available avatar theme presets and voice presets.

**Response `200`**
```jsonc
{
  "avatar_presets": [
    {
      "id": "corporate_female",
      "label": "Corporate Female",
      "cat": "industry",
      "gender": "female"
    }
    // ...
  ],
  "voice_presets": [
    { "id": "warm_narrator", "label": "Warm Narrator" }
    // ...
  ]
}
```

---

### Personas

#### `GET /api/studio/personas`

Returns all personas with their nested avatars.

**Response `200`** — `PersonaEntity[]`

---

#### `POST /api/studio/personas`

Creates a persona from an uploaded portrait.

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `persona_image` | file | Yes | PNG / JPG / WEBP |
| `name` | string | No | Default: `"Persona"` |
| `client_id` | string | No | WebSocket client ID for progress events |

**Response `201`** — `PersonaEntity`

**Error Responses**
- `400` — unsupported image format

---

#### `PATCH /api/studio/personas/{persona_id}`

Renames a persona or updates its linked voice. Changing `voice_ref_id` propagates the new voice to every associated assistant.

**Request** — `application/json`
```jsonc
{
  "name": "New Name",               // str (optional)
  "voice_ref_id": 3                 // int | null (optional) — links a VoiceEntity
}
```

**Response `200`** — `PersonaEntity`

**Error Responses**
- `404` — persona not found

---

#### `DELETE /api/studio/personas/{persona_id}`

Deletes a persona and cascade-deletes all associated avatars and assistants.

**Response `200`**
```json
{ "deleted": 1 }
```

---

#### `GET /api/studio/personas/{persona_id}/cascade-count`

Returns the number of records that will be deleted — use to confirm before a destructive operation.

**Response `200`**
```json
{ "avatars": 2, "assistants": 1 }
```

---

#### `POST /api/studio/personas/{persona_id}/cancel`

Cancels an in-progress persona creation job.

**Response `200`**
```json
{ "status": "cancelled" }
```

---

#### `POST /api/studio/personas/{persona_id}/voice/design`

Triggers AI voice design for a persona. Gemini analyses the portrait and describes an ideal voice; ElevenLabs Voice Design then generates candidates.

**Request** — `application/json`
```jsonc
{
  "user_prompt": "Warm, professional tone",  // str, max 500 chars (optional)
  "name": "My Voice",                        // str, max 100 chars (optional)
  "client_id": "ws-client-id"               // str | null (optional)
}
```

**Response `200`** — `PersonaEntity` (with updated voice fields)

**Error Responses**
- `404` — persona not found
- `409` — voice design already in progress

---

#### `POST /api/studio/personas/{persona_id}/voice/clone`

Clones a voice from an uploaded audio sample and attaches it to a persona.

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `voice_sample` | file | Yes | Audio file, max 20 MB |
| `name` | string | No | Voice display name |
| `client_id` | string | No | WebSocket client ID |

**Response `200`** — `PersonaEntity` (with updated voice fields)

**Error Responses**
- `400` — file exceeds 20 MB limit
- `404` — persona not found

---

#### `DELETE /api/studio/personas/{persona_id}/voice`

Unlinks and deletes the voice attached to a persona.

**Response `200`** — `PersonaEntity` (voice fields cleared)

---

### Avatars

#### `POST /api/studio/avatars/preview`

Generates an AI-edited avatar image without registering it with Simli. Use this endpoint to iterate on styles before committing.

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `persona_id` | int | Yes | Source persona |
| `name` | string | Yes | Display name |
| `theme_prompt` | string | No | Free-text style description |
| `preset_ids` | string | No | JSON array string, e.g. `'["corporate_female"]'` |
| `custom_prompt` | string | No | Additional free-form instruction |
| `client_id` | string | No | WebSocket client ID |

**Response `200`**
```json
{ "avatar_id": 7 }
```

The WebSocket broadcasts `avatar.preview_ready` with `preview_path` once the image is ready.

---

#### `POST /api/studio/avatars`

Commits a draft avatar and registers it with Simli to obtain a `face_id`.

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `persona_id` | int | Yes | |
| `name` | string | Yes | |
| `decoration` | string | No | |
| `theme_prompt` | string | No | |
| `preset_ids` | string | No | JSON array string |
| `draft_avatar_id` | int | No | Re-use an existing preview |
| `client_id` | string | No | WebSocket client ID |

**Response `201`** — `PersonaAvatar`

The WebSocket broadcasts `avatar.uploading` → `avatar.queued` → `avatar.ready` (or `avatar.failed`).

---

#### `GET /api/studio/avatars/{avatar_id}/cascade-count`

Returns the number of records that will be deleted upon avatar removal.

**Response `200`**
```json
{ "assistants": 1 }
```

---

#### `DELETE /api/studio/avatars/{avatar_id}`

Deletes an avatar and its associated assistants. Unregisters the face from Simli if it was previously registered.

**Response `200`**
```json
{ "deleted": 1 }
```

---

#### `POST /api/studio/avatars/{avatar_id}/retry`

Re-runs a failed Simli upload for an avatar.

**Query Parameters**

| Parameter | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `client_id` | string | No | WebSocket client ID |

**Response `200`**
```json
{ "status": "retrying" }
```

---

#### `POST /api/studio/avatars/{avatar_id}/cancel`

Cancels an in-progress avatar generation or Simli upload.

**Response `200`**
```json
{ "status": "cancelled" }
```

---

### Voices

#### `GET /api/studio/voices`

Returns all user-created voices (designed, cloned, or added from the library).

**Response `200`** — `VoiceEntity[]`

---

#### `POST /api/studio/voices/design`

Designs a new standalone voice from a text description (not tied to a specific persona).

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `name` | string | Yes | Display name |
| `description` | string | No | Natural-language voice description |
| `voice_preset_id` | string | No | ID from `GET /api/studio/presets` |
| `persona_id` | int | No | If provided, Gemini auto-generates the description from the portrait |
| `include_persona_traits` | bool | No | Default `false` |
| `client_id` | string | No | WebSocket client ID |

**Response `201`** — `VoiceEntity`

---

#### `POST /api/studio/voices/clone`

Clones a voice from an audio sample as a standalone voice.

**Request** — `multipart/form-data`

| Field | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `voice_sample` | file | Yes | Audio file, max 20 MB |
| `name` | string | No | Display name |
| `client_id` | string | No | WebSocket client ID |

**Response `201`** — `VoiceEntity`

**Error Responses**
- `400` — file exceeds 20 MB limit

---

#### `POST /api/studio/voices/suggest-description`

Uses Gemini to suggest a voice description based on a persona's portrait. Intended for pre-filling the voice design form.

**Request** — `application/json`
```jsonc
{
  "persona_id": 1,        // int — required
  "user_hint": "upbeat"   // str — optional additional guidance
}
```

**Response `200`**
```json
{ "description": "A warm, confident female voice with a slight mid-Atlantic accent..." }
```

---

#### `GET /api/studio/voices/library`

Browses the ElevenLabs public Voice Library.

**Response `200`**
```jsonc
[
  {
    "voice_id": "el-voice-id",
    "name": "Rachel",
    "preview_url": "https://...",
    "category": "premade",
    "labels": { "accent": "american", "gender": "female" }
  }
  // ...
]
```

---

#### `POST /api/studio/voices/from-library`

Adds an ElevenLabs library voice to the studio.

**Request** — `application/json`
```jsonc
{
  "voice_id": "el-voice-id",       // str — required
  "name": "Rachel",                // str — required
  "preview_url": "https://..."     // str — required
}
```

**Response `201`** — `VoiceEntity`

---

#### `GET /api/studio/voices/preview-default`

Streams the default voice preview MP3. Used as a fallback when no custom preview exists.

**Response `200`** — `audio/mpeg` binary stream

---

#### `DELETE /api/studio/voices/{voice_id}`

Deletes a voice from the studio. Also removes it from ElevenLabs if it was designed or cloned.

**Response `200`**
```json
{ "deleted": 1 }
```

---

#### `POST /api/studio/voices/{voice_id}/retry`

Retries a failed voice design or clone job.

**Query Parameters**

| Parameter | Type | Required | Notes |
|:---|:---:|:---:|:---|
| `client_id` | string | No | WebSocket client ID |

**Response `200`**
```json
{ "status": "retrying" }
```

---

### Assistants

#### `GET /api/studio/assistants`

Returns all assistants.

**Response `200`** — `Assistant[]`

---

#### `POST /api/studio/assistants`

Creates an assistant by binding a persona, avatar, and LLM configuration.

**Request** — `application/json`
```jsonc
{
  "name": "Lili Assistant",         // str, 1–100 chars — required
  "prompt": "You are Lili...",      // str, min 1 char — required (system prompt)
  "first_message": "Hi, how can I help?",  // str (optional, has default)
  "persona_id": 1,                  // int — required
  "avatar_id": 2,                   // int — required
  "llm_provider": "gemini",         // str | null — falls back to env default
  "llm_model": "gemini-3-flash-preview",  // str | null — falls back to env default
  "client_id": "ws-client-id"      // str | null (optional)
}
```

**Response `201`** — `Assistant`

**Error Responses**
- `404` — persona or avatar not found
- `400` — avatar has no registered `face_id` yet

---

#### `DELETE /api/studio/assistants/{assistant_id}`

Deletes an assistant.

**Response `200`**
```json
{ "deleted": 1 }
```

---

### Calls

#### `POST /api/studio/calls`

Initiates a live video call with an assistant. Creates a LiveKit room, dispatches the agent worker, and returns a browser join token.

**Request** — `application/json`
```jsonc
{
  "assistant_id": 1   // int — required
}
```

**Response `200`** — `AssistantCallResponse`
```jsonc
{
  "assistant_id": 1,
  "assistant_name": "Lili Assistant",
  "room_name": "room-uuid",
  "livekit_url": "wss://project.livekit.cloud",
  "participant_token": "<livekit-jwt>",
  "participant_identity": "user-uuid"
}
```

The browser connects using:
```js
room.connect(livekit_url, participant_token)
```

**Error Responses**
- `404` — assistant not found
- `400` — assistant is not in the `ready` state

---

### Infrastructure Endpoints

#### `POST /api/simli/session-token`

Mints a Simli session token for use by the browser Simli widget.

**Request** — `application/json`
```jsonc
{
  "create_transcript": true,   // bool (default: true)
  "expiry_stamp": -1           // int (default: -1 = no expiry)
}
```

**Response `200`** — Simli session token response (pass-through from the Simli API)

---

#### `POST /api/livekit/token`

Mints a custom LiveKit participant token.

**Request** — `application/json`
```jsonc
{
  "room_name": "my-room",         // str, 1–128 chars — required
  "identity": "user-123",         // str, 1–128 chars — required
  "participant_name": "Alice"     // str | null (optional)
}
```

**Response `200`**
```jsonc
{
  "token": "<livekit-jwt>",
  "livekit_url": "wss://project.livekit.cloud",
  "room_name": "my-room",
  "identity": "user-123"
}
```

---

#### `POST /api/simli/cleanup`

Purges all Simli agents, faces, and local database records.

> **Warning:** This operation is destructive and irreversible. Use with care.

**Response `200`**
```jsonc
{
  "deleted_agents": 3,
  "deleted_faces": 5,
  "local_assistants_deleted": 3,
  "local_avatars_deleted": 5,
  "local_personas_deleted": 2,
  "local_uploads_deleted": 10,
  "errors": []
}
```

---

### WebSocket

#### `WS /ws/updates/{client_id}`

Connect using any unique `client_id` string. The server fans out progress events as JSON frames.

**Event Envelope**
```jsonc
{
  "event": "<event-name>",
  // ...event-specific fields
}
```

**Persona Events**

| Event | Key Fields |
|:---|:---|
| `persona.queued` | `id`, `status` |
| `persona.ready` | `id`, `status`, `gender` |
| `persona.deleted` | `id` |
| `persona.cancelled` | `id` |
| `persona.voice` | `id`, `voice_status`, `voice_id`, `voice_preview_path` |
| `persona.voice_delete` | `id` |

**Avatar Events**

| Event | Key Fields |
|:---|:---|
| `avatar.generating` | `id`, `persona_id`, `progress`, `stage` |
| `avatar.preview_ready` | `id`, `persona_id`, `preview_path` |
| `avatar.uploading` | `id`, `persona_id`, `stage` |
| `avatar.queued` | `id`, `persona_id`, `face_id` |
| `avatar.ready` | `id`, `persona_id`, `face_id`, `status` |
| `avatar.failed` | `id`, `persona_id`, `last_error` |
| `avatar.deleted` | `id`, `persona_id` |
| `avatar.cancelled` | `id`, `persona_id` |
| `avatar.retrying` | `id`, `persona_id` |

**Voice Events**

| Event | Key Fields |
|:---|:---|
| `voice.status` | `id`, `status`, `progress`, `stage` |
| `voice.done` | `id`, `status`, `voice_id`, `preview_path` |
| `voice.deleted` | `id` |
| `voice.retrying` | `id` |

**Assistant & Miscellaneous Events**

| Event | Key Fields |
|:---|:---|
| `assistant.ready` | `id`, `status` |
| `assistant.deleted` | `id` |
| `simli.status` | `avatar_id`, `stage`, `progress` |
| `api.uploading` | `label` |

---

## How Calls Work Under the Hood

The most nuanced component is the **TTS → Simli → browser** audio-video pipeline. The following sequence describes what happens for a single spoken sentence:

```
1. User speaks                        ──► Browser publishes mic to LiveKit room
2. Worker subscribes to user audio    ──► Deepgram STT streams partial transcripts
3. Final transcript                   ──► Gemini LLM generates a reply (token stream)
4. Reply tokens                       ──► ElevenLabs TTS (Flash v2.5, streaming PCM)
5. Agent.output.audio                 ──► DataStreamAudioOutput
                                            • resamples to 16 kHz
                                            • LiveKit data channel → simli-avatar-agent
6. Simli render server                ──► generates face video locked to PCM
                                            • re-publishes audio + video tracks
7. Browser <video> element receives   ──► both tracks attached to ONE element
                                            so the browser locks them to one
                                            media clock (perfect AV sync)
```

**Three implementation details that required careful engineering:**

- **Voice freshness on every call.** The worker reads the voice configuration from the **persona** (not the assistant snapshot), so a voice change in the UI is reflected on the very next call without requiring an assistant rebuild.

- **Simli rate-limit recovery.** `avatar.start()` silently swallows HTTP 429 responses. The implementation wraps the call in a 3× retry loop that polls `room.remote_participants` for the `simli-avatar-agent` identity to confirm the connection, rather than trusting the silent return value.

- **AV sync without re-encoding.** Both Simli's audio and video tracks are attached to the *same* `<video>` element. LiveKit's `track.attach()` accumulates tracks into one `MediaStream`, and the browser maintains them on a single media clock — the only reliable way to guarantee synchronisation without re-encoding.

> **Note:** This implementation uses the LiveKit-mediated Simli pattern (per LiveKit's documentation). It provides full provider flexibility (any STT/LLM/TTS combination) at the cost of a few additional hops. For ultra-low-latency sync, the **Simli Auto** mode (direct browser ↔ Simli WebRTC) is available — `app/simli_client.py` already exposes `create_auto_session_token` for that path.

---

## Troubleshooting

| Symptom | Likely Cause | Resolution |
|:---|:---|:---|
| Call shows "Connecting…" indefinitely | Simli `avatar.start()` received a 429 (rate limit) | Wait briefly; the worker auto-retries 3×. Check `docker compose logs avatar-agent-worker` for `failed to connect to simli`. |
| Audio plays but no avatar video | Simli session token failed to mint | Verify `SIMLI_API_KEY` is valid and that the assistant's `face_id` status is `ready` (not `processing`). |
| Audio and video are out of sync | Browser cached a stale `app.js` | Hard-refresh the page (`Cmd+Shift+R`). The cache-buster in `index.html` should prevent this in most cases. |
| Voice in call is incorrect (always Sarah) | Stale assistant row | This is resolved — the worker now reads the voice from the persona. If the issue persists, restart the worker container. |
| `Gemini image edit failed (400)` | `responseModalities` includes `TEXT` | Pull the latest version — the value must be `["IMAGE"]` only for `gemini-3.1-flash-image-preview`. |
| Simli face upload SSL error | Transient TLS failure from `api.simli.ai` | Auto-retried (3× with backoff). If the error persists, check the [Simli status page](https://simli.com). |

---

## Credits

Built by **Sagar Deka**.

Powered by [LiveKit](https://livekit.io) · [Simli](https://simli.com) · [ElevenLabs](https://elevenlabs.io) · [Gemini](https://ai.google.dev) · [Deepgram](https://deepgram.com) · [OpenAI](https://openai.com)
