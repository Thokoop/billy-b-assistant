"""Microphone management wrapper for Billy session."""

import asyncio
import time
from collections import deque

from .. import audio
from ..audio import calculate_input_rms, mic_input_samples_for_meter
from ..config import SILENCE_THRESHOLD, TEXT_ONLY_MODE
from ..logger import logger
from ..mic import MicManager


class MicManagerWrapper:
    """Manages microphone lifecycle and audio input."""

    def __init__(self, session):
        self.session = session
        self.mic = MicManager()
        self.mic_running = False
        self.mic_timeout_task = None
        self.last_rms = 0.0
        self._mic_guard_until = 0.0
        self._mic_data_started = False
        self._logged_waiting_for_wakeup = False
        self._timeout_countdown_active = False
        self._local_activity_until = 0.0
        self._last_local_activity_rms = 0.0
        self._last_timeout_progress_log = 0.0
        self._timeout_countdown_started_at = 0.0
        self._prebuffered_audio = deque()
        self._prebuffered_audio_samples = 0
        self._prebuffer_seconds = 3.0

    def start(self, *, retry=True):
        """Try to open the mic with optional retry on failure."""
        if self.mic_running or not self.session.session_active.is_set():
            return

        try:
            self._mic_data_started = False
            self._logged_waiting_for_wakeup = False
            self._clear_prebuffer()
            if self.mic is None:
                self.mic = MicManager()

            self.mic.start(self.callback)
            self.mic_running = True
            self._mic_guard_until = time.time() + 0.35
            self._timeout_countdown_active = False
            self._timeout_countdown_started_at = 0.0
            self._last_timeout_progress_log = 0.0
            logger.verbose("Mic started", "🎤")
            if not self.mic_timeout_task or self.mic_timeout_task.done():
                self.mic_timeout_task = asyncio.create_task(self.timeout_checker())
            self.session._set_listening_state()

        except Exception as e:
            self.mic_running = False
            logger.error(f"Mic start failed: {e}")
            if retry and self.session.session_active.is_set():
                asyncio.create_task(self._retry_loop())

    def stop(self):
        """Stop the microphone."""
        if self.mic_running:
            try:
                self.mic.stop()
            except Exception as e:
                logger.warning(f"Error stopping mic: {e}")
            self.mic_running = False
        self._timeout_countdown_active = False
        self._clear_prebuffer()

    def _clear_prebuffer(self):
        self._prebuffered_audio.clear()
        self._prebuffered_audio_samples = 0

    def _store_prebuffer(self, samples):
        if samples is None or len(samples) == 0:
            return
        chunk = samples.copy()
        self._prebuffered_audio.append(chunk)
        self._prebuffered_audio_samples += len(chunk)
        max_samples = int(
            (audio.MIC_RATE or audio.PROVIDER_MIC_RATE) * self._prebuffer_seconds
        )
        while self._prebuffered_audio_samples > max_samples and self._prebuffered_audio:
            dropped = self._prebuffered_audio.popleft()
            self._prebuffered_audio_samples -= len(dropped)

    def flush_prebuffer(self):
        """Send audio captured after wake-up playback but before the session was ready."""
        if not self._prebuffered_audio:
            return
        if self.session.ws is None or self.session.loop is None:
            return
        chunks = len(self._prebuffered_audio)
        samples = self._prebuffered_audio_samples
        while self._prebuffered_audio:
            audio.send_mic_audio(
                self.session.ws,
                self._prebuffered_audio.popleft(),
                self.session.loop,
            )
        self._prebuffered_audio_samples = 0
        logger.info(
            f"Flushed startup mic prebuffer ({chunks} chunks, {samples} samples).",
            "🎤",
        )

    async def start_after_playback(self, delay: float = 0.6, retries: int = 3) -> bool:
        """Open mic after playback with retry logic."""
        # Fast path: in normal interactive sessions the mic stream is already open
        # and only input gating was disabled while Billy spoke.
        if self.mic_running:
            # New listen window; reset timeout countdown state so "started" is
            # emitted consistently for each follow-up window.
            self._timeout_countdown_active = False
            self._timeout_countdown_started_at = 0.0
            self._last_timeout_progress_log = 0.0
            if not self.mic_timeout_task or self.mic_timeout_task.done():
                self.mic_timeout_task = asyncio.create_task(self.timeout_checker())
            self.session._set_listening_state()
            logger.info("Mic already open; continuing follow-up listen window.", "🎙️")
            return True

        for attempt in range(1, retries + 1):
            try:
                if attempt > 1:
                    wait_time = delay * (attempt - 1) + 0.5
                    logger.info(
                        f"Waiting {wait_time:.1f}s before mic retry {attempt}...", "⏳"
                    )
                    await asyncio.sleep(wait_time)

                if self.mic_running:
                    await asyncio.wait_for(
                        asyncio.to_thread(self.mic.stop), timeout=1.5
                    )
                    self.mic_running = False
                    await asyncio.sleep(0.2)

                if not self.mic_running:
                    self._mic_data_started = False
                    self._logged_waiting_for_wakeup = False
                    # Run stream open on a worker thread with timeout so audio-driver
                    # stalls cannot wedge the session loop.
                    await asyncio.wait_for(
                        asyncio.to_thread(self.mic.start, self.callback), timeout=2.5
                    )
                    self.mic_running = True
                    self._mic_guard_until = time.time() + 0.35
                    self._timeout_countdown_active = False
                    self._timeout_countdown_started_at = 0.0
                    self._last_timeout_progress_log = 0.0
                    if not self.mic_timeout_task or self.mic_timeout_task.done():
                        self.mic_timeout_task = asyncio.create_task(
                            self.timeout_checker()
                        )
                    self.session._set_listening_state()
                logger.info(f"Mic opened (attempt {attempt}).", "🎙️")
                return True
            except Exception as e:
                err = str(e)
                logger.warning(f"Mic open failed (attempt {attempt}/{retries}): {err}")
                self.mic_running = False
                # Ensure we get a fresh stream object on retry.
                self.mic = MicManager()
                if "Device unavailable" in err or "PaErrorCode -9985" in err:
                    logger.warning(
                        "Mic device unavailable (-9985) during follow-up reopen; retrying with backoff.",
                        "⚠️",
                    )
                    # Extra settle time for ALSA handoff races (e.g. wake-word stream).
                    await asyncio.sleep(0.6)

        logger.error("Mic failed to open after retries.")
        return False

    def callback(self, indata, *_):
        """Handle incoming audio data from microphone."""
        if not self.session.session_active.is_set():
            return

        if not self.session.state.allow_mic_input:
            return

        # Don't send audio while Billy is speaking (prevents echo)
        if self.session.state.assistant_speaking:
            return

        # Don't send audio while response is active (prevents echo from buffered audio)
        if self.session.state.response_active:
            return

        if not TEXT_ONLY_MODE and not audio.playback_done_event.is_set():
            if not self._logged_waiting_for_wakeup:
                logger.info("Mic waiting for wake-up sound to finish...", "⏳")
                self._logged_waiting_for_wakeup = True
            return

        if not self._mic_data_started and not TEXT_ONLY_MODE:
            logger.info("Mic data now being sent", "🎤")
            self._mic_data_started = True

        samples = mic_input_samples_for_meter(indata)
        rms = calculate_input_rms(indata)
        self.last_rms = rms
        self.session.state.observe_rms(rms)
        now = time.time()

        # Pause timeout countdown only when local mic activity reaches the
        # configured speech threshold.
        if rms >= SILENCE_THRESHOLD:
            self._local_activity_until = now + 0.8
            self._last_local_activity_rms = rms
            if self._timeout_countdown_active:
                logger.info(
                    (
                        "Mic timeout countdown paused by local mic activity "
                        f"(rms={rms:.1f}, threshold={SILENCE_THRESHOLD:.1f})."
                    ),
                    "🎤",
                )
                self._timeout_countdown_active = False
                self._timeout_countdown_started_at = 0.0

        if rms > SILENCE_THRESHOLD:
            if self._timeout_countdown_active:
                logger.info(
                    f"Mic timeout interrupted by speech above threshold (rms={rms:.1f} >= {SILENCE_THRESHOLD:.1f}).",
                    "🎤",
                )
                self._timeout_countdown_active = False
                self._timeout_countdown_started_at = 0.0
            self.session.state.update_activity()
            self.session.state.increment_loud_mic_chunks()

        self.session.state.increment_mic_chunks()
        if self.session.ws is None or not self.session.session_initialized:
            self._store_prebuffer(samples)
            return
        if self._prebuffered_audio:
            self.flush_prebuffer()
        audio.send_mic_audio(self.session.ws, samples, self.session.loop)

    async def timeout_checker(self):
        """Monitor mic activity and timeout if idle too long."""
        from ..config import MIC_TIMEOUT_SECONDS
        from ..movements import move_tail_async

        logger.info("Mic timeout checker active", "🛡️")
        last_tail_move = 0

        while self.session.session_active.is_set():
            now = time.time()
            if not self.mic_running:
                await asyncio.sleep(0.2)
                continue

            if not TEXT_ONLY_MODE and not audio.playback_done_event.is_set():
                await asyncio.sleep(0.2)
                continue

            # Don't timeout while assistant is actively processing/responding.
            if self.session.state.response_active:
                await asyncio.sleep(0.2)
                continue
            # Don't timeout while the server is actively detecting user speech.
            if self.session.state._server_input_speaking:
                if self._timeout_countdown_active:
                    logger.info(
                        "Mic timeout countdown paused while user speech is active.",
                        "🎤",
                    )
                    self._timeout_countdown_active = False
                    self._timeout_countdown_started_at = 0.0
                    self._last_timeout_progress_log = 0.0
                await asyncio.sleep(0.2)
                continue
            # Mini model does not always emit speech_started/speech_stopped
            # reliably. Use local mic activity as an additional pause signal.
            if now < self._local_activity_until:
                if self._timeout_countdown_active:
                    logger.info(
                        (
                            "Mic timeout countdown paused while local mic activity is ongoing "
                            f"(last_rms={self._last_local_activity_rms:.1f})."
                        ),
                        "🎤",
                    )
                    self._timeout_countdown_active = False
                    self._timeout_countdown_started_at = 0.0
                    self._last_timeout_progress_log = 0.0
                await asyncio.sleep(0.2)
                continue

            idle_seconds = now - max(
                self.session.last_activity[0], audio.last_played_time
            )

            if idle_seconds > 0.5:
                if not self._timeout_countdown_active:
                    self._timeout_countdown_active = True
                    self._timeout_countdown_started_at = now
                    logger.info(
                        f"Mic timeout countdown started ({MIC_TIMEOUT_SECONDS}s limit, threshold={SILENCE_THRESHOLD:.1f}).",
                        "⏳",
                    )
                    self._last_timeout_progress_log = 0.0

                elapsed = now - self._timeout_countdown_started_at
                progress = min(elapsed / MIC_TIMEOUT_SECONDS, 1.0)
                bar_len = 20
                filled = int(bar_len * progress)
                bar = "█" * filled + "-" * (bar_len - filled)
                print(
                    f"\r👂 {MIC_TIMEOUT_SECONDS}s timeout: [{bar}] {elapsed:.1f}s "
                    f"| Mic Volume:: {self.last_rms:.4f} / Threshold: {SILENCE_THRESHOLD:.4f}",
                    end="",
                    flush=True,
                )
                if (
                    self._last_timeout_progress_log == 0.0
                    or now - self._last_timeout_progress_log >= 1.0
                ):
                    remaining = max(0.0, MIC_TIMEOUT_SECONDS - elapsed)
                    logger.info(
                        (
                            f"Mic timeout countdown progress: elapsed={elapsed:.1f}s, "
                            f"remaining={remaining:.1f}s, rms={self.last_rms:.1f}"
                        ),
                        "⏳",
                    )
                    self._last_timeout_progress_log = now

                if now - last_tail_move > 1.0:
                    move_tail_async(duration=0.2)
                    last_tail_move = now

                if elapsed > MIC_TIMEOUT_SECONDS:
                    logger.info(
                        (
                            f"Mic timeout reached end ({MIC_TIMEOUT_SECONDS}s). "
                            f"Ending input... last_rms={self.last_rms:.1f}"
                        ),
                        "⏱️",
                    )
                    self._timeout_countdown_active = False
                    self._timeout_countdown_started_at = 0.0
                    self._last_timeout_progress_log = 0.0
                    await self.session.stop_session(reason="mic_timeout")
                    break
            elif self._timeout_countdown_active:
                logger.info(
                    "Mic timeout countdown cleared before expiry.",
                    "✅",
                )
                self._timeout_countdown_active = False
                self._timeout_countdown_started_at = 0.0
                self._last_timeout_progress_log = 0.0

            await asyncio.sleep(0.5)

    async def _retry_loop(self):
        """Retry opening mic once with backoff."""
        logger.verbose("Mic retry loop started", "🔁")

        if not self.session.session_active.is_set():
            return

        await asyncio.sleep(0.5)

        try:
            self.mic = MicManager()
        except Exception as e:
            logger.warning(f"MicManager recreate failed: {e}")

        try:
            self.mic.start(self.callback)
            self.mic_running = True
            self._mic_guard_until = time.time() + 0.35
            logger.info("Mic started after retry", "✅")
            if not self.mic_timeout_task or self.mic_timeout_task.done():
                self.mic_timeout_task = asyncio.create_task(self.timeout_checker())
            self.session._set_listening_state()
        except Exception as e:
            self.mic_running = False
            logger.warning(f"Mic retry failed: {e}")
            logger.info("Assuming no follow-up needed, ending session.", "🛑")
            await self.session.stop_session(reason="mic_retry_failed")

    async def _reset_audio_system(self):
        """Reset audio system for device unavailable errors."""
        logger.info("Attempting audio system reset...", "🔄")
        try:
            import subprocess

            import sounddevice as sd

            sd._terminate()
            await asyncio.sleep(0.5)
            sd._initialize()

            subprocess.run(
                ["sudo", "alsactl", "restore"], capture_output=True, timeout=5
            )
            subprocess.run(
                ["sudo", "fuser", "-k", "/dev/snd/*"], capture_output=True, timeout=3
            )

            await asyncio.sleep(2.0)
            logger.info("Audio system reset completed", "✅")
        except Exception as e:
            logger.warning(f"Audio reset failed: {e}")
