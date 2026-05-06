# Avatar Agent Flow Test (FastAPI + LiveKit + Simli)

This app gives you a feature-flow test for:
- persona creation
- Simli agent creation
- avatar video call launch via Simli widget
- LiveKit room token generation

## 1) Install

```bash
uv sync
```

## 2) Configure

`.env` is already created with your provided values. Add optional values as needed:
- `DEFAULT_SIMLI_FACE_ID` for faster persona creation
- `OPENAI_API_KEY` only if you run the LiveKit worker

## 3) Run FastAPI app

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Open: http://localhost:8000

## Docker (local)

```bash
docker compose up --build -d
```

Then open: http://localhost:8000

Stop:

```bash
docker compose down
```

## 4) Test Flow

1. Create persona (name, prompt, face_id, etc.)
2. Click **Start Video Call** for a persona
3. Click **Open Current Call** to open Simli overlay call
4. Optionally generate LiveKit token from the test form

## 5) Optional: Run LiveKit Agent Worker

This is a separate worker path using `livekit-agents` + `simli` plugin:

```bash
uv run python livekit_agent/worker.py dev
```

Required env vars for worker:
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `SIMLI_API_KEY`
- `DEFAULT_SIMLI_FACE_ID`
- `OPENAI_API_KEY`

## Notes

- The Simli widget path is used for the fastest end-to-end feature flow test.
- For production, do not expose long-lived tokens in browser code.
- Rotate credentials after sharing them in plain text.
