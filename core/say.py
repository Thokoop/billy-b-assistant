from typing import Literal


def _classify_kind(text: str) -> tuple[Literal["prompt", "literal", "raw"], str]:
    s = text.strip()
    if s.startswith("{{") and s.endswith("}}"):
        return "prompt", s[2:-2].strip()
    return "literal", s


async def say(text: str, *, interactive: bool | None = None):
    # 🔁 Lazy import to avoid: session -> mqtt -> say -> session
    from . import audio
    from .config import CHUNK_MS, TEXT_ONLY_MODE
    from .session_manager import BillySession

    kind, cleaned = _classify_kind(text)

    # MQTT "say" sessions don't play the wake-up clip, so ensure the
    # playback gate is open; otherwise run_stream can block indefinitely.
    if not TEXT_ONLY_MODE:
        audio.ensure_playback_worker_started(CHUNK_MS)
        audio.playback_done_event.set()

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

    await session.start()
