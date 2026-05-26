from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli

# Make the FastAPI app package importable when this file is run directly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from app import repositories as repo  # noqa: E402
from app.config import settings  # noqa: E402
from app.tenancy import open_background_context  # noqa: E402

AGENT_NAME = "lili-avatar-agent"

logger = logging.getLogger("lili-avatar-agent")
logging.basicConfig(level=logging.INFO)

IDLE_WARN_SECONDS = 25   # silence after agent finishes speaking before "Are you still there?"
IDLE_BYE_SECONDS  = 15   # extra silence before goodbye + disconnect

# Appended to every agent instruction set to enforce dynamic language following.
_LANGUAGE_LOCK = (
    "\n\nLANGUAGE RULE — this overrides everything else: "
    "Always detect the language of the user's most recent message and respond in that exact language. "
    "If the user switches language mid-conversation, you switch immediately and fully — no lag, no mixing. "
    "Every single word of your response must be in the user's current language. "
    "Never insert English words or phrases when the user is speaking a non-English language. "
    "If the user speaks Hindi → respond in Hindi only. "
    "If Tamil → Tamil only. If Marathi → Marathi only. If Bengali → Bengali only. "
    "If the user mixes Hindi and English (Hinglish), mirror that exact mix — do not drift to pure English or pure Hindi. "
    "Follow the user's language naturally like a fluent bilingual speaker would."
)


async def _idle_monitor(session: AgentSession, ctx: JobContext) -> None:
    last_activity = asyncio.get_event_loop().time()
    warned = False
    warn_time = 0.0

    def _on_user_input(ev: object) -> None:
        nonlocal last_activity, warned, warn_time
        if getattr(ev, "is_final", True):
            last_activity = asyncio.get_event_loop().time()
            warned = False
            warn_time = 0.0

    def _on_agent_state(ev: object) -> None:
        # Reset the idle clock whenever the agent transitions back to listening
        # (i.e. it just finished speaking). This prevents the timer from firing
        # while the agent itself is still talking or during startup.
        nonlocal last_activity
        if getattr(ev, "new_state", None) == "listening":
            last_activity = asyncio.get_event_loop().time()

    session.on("user_input_transcribed", _on_user_input)
    session.on("agent_state_changed", _on_agent_state)
    try:
        while True:
            await asyncio.sleep(1)
            now = asyncio.get_event_loop().time()
            if not warned and (now - last_activity) >= IDLE_WARN_SECONDS:
                warned = True
                warn_time = now
                await session.generate_reply(
                    instructions=(
                        "The user has been silent for a while. "
                        "Briefly ask if they are still there in one short sentence. "
                        "Use the same language the user was speaking most recently — "
                        "if they were speaking Hindi, ask in Hindi only; if English, ask in English. "
                        "Do not mix languages."
                    )
                )
            elif warned and (now - warn_time) >= IDLE_BYE_SECONDS:
                await session.generate_reply(
                    instructions=(
                        "The user has not responded. "
                        "Say a warm, brief goodbye and let them know they can start a new call anytime. "
                        "One or two sentences only. "
                        "Use the same language the user was speaking most recently — "
                        "if they were speaking Hindi, say goodbye in Hindi only; if English, in English. "
                        "Do not mix languages."
                    )
                )
                await asyncio.sleep(3)
                await ctx.room.disconnect()
                return
    except asyncio.CancelledError:
        pass


def _build_stt():
    provider = settings.stt_provider
    if provider == "deepgram":
        from livekit.plugins import deepgram

        return deepgram.STT(
            model=settings.stt_model,
            api_key=settings.deepgram_api_key or None,
            language="multi",  # multilingual streaming — Hindi, English, etc.
        )
    if provider == "openai":
        from livekit.plugins import openai

        # whisper-1 is multilingual by default — no language param needed
        return openai.STT(model=settings.stt_model, api_key=settings.openai_api_key or None)
    raise RuntimeError(f"Unsupported STT_PROVIDER: {provider}")


def _default_call_llm_model(provider: str) -> str:
    """Pick the env-configured default model for the given LLM provider."""
    if provider == "gemini":
        return settings.call_llm_model_gemini
    if provider == "azure_openai":
        return settings.call_llm_model_azure_openai
    return settings.call_llm_model_openai


