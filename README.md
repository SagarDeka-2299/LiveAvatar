# Lili Studio

> A studio for building **AI avatar assistants** — design a persona, generate a photorealistic face, design or clone a voice, then call your assistant in a real-time video conversation.

Lili Studio is a full-stack workbench that lets you go from **idea → photoreal avatar → voice-acted assistant → live video call** in a few clicks. It glues together best-in-class providers (Simli, LiveKit, ElevenLabs, Gemini, Deepgram) behind one drag-and-drop UI.

---

## Table of contents

- [What you can do](#what-you-can-do)
- [Tech stack](#tech-stack)
- [Architecture](#architecture)
- [End-to-end workflow](#end-to-end-workflow)
- [User flow](#user-flow)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Project structure](#project-structure)
- [API surface](#api-surface)
- [How calls work under the hood](#how-calls-work-under-the-hood)
- [Troubleshooting](#troubleshooting)

---

## What you can do

| Feature | What it does |
|---|---|
| **Personas** | Upload a portrait, auto-detect gender, store identity + system prompt. |
| **Avatar generator** | AI-edit the portrait (background, outfit, lighting presets) using Gemini `nano-banana` / OpenAI `gpt-image-1`. Register the result with Simli to get a `face_id` for live animation. |
| **Voice design** | Describe a voice in natural language → ElevenLabs Voice Design synthesises an AI voice tailored to the persona's portrait. |
| **Voice cloning** | Upload a 30s audio sample → ElevenLabs Voice Clone produces a faithful copy. |
| **Voice library** | Browse the ElevenLabs Voice Library and one-click-add any voice into your studio. |
| **Assistants** | Bind one persona + one avatar + one voice + one LLM into a callable assistant. |
| **Live video calls** | Click **Start Call** → instant WebRTC call with your assistant. The avatar lip-syncs to TTS audio in real time. |
| **Streaming updates** | A WebSocket pipes live progress (avatar generating, voice processing, Simli registering…) into the UI. |

---

## Tech stack

### Core
- **FastAPI** — async HTTP + WebSocket server
- **uv** — Python dependency manager (fast, reproducible)
- **SQLite** — embedded persistence (personas, voices, avatars, assistants)
- **Vanilla JS + HTML + CSS** — single-page studio UI, zero framework overhead
- **Docker Compose** — two-service deployment (`flow` API + `worker` LiveKit agent)

### Realtime stack
- **LiveKit Cloud** — WebRTC SFU, room management, agent dispatch
- **LiveKit Agents (Python)** — orchestrates STT → LLM → TTS → avatar pipeline
- **Simli** — photoreal avatar face animation + lip-sync (joins LiveKit room as a virtual participant)

### AI providers (all swappable via `.env`)
| Task | Default | Alternatives |
|---|---|---|
| Gender detection | Gemini 3 Flash | OpenAI GPT-5-nano |
| Image edit (avatar) | Gemini 3.1 Flash Image | OpenAI `gpt-image-1` |
| In-call LLM | Gemini 3 Flash | OpenAI GPT-5-mini |
| STT | Deepgram Nova-3 | OpenAI Whisper |
| TTS | ElevenLabs Flash v2.5 | OpenAI / Google |
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

Two containers, one shared SQLite volume:

| Service | Role |
|---|---|
| `avatar-agent-flow` | FastAPI app on `:8000`. Serves the studio UI, REST API, and WebSocket. |
| `avatar-agent-worker` | LiveKit agent. Idle until a call comes in, then spawns a per-call subprocess. |

---

## End-to-end workflow

### 1. Create a persona

```
POST /api/studio/personas    (multipart: image + prompt + name)
        │
        ├─► Save uploaded portrait
        ├─► Gemini Vision → detect apparent gender
        └─► Insert persona row, return entity
```

### 2. Generate an avatar

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

### 3. Design or clone a voice

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

### 4. Build an assistant

```
POST /api/studio/assistants
        │
        ├─► Snapshot persona's voice + face_id
        ├─► Set LLM provider/model
        └─► Insert assistant row (status=ready)
```

### 5. Start a call

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

## User flow

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌─────────┐
│  Upload  │──►│ Generate │──►│  Design  │──►│   Build    │──►│  Call   │
│ portrait │   │  avatar  │   │  voice   │   │ assistant  │   │  live   │
└──────────┘   └──────────┘   └──────────┘   └────────────┘   └─────────┘
   persona       face_id      voice_id /         binds         WebRTC
   created       from Simli   ref_id          all together     conversation
```

The studio is built around a **drag-and-drop** sidebar:

1. **Voices panel (top-left)** — your designed/cloned voices + the ElevenLabs library.
2. **Personas panel** — every persona is a card; click one to edit on the right.
3. **Avatars panel** — variant faces generated from a persona; each has its own Simli `face_id`.
4. **Contacts panel** — finished assistants, ready to call.

To bind a voice to a persona, just **drag the voice card onto the persona's voice slot**. Saving the persona propagates the new voice to every assistant linked to that persona — so the next call uses the new voice immediately, no manual rebuild needed.

---

## Setup

### Prerequisites

- **Docker** + **Docker Compose** (recommended) **or** Python 3.12 + [`uv`](https://docs.astral.sh/uv/)
- API keys for: LiveKit Cloud, Simli, ElevenLabs, Gemini (or OpenAI), Deepgram
- A modern Chromium-based browser for the studio (microphone permission required for calls)

### Clone & install

```bash
git clone https://github.com/SagarDeka-2299/LiveAvatar.git
cd LiveAvatar
cp .env.example .env
# fill in your API keys
```

For non-Docker development:

```bash
uv sync
```

---

## Configuration

`.env` controls every provider. The full set:

```bash
# ── Realtime infra ──
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
SIMLI_API_KEY=...

# ── Provider keys ──
OPENAI_API_KEY=...
GEMINI_API_KEY=...
DEEPGRAM_API_KEY=...
ELEVENLABS_API_KEY=...

# ── Task 1: gender detection (vision) ──
GENDER_PROVIDER=gemini                       # openai | gemini
GENDER_MODEL_GEMINI=gemini-3-flash-preview
GENDER_MODEL_OPENAI=gpt-5-nano

# ── Task 2: avatar image edit ──
IMAGE_PROVIDER=gemini                        # openai | gemini
IMAGE_MODEL_GEMINI=gemini-3.1-flash-image-preview
IMAGE_MODEL_OPENAI=gpt-image-1

# ── Task 3: in-call LLM ──
LLM_PROVIDER=gemini                          # openai | gemini
LLM_MODEL_OPENAI=gpt-5-mini
LLM_MODEL_GEMINI=gemini-3-flash-preview

# ── Task 4: STT ──
STT_PROVIDER=deepgram                        # deepgram | openai
STT_MODEL=nova-3-general

# ── Task 5: TTS ──
TTS_PROVIDER=elevenlabs                      # elevenlabs | openai | google
TTS_MODEL=eleven_flash_v2_5
TTS_VOICE_ID=                                # leave blank → fallback Sarah voice

# ── Simli defaults ──
DEFAULT_SIMLI_FACE_ID=
DEFAULT_SIMLI_VOICE_PROVIDER=elevenlabs
DEFAULT_SIMLI_VOICE_MODEL=eleven_flash_v2_5
DEFAULT_SIMLI_VOICE_ID=
```

Every `*_PROVIDER` env var is hot-swappable — change it, restart, done. There is no provider lock-in.

---

## Running locally

### Docker (recommended)

```bash
docker compose up --build -d
```

Open `http://localhost:8000` and you're in the studio. Two containers come up:

```
NAME                    STATUS    PORTS
avatar-agent-flow       Up        0.0.0.0:8000->8000/tcp
avatar-agent-worker     Up
```

Logs:

```bash
docker compose logs -f avatar-agent-flow      # API logs
docker compose logs -f avatar-agent-worker    # call/agent logs
```

Stop:

```bash
docker compose down
```

### Without Docker

Two terminals — one per process:

```bash
# Terminal 1 — API + UI
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2 — LiveKit agent worker
uv run python livekit_agent/worker.py dev
```

---

## Project structure

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
├── Dockerfile                    Single image, both services share it
├── pyproject.toml                Python deps (uv)
└── .env / .env.example
```

---

## API surface

A pragmatic overview — see `app/main.py` for full request/response shapes.

### Personas

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/studio/personas` | List with nested avatars |
| `POST` | `/api/studio/personas` | Create from portrait + prompt |
| `PATCH`| `/api/studio/personas/{id}` | Rename or change linked voice (propagates to assistants) |
| `DELETE` | `/api/studio/personas/{id}` | Cascade delete |
| `POST` | `/api/studio/personas/{id}/voice/design` | Trigger AI voice design |
| `POST` | `/api/studio/personas/{id}/voice/clone` | Clone from uploaded sample |

### Avatars

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/studio/avatars/preview` | Generate edited image (no Simli register) |
| `POST` | `/api/studio/avatars` | Commit preview + register with Simli |
| `POST` | `/api/studio/avatars/{id}/retry` | Re-run a failed Simli upload |
| `DELETE` | `/api/studio/avatars/{id}` | Delete + Simli unregister |

### Voices

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/studio/voices` | List user voices |
| `GET`  | `/api/studio/voices/library` | Browse the ElevenLabs Voice Library |
| `POST` | `/api/studio/voices/design` | Synthesise a voice from a description |
| `POST` | `/api/studio/voices/clone` | Clone from an audio sample |
| `POST` | `/api/studio/voices/from-library` | Add a library voice into the studio |

### Assistants & calls

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/studio/assistants` | List ready-to-call assistants |
| `POST` | `/api/studio/assistants` | Create assistant from persona + avatar + LLM |
| `POST` | `/api/studio/calls` | Mint a LiveKit token + dispatch the agent |

### Realtime

| Path | Purpose |
|---|---|
| `WS  /ws/updates/{client_id}` | Live progress: avatar generating, voice ready, errors |

---

## How calls work under the hood

The most subtle piece is the **TTS → Simli → browser** path. Here's exactly what happens for one spoken sentence:

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

Three details that took real engineering to get right:

- **Voice freshness on every call.** The worker reads the voice from the **persona** (not the assistant snapshot) so changing a voice in the UI reflects on the very next call without rebuilding the assistant.
- **Simli rate-limit recovery.** `avatar.start()` silently swallows 429s — we wrap it in a 3× retry that polls `room.remote_participants` for the `simli-avatar-agent` identity to verify connection, instead of trusting the (silent) return.
- **AV sync without hacks.** Both Simli's audio and video tracks are attached to the *same* `<video>` element. LiveKit's `track.attach()` accumulates tracks into one `MediaStream`, and the browser keeps them on one media clock — the only way to guarantee sync without re-encoding.

> **Note:** This is the LiveKit-mediated Simli pattern (per LiveKit's docs). It gives you provider flexibility (any STT/LLM/TTS) at the cost of a few extra hops. For ultra-tight sync you can switch to **Simli Auto** (browser ↔ Simli direct WebRTC) — `app/simli_client.py` already exposes `create_auto_session_token` for that path.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Call shows "Connecting…" forever | Simli `avatar.start()` got 429 (rate limit) | Wait a moment; the worker auto-retries 3× — check `docker compose logs avatar-agent-worker` for `failed to connect to simli`. |
| Audio plays but no avatar video | Simli session token failed to mint | Verify `SIMLI_API_KEY` and that the assistant's `face_id` is `ready` (not `processing`). |
| Audio/video out of sync | Browser cached old `app.js` | Hard refresh (`Cmd+Shift+R`); the cache-buster in `index.html` should already prevent this. |
| Voice in call is wrong (always Sarah) | Stale assistant row | Already fixed — worker now reads voice from the persona. If you still see this, restart the worker container. |
| `Gemini image edit failed (400)` | `responseModalities` includes `TEXT` | Pull latest — must be `["IMAGE"]` only for `gemini-3.1-flash-image-preview`. |
| Simli face upload SSL error | Transient TLS drop from `api.simli.ai` | Auto-retried (3× with backoff). If it persists, check Simli status. |

---

## License & credits

Built by Sagar Deka.
Powered by [LiveKit](https://livekit.io), [Simli](https://simli.com), [ElevenLabs](https://elevenlabs.io), [Gemini](https://ai.google.dev), [Deepgram](https://deepgram.com), and [OpenAI](https://openai.com).
