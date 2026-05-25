# Lili Studio API

A multi-tenant FastAPI service that turns a user-supplied portrait + voice
into a real-time AI assistant joinable as a LiveKit room. Every request is
scoped to a `tenant_id` whose database (Postgres) and media store (Azure
Blob) are resolved at request time from Azure Key Vault. A working reference
front-end is mounted at `/demo`.

---

## Setup

### Docker

The repo ships with a `Dockerfile`. Build, then run with the env vars below.

```bash
docker build -t lili-api .
docker run --rm -p 8000:8000 --env-file .env lili-api
# OpenAPI docs at http://localhost:8000/docs
```

### Environment variables

Only the API container reads these — per-tenant Postgres / Azure Storage
credentials live in Key Vault and are not env vars.

| Variable | Purpose |
| --- | --- |
| `AZURE_KEYVAULT_URL` | `https://<vault>.vault.azure.net/` |
| `AZURE_CLIENT_ID` | Service-principal app id |
| `AZURE_TENANT_ID` | Azure AD tenant of the SP (≠ our customer `tenant_id`) |
| `AZURE_CLIENT_SECRET` | Service-principal secret |
| `TENANT_SECRET_TTL_SECONDS` | In-process cache TTL for per-tenant secrets (default `600`) |
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | LiveKit room credentials |
| `SIMLI_API_KEY` | Simli avatar API key |
| `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` | AI provider keys |
| `GENDER_PROVIDER`, `GENDER_MODEL_GEMINI`, `GENDER_MODEL_OPENAI` | Vision gender classifier |
| `IMAGE_PROVIDER`, `IMAGE_MODEL_GEMINI`, `IMAGE_MODEL_OPENAI` | Avatar image editor |
| `LLM_PROVIDER`, `LLM_MODEL_OPENAI`, `LLM_MODEL_GEMINI` | In-call LLM |
| `STT_PROVIDER`, `STT_MODEL` | In-call STT |
| `TTS_PROVIDER`, `TTS_MODEL`, `TTS_VOICE_ID` | In-call TTS defaults |
| `DEFAULT_SIMLI_FACE_ID`, `DEFAULT_SIMLI_VOICE_PROVIDER`, `DEFAULT_SIMLI_VOICE_MODEL`, `DEFAULT_SIMLI_VOICE_ID` | Fallback Simli avatar |
| `LOCAL_TENANT_ID` | Sentinel id that triggers local fallback. Default `local_tenant` |
| `LOCAL_DATA_DIR` | Where the local fallback writes SQLite + blob files. Default `./local_data` |
| `LOCAL_BLOB_BASE_URL` | URL stamped into rows when running the local fallback. Default `http://localhost:8000/local-blob` |

### Quick local test

Send `tenant_id="local_tenant"` (or whatever `LOCAL_TENANT_ID` resolves to)
on any request. The backend transparently swaps Postgres for a per-tenant
SQLite file and Azure Blob for `LOCAL_DATA_DIR/<tenant_id>/blob/`. No Azure
setup required. Reset the local state by deleting `LOCAL_DATA_DIR`.

---

## Azure Key Vault — per-tenant secrets

For each customer tenant the operator pre-creates these secrets in Key Vault:

| Secret name | Value |
| --- | --- |
| `{tenant_id}-db-url` | Postgres URL, e.g. `postgresql+asyncpg://user:pass@host:5432/db` |
| `{tenant_id}-blob-account-url` | `https://<account>.blob.core.windows.net/` |
| `{tenant_id}-blob-account-name` | `<account>` |
| `{tenant_id}-blob-account-key` | Access key |
| `{tenant_id}-blob-container` | Container name |

**`tenant_id` charset**: `[a-zA-Z0-9-]{1,63}` (Key Vault secret-name rules).

**Container ACL**: must be **anonymous read** (`public-access blob`) — the
API hands back the bare blob URL and never signs it. Without that ACL,
returned URLs return 403.

---

## Database schema (per tenant)

