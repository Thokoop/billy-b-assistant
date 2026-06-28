"""Error handling for Billy session."""

import asyncio
import os

from .. import audio
from ..logger import logger
from ..movements import stop_all_motors
from ..status_led import set_status_led_state


class ErrorHandler:
    """Handles error sounds and error scenarios."""

    def __init__(self, session):
        self.session = session

    async def play_error_sound(self, code: str = "error", message: str | None = None):
        """Play an error sound based on code (error, nowifi, noapikey)."""
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

        await self.session.stop_session()