def _build_llm(provider: str, model: str):
    if provider == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=model, api_key=settings.openai_api_key or None)
    if provider == "gemini":
        from livekit.plugins import google

        return google.LLM(model=model, api_key=settings.gemini_api_key or None)
    if provider == "azure_openai":
        from livekit.plugins import openai

        # Uses the gpt-4o deployment configured via AZURE_OPENAI_*.
        # ``model`` here is the deployment name (Azure routes by
        # deployment, not by model identifier).
        return openai.LLM.with_azure(
            azure_deployment=model or settings.azure_openai_deployment,
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key or None,
            api_version=settings.azure_openai_api_version,
        )
    raise RuntimeError(f"Unsupported LLM provider: {provider}")


_SUPPORTED_TTS = {"elevenlabs", "openai", "google"}


def _build_tts(voice_id_override: str = "", provider_override: str = ""):
    provider = (provider_override or settings.tts_provider).lower()
    if provider not in _SUPPORTED_TTS:
        logger.warning("Stored TTS provider %r is not supported — falling back to %s", provider, settings.tts_provider)
        provider = settings.tts_provider.lower()
        voice_id_override = ""  # stored voice_id is provider-specific; don't reuse
    voice_id = voice_id_override or settings.tts_voice_id
    return _make_tts(provider, settings.tts_model, voice_id)


FALLBACK_ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah — present in every account


def _make_tts(provider: str, model: str, voice_id: str):
    if provider == "elevenlabs":
        from livekit.plugins import elevenlabs

        # Never rely on the plugin's hardcoded DEFAULT_VOICE_ID — it changes between
        # plugin versions and the current default ("l7kNoIfnJKPg7779LI2t") is not
        # available in standard accounts, which causes voice_id_does_not_exist errors.
        effective_voice_id = voice_id or FALLBACK_ELEVENLABS_VOICE_ID
        kwargs: dict = {
            "model": model,
            "api_key": settings.elevenlabs_api_key or None,
            "voice_id": effective_voice_id,
            # PCM at 16 kHz matches Simli's expected sample rate exactly —
            # no MP3 decode or resample step, which is the primary cause of lip-sync drift.
            "encoding": "pcm_16000",
            # Deliver audio in smaller chunks so the first audio bytes reach
            # Simli faster, reducing the gap between speech start and lip movement.
            "chunk_length_schedule": [50, 100, 150, 200],
        }
        return elevenlabs.TTS(**kwargs)
    if provider == "openai":
        from livekit.plugins import openai

        return openai.TTS(model=model, voice=voice_id or "alloy", api_key=settings.openai_api_key or None)
    if provider == "google":
        from livekit.plugins import google

        return google.TTS(api_key=settings.gemini_api_key or None)
    raise RuntimeError(f"Unsupported TTS provider: {provider}")