Four tables. Migrations live under `alembic/versions/` and are run against
the per-tenant Postgres DB when the tenant is provisioned.

### `persona_entities`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `name` | string | display name |
| `image_url` | text | blob URL of the source portrait |
| `gender` | string | `male`, `female`, or `unknown` (vision-classified) |
| `status`, `stage`, `progress`, `last_error` | string / int | background-task state |
| `voice_provider`, `voice_id`, `voice_source` | string | currently attached voice |
| `voice_description`, `voice_sample_url`, `voice_preview_url` | text | |
| `voice_status`, `voice_last_error` | string | |
| `voice_ref_id` | int? | id of the `voices` row this persona references |
| `created_at` | timestamp | |

### `persona_avatars`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `persona_id` | int? FK → `persona_entities.id` (`ON DELETE SET NULL`) | nulls when persona deleted |
| `name`, `decoration`, `theme_prompt`, `preset_ids` | text | |
| `face_id` | string | Simli face id |
| `image_url` | text | blob URL of the styled preview |
| `status`, `stage`, `progress`, `last_error` | | |
| `created_at` | timestamp | |

### `assistants`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `name`, `prompt`, `first_message` | text | |
| `persona_id` | int? FK → `persona_entities.id` (`ON DELETE SET NULL`) | nulls when persona deleted |
| `avatar_id` | int? FK → `persona_avatars.id` (`ON DELETE SET NULL`) | nulls when avatar deleted |
| `face_id`, `simli_agent_id` | string | (`face_id` cleared when avatar deleted) |
| `voice_provider`, `voice_id`, `voice_model`, `language` | string | |
| `llm_provider`, `llm_model` | string | |
| `status`, `stage`, `progress`, `last_error` | | |
| `created_at` | timestamp | |

### `voices`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | int PK | |
| `name`, `description`, `source` | string | `source` ∈ `designed` / `cloned` / `from-library` |
| `provider`, `voice_id` | string | upstream voice id (e.g. ElevenLabs id) |
| `sample_url`, `preview_url` | text | blob URLs |
| `persona_id` | int? | optional link back to the persona this voice was designed from |
| `gender` | string | |
| `status`, `last_error` | | |
| `created_at` | timestamp | |

### Delete semantics

| Delete | DB row | Linked blob | External resource | Effect on children |
| --- | --- | --- | --- | --- |
| Persona | yes | source image + any owned voice sample/preview | owned ElevenLabs voice | avatars + assistants survive; their `persona_id` → `NULL` |
| Avatar | yes | preview image | Simli face | assistants survive; their `avatar_id` → `NULL`, `face_id` → `""` |
| Voice (`voices` row) | yes | sample + preview | ElevenLabs voice | any persona pointing at it has its voice fields cleared |
| Persona voice (`DELETE /personas/{id}/voice`) | n/a | sample + preview on the persona | ElevenLabs voice | n/a — only the persona's voice columns reset |
| Assistant | yes | — | — | — |

---

## API reference

**Contract**: `tenant_id` is **always in the request body** (JSON body field
on JSON routes, form field on multipart routes) — **never in the URL**.
Filters, pagination (`limit`, `offset`), and resource IDs (`{persona_id}`,
`{avatar_id}`, …) are URL parameters; `tenant_id` is not.

All routes return JSON unless noted. List endpoints accept `limit` (1–500,
default 100) and `offset` (≥0, default 0) as query parameters.

### Meta

#### `GET /health`
- **Body**: —
- **Resp**: `{ status: "ok" }`
- **UI use**: liveness probe.

#### `GET /config`
- **Body**: —
- **Resp**: `{ livekit_url, has_livekit_creds, has_simli_api_key, has_keyvault_creds: bool }`
- **UI use**: sanity check that the API has its dependencies wired before showing the "start call" button.

#### `GET /presets`
- **Body**: —
- **Resp**: `{ avatar_presets: ThemePreset[], voice_presets: ThemePreset[] }`
- **UI use**: populates the style/preset chip rows for avatar and voice design.

