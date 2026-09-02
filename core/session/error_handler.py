"""Error handling for Billy session."""

from __future__ import annotations

import asyncio
import os

from .. import audio
from ..logger import logger
from ..mood import mood_manager
from ..movements import stop_all_motors
from ..status_led import set_status_led_state


async def play_standalone_error_sound(code: str = "error", message: str | None = None):
    """Play an error sound with no active session (e.g. the session failed to start)."""
    mood_manager.apply_event("error")
    stop_all_motors()
    set_status_led_state("error")

    filename = f"{code}.wav"
    sound_path = os.path.join("sounds", filename)

    logger.error(f"Error ({code}): {message or 'No message'}")
    logger.info(f"Attempting to play {filename}...", "🔊")

    if os.path.exists(sound_path):
        await asyncio.to_thread(audio.enqueue_wav_to_playback, sound_path)
        await asyncio.to_thread(audio.playback_queue.join)
    else:
        logger.warning(f"{sound_path} not found, skipping audio playback.")


class ErrorHandler:
    """Handles error sounds and error scenarios."""

    def __init__(self, session):
        self.session = session

    async def play_error_sound(self, code: str = "error", message: str | None = None):
        """Play an error sound based on code (error, nowifi, noapikey)."""
        await play_standalone_error_sound(code, message)
        await self.session.stop_session()
