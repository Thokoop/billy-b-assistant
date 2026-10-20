"""Audio handling for Billy session."""

import asyncio
import base64
import time
from typing import Any

from .. import audio
from ..config import CHUNK_MS, TEXT_ONLY_MODE
from ..logger import logger


# 24 kHz mono 16-bit PCM from the provider.
_PROVIDER_OUTPUT_BYTES_PER_MS = audio.PROVIDER_OUTPUT_RATE * 2 / 1000


class AudioHandler:
    """Handles audio input/output for the session."""

    def __init__(self, session):
        self.session = session
        self.audio_buffer = bytearray()
        self._playback_generation = None
        self._playback_item_id = None
        self._playback_content_index = 0
        # One response can speak through several items (for example a short
        # "commentary" line and then the answer). Playback position is counted
        # across the whole response, so remember where each item starts to
        # report a position that belongs to the item being truncated.
        self._playback_items: list[dict[str, Any]] = []
        self._queued_output_bytes = 0

    def clear_buffer(self):
        """Clear the audio buffer."""
        self.audio_buffer.clear()
        self._playback_generation = None
        self._playback_item_id = None
        self._playback_content_index = 0
        self._playback_items = []
        self._queued_output_bytes = 0

    def interruption_point(self):
        """Return the assistant item and audio position heard by the user."""
        if not self._playback_item_id or self._playback_generation is None:
            return None
        heard_ms = int(audio.aec_heard_audio_ms(self._playback_generation) or 0)
        item_id = self._playback_item_id
        content_index = self._playback_content_index
        start_ms = 0
        end_ms = int(self._queued_output_bytes / _PROVIDER_OUTPUT_BYTES_PER_MS)
        for index, entry in enumerate(self._playback_items):
            if heard_ms < entry["start_ms"] and index > 0:
                break
            item_id = entry["item_id"]
            content_index = entry["content_index"]
            start_ms = entry["start_ms"]
            end_ms = (
                self._playback_items[index + 1]["start_ms"]
                if index + 1 < len(self._playback_items)
                else int(self._queued_output_bytes / _PROVIDER_OUTPUT_BYTES_PER_MS)
            )
        # Never report more audio than this item actually holds: the provider
        # rejects a truncation past the item's own length.
        position_ms = max(0, min(heard_ms, end_ms) - start_ms)
        return {
            "item_id": item_id,
            "content_index": content_index,
            "audio_end_ms": position_ms,
        }

    def on_audio_delta(self, data: dict[str, Any]):
        """Handle incoming audio delta from assistant."""
        if TEXT_ONLY_MODE:
            return

        self.session.state.assistant_speaking = True
        self.session.state._turn_had_speech = True

        audio_b64 = data.get("audio") or data.get("delta")
        if not audio_b64:
            return

        # First audio frame of this turn: force mic gating until playback is done.
        if not self.audio_buffer and audio.playback_done_event.is_set():
            audio.playback_done_event.clear()
        if self._playback_generation is None:
            self._playback_generation = audio.begin_aec_playback_generation()
        item_id = data.get("item_id")
        content_index = int(data.get("content_index") or 0)
        if item_id and (
            item_id != self._playback_item_id
            or content_index != self._playback_content_index
            or not self._playback_items
        ):
            self._playback_item_id = item_id
            self._playback_content_index = content_index
            self._playback_items.append({
                "item_id": item_id,
                "content_index": content_index,
                "start_ms": int(
                    self._queued_output_bytes / _PROVIDER_OUTPUT_BYTES_PER_MS
                ),
            })

        audio_chunk = base64.b64decode(audio_b64)
        self._queued_output_bytes += len(audio_chunk)
        self.audio_buffer.extend(audio_chunk)
        self.session.last_activity[0] = time.time()
        audio.playback_queue.put(("tts", audio_chunk, self._playback_generation))

        if self.session.interrupt_event.is_set():
            logger.warning(
                "Assistant turn interrupted. Stopping response playback.", "⛔"
            )
            audio.stop_playback()
            self.session.session_active.clear()
            self.session.interrupt_event.clear()

    async def wait_for_playback_complete(self):
        """Wait for audio playback to complete."""
        if not TEXT_ONLY_MODE:
            await asyncio.to_thread(audio.playback_queue.join)
            await asyncio.sleep(0.3)

    def save_response_audio(self):
        """Save the response audio buffer to disk."""
        if len(self.audio_buffer) > 0:
            logger.verbose(
                f"Saving audio buffer ({len(self.audio_buffer)} bytes)", "💾"
            )
            audio.rotate_and_save_response_audio(self.audio_buffer)
        else:
            logger.warning("Audio buffer was empty, skipping save.")

    def signal_playback_done(self):
        """Signal that playback is complete."""
        audio.playback_done_event.set()

    @staticmethod
    def ensure_playback_worker():
        """Ensure the playback worker is started."""
        if not TEXT_ONLY_MODE:
            audio.ensure_playback_worker_started(CHUNK_MS)

    @staticmethod
    def stop_playback():
        """Stop audio playback immediately."""
        audio.stop_playback()

    @staticmethod
    async def enqueue_wav(path: str):
        """Enqueue a WAV file for playback."""
        await asyncio.to_thread(audio.enqueue_wav_to_playback, path)

    @staticmethod
    async def wait_for_playback_queue():
        """Wait for the playback queue to empty."""
        await asyncio.to_thread(audio.playback_queue.join)