### Personas

#### `POST /personas`
- **Body** (multipart): `persona_image` (file), `name` (string), `tenant_id` (string)
- **Resp**: `PersonaEntity` with `status: "processing"`, `stage: "detecting_gender"`
- **UI use**: create-persona form. After save, poll `POST /personas/{id}/status` until `status ∈ {ready, failed}`.

#### `POST /personas/list`
- **Body**: `{ tenant_id }`
- **Query**: `gender`, `status`, `voice_status`, `voice_provider` (all optional); `limit`, `offset`
- **Resp**: `PersonaEntity[]` (each row includes its `avatars`)
- **UI use**: left-pane "Personas" panel.

#### `POST /personas/{id}/get`
- **Body**: `{ tenant_id }`
- **Resp**: `PersonaEntity`
- **UI use**: refresh a single persona card without a full list refetch.

#### `POST /personas/{id}/status`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status, stage, progress, last_error }`
- **UI use**: poll while persona creation is in flight (gender detection + voice description generation).

#### `POST /personas/{id}/cascade-count`
- **Body**: `{ tenant_id }`
- **Resp**: `{ avatars: int, assistants: int }`
- **UI use**: delete-confirmation modal — "Will leave X avatars and Y assistants orphaned."

#### `POST /personas/{id}/cancel`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status: "cancelled" }`
- **UI use**: "Cancel" button on a still-processing persona.

#### `PATCH /personas/{id}`
- **Body**: `{ tenant_id, name?, voice_ref_id? | null, ... }` — any column subset
- **Resp**: `PersonaEntity`
- **UI use**: rename a persona; attach/detach a standalone voice via `voice_ref_id`.

