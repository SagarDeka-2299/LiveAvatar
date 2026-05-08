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

from app.config import settings  # noqa: E402
from app.db import get_assistant, get_persona_entity  # noqa: E402

AGENT_NAME = "lili-avatar-agent"

logger = logging.getLogger("lili-avatar-agent")
logging.basicConfig(level=logging.INFO)

IDLE_WARN_SECONDS = 10   # silence before "Are you still there?"
IDLE_BYE_SECONDS  = 5    # extra silence before goodbye + disconnect


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

    session.on("user_input_transcribed", _on_user_input)
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
                        "Use the same language as the conversation so far."
                    )
                )
            elif warned and (now - warn_time) >= IDLE_BYE_SECONDS:
                await session.generate_reply(
                    instructions=(
                        "The user has not responded. "
                        "Say a warm, brief goodbye and let them know they can start a new call anytime. "
                        "Use the same language as the conversation so far. One or two sentences only."
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

        return deepgram.STT(model=settings.stt_model, api_key=settings.deepgram_api_key or None)
    if provider == "openai":
        from livekit.plugins import openai

        return openai.STT(model=settings.stt_model, api_key=settings.openai_api_key or None)
    raise RuntimeError(f"Unsupported STT_PROVIDER: {provider}")


def _build_llm(provider: str, model: str):
    if provider == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=model, api_key=settings.openai_api_key or None)
    if provider == "gemini":
        from livekit.plugins import google

        return google.LLM(model=model, api_key=settings.gemini_api_key or None)
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
        }
        return elevenlabs.TTS(**kwargs)
    if provider == "openai":
        from livekit.plugins import openai

        return openai.TTS(model=model, voice=voice_id or "alloy", api_key=settings.openai_api_key or None)
    if provider == "google":
        from livekit.plugins import google

        return google.TTS(api_key=settings.gemini_api_key or None)
    raise RuntimeError(f"Unsupported TTS provider: {provider}")


def _load_assistant_from_job(ctx: JobContext) -> dict | None:
    meta_str = getattr(getattr(ctx, "job", None), "metadata", "") or ""
    if not meta_str:
        return None
    try:
        meta = json.loads(meta_str)
    except Exception:
        return None
    aid = meta.get("assistant_id")
    if not isinstance(aid, int):
        try:
            aid = int(aid)
        except Exception:
            return None
    return get_assistant(aid)


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    assistant = _load_assistant_from_job(ctx)
    if assistant:
        face_id = str(assistant.get("face_id") or settings.default_simli_face_id or "")
        instructions = str(assistant.get("prompt") or (
            "You are a friendly, conversational AI avatar assistant in a live voice session. "
            "Keep responses concise and natural — this is real-time spoken conversation, not a text chat. "
            "Respond in 1–3 sentences unless a detailed answer is genuinely needed. "
            "Be warm, clear, and direct. "
            "Do not use bullet points, numbered lists, or markdown — speak in plain natural sentences."
        ))
        first_message = str(assistant.get("first_message") or "")
        llm_provider = (str(assistant.get("llm_provider") or settings.llm_provider)).lower()
        if llm_provider not in {"openai", "gemini"}:
            llm_provider = settings.llm_provider
        llm_model = str(assistant.get("llm_model") or "") or (
            settings.llm_model_gemini if llm_provider == "gemini" else settings.llm_model_openai
        )
        # Read voice from the persona as the authoritative source: assistants are
        # created with a snapshot of the persona's voice, but the user can change
        # the persona's voice afterwards. Reading from persona on each call
        # guarantees fresh voice data even if assistant rows weren't propagated.
        persona_row = None
        pid = assistant.get("persona_id")
        if pid:
            try:
                persona_row = get_persona_entity(int(pid))
            except Exception:
                persona_row = None
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
        )
        first_message = ""
        llm_provider = settings.llm_provider
        llm_model = settings.llm_model_gemini if llm_provider == "gemini" else settings.llm_model_openai
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

    _SIMLI_IDENTITY = "simli-avatar-agent"
    for attempt in range(1, 4):
        avatar = simli.AvatarSession(
            simli_config=simli.SimliConfig(
                api_key=settings.simli_api_key,
                face_id=face_id,
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

    await session.start(
        agent=Agent(instructions=instructions),
        room=ctx.room,
    )

    if first_message:
        await session.say(first_message)

    asyncio.create_task(_idle_monitor(session, ctx))


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME))
