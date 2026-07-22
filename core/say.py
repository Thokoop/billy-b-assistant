from typing import Literal, Optional


def _strip_outer_quotes(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        inner = s[1:-1].strip()
        if inner:
            return inner
    return s


def _classify_kind(text: str) -> tuple[Literal["prompt", "literal", "raw"], str]:
    s = text.strip()
    if s.startswith("{{") and s.endswith("}}"):
        return "prompt", s[2:-2].strip()
    return "literal", _strip_outer_quotes(s)


async def say(text: str, *, interactive: Optional[bool] = None):
    # 🔁 Lazy import to avoid: session -> mqtt -> say -> session
    from . import audio
    from .config import CHUNK_MS, REALTIME_AI_PROVIDER, TEXT_ONLY_MODE
    from .persona_manager import persona_manager
    from .realtime_ai_provider import voice_provider_registry
    from .session_manager import BillySession

    kind, cleaned = _classify_kind(text)

    # Literal MQTT "say" should default to one-shot announcement unless
    # explicitly forced interactive.
    if interactive is None and kind == "literal":
        interactive = False
    if kind == "literal" and interactive is False:
        from .logger import logger

        logger.info("SAY mode: direct literal TTS (no conversational session).", "🗣️")
    else:
        from .logger import logger

        logger.info(
            f"SAY mode: conversational session (kind={kind}, interactive={interactive}).",
            "🗣️",
        )

    # MQTT "say" sessions don't play the wake-up clip, so ensure the
    # playback gate is open; otherwise run_stream can block indefinitely.
    if not TEXT_ONLY_MODE:
        audio.ensure_playback_worker_started(CHUNK_MS)
        audio.playback_done_event.set()

    # Fast path: strict literal announce without opening conversational session.
    if kind == "literal" and interactive is False:
        provider = voice_provider_registry.get_provider(REALTIME_AI_PROVIDER)
        voice = persona_manager.get_current_persona_voice()
        clip = await provider.generate_audio_clip(
            prompt=cleaned,
            voice=voice,
            instructions=(
                "Speak the provided text exactly as written, with no added words "
                "before or after."
            ),
            strict_literal=True,
        )
        audio.playback_queue.put(clip)
        return

    # Conversational SAY sessions use the mic; pause wake-word listener to avoid
    # ALSA capture contention (-9985) during follow-up mic reopen.
    wakeword_paused = False
    try:
        from . import button as button_mod

        pause_fn = getattr(button_mod, "_pause_wakeword_listener", None)
        if callable(pause_fn):
            pause_fn()
            wakeword_paused = True
    except Exception:
        wakeword_paused = False

    session = BillySession(
        kickoff_text=cleaned,
        kickoff_kind=kind,
        kickoff_to_interactive=(interactive is True),
        autofollowup=(
            "always"
            if interactive is True
            else "never"
            if interactive is False
            else "auto"
        ),
    )

    if interactive is False:
        session.run_mode = "dory"  # one-and-done

    try:
        await session.start()
    finally:
        if wakeword_paused:
            try:
                from . import button as button_mod

                resume_fn = getattr(button_mod, "_resume_wakeword_listener", None)
                if callable(resume_fn):
                    resume_fn()
            except Exception:
                pass