async def _load_assistant_from_job(
    ctx: JobContext,
) -> tuple[str, dict | None, dict | None]:
    """Return ``(tenant_id, assistant_dict, persona_dict)`` from the dispatch metadata.

    Tenant ID is required so the worker can hit the correct tenant DB. If the
    metadata is missing or malformed we return empty strings/None and the
    caller falls back to environment defaults.
    """
    meta_str = getattr(getattr(ctx, "job", None), "metadata", "") or ""
    if not meta_str:
        return "", None, None
    try:
        meta = json.loads(meta_str)
    except Exception:
        return "", None, None
    tenant_id = str(meta.get("tenant_id") or "")
    aid_raw = meta.get("assistant_id")
    try:
        aid = int(aid_raw)
    except (TypeError, ValueError):
        return tenant_id, None, None
    if not tenant_id:
        return "", None, None
    try:
        async with open_background_context(tenant_id) as tctx:
            assistant = await repo.get_assistant(tctx.session, aid)
            persona = None
            if assistant is not None and assistant.persona_id:
                persona = await repo.get_persona_entity(
                    tctx.session, assistant.persona_id
                )
            asst_dict = (
                {
                    "id": assistant.id,
                    "name": assistant.name,
                    "prompt": assistant.prompt,
                    "first_message": assistant.first_message,
                    "persona_id": assistant.persona_id,
                    "face_id": assistant.face_id,
                    "voice_provider": assistant.voice_provider,
                    "voice_id": assistant.voice_id,
                    "llm_provider": assistant.llm_provider,
                    "llm_model": assistant.llm_model,
                }
                if assistant is not None
                else None
            )
            persona_dict = (
                {
                    "voice_provider": persona.voice_provider,
                    "voice_id": persona.voice_id,
                }
                if persona is not None
                else None
            )
            return tenant_id, asst_dict, persona_dict
    except Exception:
        logger.exception("Failed to load assistant %s for tenant %s", aid, tenant_id)
        return tenant_id, None, None


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    _tenant_id, assistant, persona_row = await _load_assistant_from_job(ctx)
    if assistant:
        face_id = str(assistant.get("face_id") or settings.default_simli_face_id or "")
        instructions = str(assistant.get("prompt") or (
            "You are a friendly, conversational AI avatar assistant in a live voice session. "
            "Keep responses concise and natural — this is real-time spoken conversation, not a text chat. "
            "Respond in 1–3 sentences unless a detailed answer is genuinely needed. "
            "Be warm, clear, and direct. "
            "Do not use bullet points, numbered lists, or markdown — speak in plain natural sentences."
        )) + _LANGUAGE_LOCK
        first_message = str(assistant.get("first_message") or "")
        llm_provider = (str(assistant.get("llm_provider") or settings.call_llm_provider)).lower()
        if llm_provider not in {"openai", "gemini", "azure_openai"}:
            llm_provider = settings.call_llm_provider
        llm_model = str(assistant.get("llm_model") or "") or _default_call_llm_model(llm_provider)
        # The persona is the authoritative source for voice: users may change
        # the persona's voice after the assistant was created. Fall back to the
        # assistant's snapshot only if the persona has nothing set.
        if persona_row and persona_row.get("voice_id"):
            voice_provider_override = (str(persona_row.get("voice_provider") or "")).lower()
            voice_id_override = str(persona_row.get("voice_id") or "")
        else:
            voice_provider_override = (str(assistant.get("voice_provider") or "")).lower()
            voice_id_override = str(assistant.get("voice_id") or "")
    else:
        face_id = settings.default_simli_face_id
        instructions = os.getenv(
            "AGENT_PERSONA_PROMPT",
            "You are a friendly, conversational AI avatar assistant in a live voice session. "
            "Keep responses concise and natural — this is real-time spoken conversation, not a text chat. "
            "Respond in 1–3 sentences unless a detailed answer is genuinely needed. "
            "Be warm, clear, and direct. "
            "Do not use bullet points, numbered lists, or markdown — speak in plain natural sentences.",
        ) + _LANGUAGE_LOCK
        first_message = ""
        llm_provider = settings.call_llm_provider
        llm_model = _default_call_llm_model(llm_provider)
        voice_provider_override = ""
        voice_id_override = ""

    if not face_id:
        raise RuntimeError("No face_id available (assistant row missing face_id and DEFAULT_SIMLI_FACE_ID unset)")

    logger.info(
        "Starting agent | room=%s assistant=%s llm=%s:%s stt=%s tts=%s voice=%s face=%s",
        ctx.room.name,
        (assistant or {}).get("name"),
        llm_provider,
        llm_model,
        settings.stt_provider,
        (voice_provider_override or settings.tts_provider),
        voice_id_override or settings.tts_voice_id or "(default)",
        face_id,
    )

    session = AgentSession(
        stt=_build_stt(),
        llm=_build_llm(llm_provider, llm_model),
        tts=_build_tts(voice_id_override=voice_id_override, provider_override=voice_provider_override),
    )

    from livekit.plugins import simli

    # Monkey-patch SimliConfig to force High Quality stream and SyncAudio
    if not hasattr(simli.SimliConfig, "_patched_for_hq"):
        _original_create_json = simli.SimliConfig.create_json
        def _patched_create_json(self):
            res = _original_create_json(self)
            res["isHighQuality"] = True
            res["syncAudio"] = True
            return res
        simli.SimliConfig.create_json = _patched_create_json
        simli.SimliConfig._patched_for_hq = True

    _SIMLI_IDENTITY = "simli-avatar-agent"
    for attempt in range(1, 4):
        avatar = simli.AvatarSession(
            simli_config=simli.SimliConfig(
                api_key=settings.simli_api_key,
                face_id=face_id,
                # Must exceed IDLE_WARN_SECONDS + IDLE_BYE_SECONDS (25+15=40s) so
                # Simli's own idle timeout never fires before our graceful disconnect.
                max_idle_time=90,
            )
        )
        await avatar.start(session, ctx.room)
        await asyncio.sleep(3)
        if _SIMLI_IDENTITY in ctx.room.remote_participants:
            break
        if attempt < 3:
            logger.warning("Simli avatar not connected (attempt %d/3), retrying in 5s…", attempt)
            await asyncio.sleep(5)
        else:
            logger.error("Simli avatar failed to connect after 3 attempts, continuing voice-only")

    from livekit.agents.voice.room_io import RoomOptions
    await session.start(
        agent=Agent(instructions=instructions),
        room=ctx.room,
        room_options=RoomOptions(audio_output=False),
    )

    if first_message:
        await session.say(first_message)

    asyncio.create_task(_idle_monitor(session, ctx))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))