#### `DELETE /personas/{id}`
- **Body**: `{ tenant_id }`
- **Resp**: `{ deleted: int }`
- **UI use**: persona delete confirmation. See [Delete semantics](#delete-semantics).

#### `POST /personas/{id}/voice/design`
- **Body**: `{ tenant_id, user_prompt?, name?, gender? }` — `gender` ∈ `male` / `female` / `null`; defaults to persona's detected gender
- **Resp**: `PersonaEntity` with `voice_status: "processing"`
- **UI use**: "Design a voice from this persona" — kicks off the ElevenLabs Voice Design background task.

#### `POST /personas/{id}/voice/clone`
- **Body** (multipart): `voice_sample` (audio file), `name?`, `tenant_id`
- **Resp**: `PersonaEntity` with `voice_status: "processing"`
- **UI use**: "Clone this user's voice" — uploads a 30-90s sample, ElevenLabs returns a voice_id; Gemini analyses the audio for gender.

#### `DELETE /personas/{id}/voice`
- **Body**: `{ tenant_id }`
- **Resp**: `PersonaEntity` (voice fields cleared)
- **UI use**: "Detach voice" button on a persona. Drops the ElevenLabs voice + blob audio + clears the columns.

### Avatars

#### `POST /avatars/preview`
- **Body** (multipart): `persona_id`, `name`, `theme_prompt?`, `preset_ids?` (JSON array), `draft_avatar_id?`, `tenant_id`
- **Resp**: `{ avatar_id: int }` — preview is generated synchronously
- **UI use**: "Generate preview" in the avatar wizard. Returned `avatar_id` is a draft; subsequent calls reuse it.

#### `POST /avatars`
- **Body** (multipart): `persona_id`, `draft_avatar_id`, `name`, `theme_prompt?`, `preset_ids?`, `tenant_id`
- **Resp**: `PersonaAvatar` with `status: "processing"`, `stage: "uploading"`
- **UI use**: "Save avatar" — locks the preview into a persistent avatar and starts the Simli face-upload background task.

#### `POST /avatars/list`
- **Body**: `{ tenant_id }`
- **Query**: `persona_id`, `gender`, `voice_id`, `status` (optional); `limit`, `offset`
- **Resp**: `PersonaAvatar[]`
- **UI use**: avatars panel on a persona card.

#### `POST /avatars/{id}/get`
- **Body**: `{ tenant_id }`
- **Resp**: `PersonaAvatar`
- **UI use**: single-card refresh.

#### `POST /avatars/{id}/status`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status, stage, progress, last_error }`
- **UI use**: poll the Simli upload pipeline (uploading → polling_simli → ready).

#### `POST /avatars/{id}/cascade-count`
- **Body**: `{ tenant_id }`
- **Resp**: `{ assistants: int }`
- **UI use**: delete-confirmation modal — "Will leave X assistants orphaned."

#### `POST /avatars/{id}/cancel`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status: "cancelled" }`
- **UI use**: cancel an in-flight Simli upload.

#### `POST /avatars/{id}/retry`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status: "retrying" }`
- **UI use**: retry a failed avatar build (regenerates image if needed, re-uploads to Simli).

#### `DELETE /avatars/{id}`
- **Body**: `{ tenant_id }`
- **Resp**: `{ deleted: int }`
- **UI use**: avatar delete. See [Delete semantics](#delete-semantics).

### Voices

#### `POST /voices/design`
- **Body** (multipart): `name`, `description?`, `voice_preset_id?`, `persona_id?`, `include_persona_traits?`, `gender?`, `tenant_id`
- **Resp**: `VoiceEntity` with `status: "processing"`
- **UI use**: "Design from prompt" tab — kicks off ElevenLabs Voice Design. Final gender = explicit input → linked persona's gender → `unknown`.

#### `POST /voices/clone`
- **Body** (multipart): `voice_sample` (audio), `name`, `description?`, `persona_id?`, `tenant_id`
- **Resp**: `VoiceEntity` with `status: "processing"`
- **UI use**: "Clone from upload" tab. Gemini detects gender; ElevenLabs receives the gender label too.

#### `POST /voices/list`
- **Body**: `{ tenant_id }`
- **Query**: `gender`, `source`, `provider`, `status`, `persona_id` (optional); `limit`, `offset`
- **Resp**: `VoiceEntity[]`
- **UI use**: "Voices" panel in the sidebar.

#### `POST /voices/{id}/get`
- **Body**: `{ tenant_id }`
- **Resp**: `VoiceEntity`
- **UI use**: single-row refresh.

#### `POST /voices/{id}/status`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status, stage, progress, last_error }` (stage/progress null for voices)
- **UI use**: poll while design/clone is in flight.

#### `POST /voices/library`
- **Body**: `{ tenant_id }`
- **Resp**: `LibraryVoice[]` — `{ voice_id, name, preview_url, category, labels }`
- **UI use**: "Pick from library" tab — ElevenLabs's premade catalogue (cached 5 min server-side).

#### `POST /voices/from-library`
- **Body**: `{ tenant_id, voice_id, name, preview_url }`
- **Resp**: `VoiceEntity` with `status: "ready"`
- **UI use**: clicking a library entry imports it as a standalone voice row.

#### `POST /voices/preview-default`
- **Body**: `{ tenant_id }`
- **Resp**: MP3 bytes (`Content-Type: audio/mpeg`)
- **UI use**: play the env-configured default voice. Fetch as a blob → `URL.createObjectURL` → set as `<audio src>`.

#### `POST /voices/suggest-description`
- **Body**: `{ tenant_id, user_prompt? }`
- **Resp**: `{ description: string }`
- **UI use**: "Suggest" button on the design form — Gemini produces a voice-design brief from an optional user hint.

#### `POST /voices/{id}/retry`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status: "retrying" }`
- **UI use**: retry a failed design or clone job.

#### `DELETE /voices/{id}`
- **Body**: `{ tenant_id }`
- **Resp**: `{ deleted: int }`
- **UI use**: voice delete. See [Delete semantics](#delete-semantics).

### Assistants

#### `POST /assistants`
- **Body**: `{ tenant_id, name, prompt, first_message, persona_id, avatar_id, llm_provider?, llm_model? }`
- **Resp**: `Assistant` with `status: "processing"`
- **UI use**: "Create assistant" wizard — combines a persona, an avatar, and a system prompt.

#### `POST /assistants/list`
- **Body**: `{ tenant_id }`
- **Query**: `persona_id`, `avatar_id`, `voice_id`, `llm_provider`, `status` (optional); `limit`, `offset`
- **Resp**: `Assistant[]`
- **UI use**: assistants panel.

#### `POST /assistants/{id}/get`
- **Body**: `{ tenant_id }`
- **Resp**: `Assistant`
- **UI use**: single-card refresh.

#### `POST /assistants/{id}/status`
- **Body**: `{ tenant_id }`
- **Resp**: `{ status, stage, progress, last_error }`
- **UI use**: poll while the LiveKit agent is being prepared.

#### `DELETE /assistants/{id}`
- **Body**: `{ tenant_id }`
- **Resp**: `{ deleted: int }`
- **UI use**: assistant delete. No external resources are owned directly — the row is dropped.

### Call

#### `POST /calls`
- **Body**: `{ tenant_id, assistant_id }`
- **Resp** (single plug-and-play payload):

  ```jsonc
  {
    "livekit":   { "url": "wss://…", "token": "eyJ…", "room": "…", "identity": "…" },
    "assistant": { "id": 42, "name": "Maya", "first_message": "Hi…" },
    "avatar":    { "id": 7,  "face_id": "simli_xxx", "image_url": "https://…" },
    "voice":     { "provider": "elevenlabs", "voice_id": "21m00…", "name": "Rachel", "preview_url": "https://…" }
  }
  ```

- **UI use**: pressing the "Call" button on an assistant. Use `livekit.url` + `livekit.token` to join the room; the agent worker on the backend connects in parallel and publishes the lip-synced avatar video + voice audio. The other fields are display metadata for the call shell.

### Local-blob (local fallback only)

#### `GET /local-blob/{tenant_id}/{key:path}`
- **Body**: —
- **Resp**: raw bytes of the file at `LOCAL_DATA_DIR/{tenant_id}/blob/{key}`
- **UI use**: served as the value of `image_url` / `sample_url` etc. when running against `LOCAL_TENANT_ID`. In production (Azure) the URLs point directly at the blob endpoint and don't need this route.

---

## Polling pattern

There is no WebSocket. Background-task progress is exposed via per-resource
`*/status` routes. Poll every ~3 s until `status ∈ {ready, failed, cancelled}`.

```js
async function pollUntilDone(resource, id, tenant_id) {
  for (;;) {
    const r = await fetch(`/${resource}/${id}/status`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ tenant_id }),
    });
    const s = await r.json();
    if (s.status !== "processing") return s;
    await new Promise(r => setTimeout(r, 3000));
  }
}
```

---

## Calling from a browser

Minimal vanilla HTML that joins an assistant call using `livekit-client`:

```html
<!doctype html>
<video id="v" autoplay playsinline></video>
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"></script>
<script>
async function startCall(assistantId) {
  const r = await fetch("/calls", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ tenant_id: "local_tenant", assistant_id: assistantId }),
  });
  const data = await r.json();

  const { Room, RoomEvent } = window.LivekitClient;
  const room = new Room({ adaptiveStream: true });
  room.on(RoomEvent.TrackSubscribed, (track, pub) => {
    if (track.kind === "video") track.attach(document.getElementById("v"));
    else if (track.kind === "audio") track.attach();
  });
  await room.connect(data.livekit.url, data.livekit.token);
  await room.localParticipant.setMicrophoneEnabled(true);
}
</script>
```

A complete reference implementation (persona create flow, avatar wizard,
voice design/clone, assistant builder, call shell) is mounted at `/demo` —
all of it built on the routes above with `tenant_id` hardcoded to
`local_tenant`. Source under [app/demo_static/](app/demo_static/).
