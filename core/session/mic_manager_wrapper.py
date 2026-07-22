"""Microphone management wrapper for Billy session."""

import asyncio
import math
import time
from collections import deque

from .. import audio
from ..audio import calculate_input_rms, mic_input_samples_for_meter
from ..config import AEC_BARGE_IN_SNR_DB, CHUNK_MS, SILENCE_THRESHOLD, TEXT_ONLY_MODE
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
        self._logged_barge_in_active = False
        self._barge_in_started_at = 0.0
        self._barge_in_evidence = deque(
            maxlen=max(4, int(round(480 / max(1, CHUNK_MS))))
        )
        self._barge_in_interrupt_requested = False
        self._barge_in_prebuffer = deque()
        self._barge_in_prebuffer_samples = 0
        self._barge_in_raw_peak = 0.0
        self._barge_in_cleaned_peak = 0.0
        self._barge_in_last_level_log = 0.0
        self._barge_in_residual_window = deque(maxlen=max(20, int(2000 / CHUNK_MS)))
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

        aec_active = audio.echo_cancellation_active()
        samples = mic_input_samples_for_meter(indata)
        raw_rms = calculate_input_rms(samples)
        playback_active = not audio.playback_done_event.is_set()
        if aec_active and playback_active:
            # Process even during wake-up playback so the adaptive filter learns
            # Billy's real speaker/microphone path before the first response.
            # Once playback stops, bypass AEC immediately: without a live render
            # reference its stale tail can keep server VAD stuck in speech_started.
            samples = audio.process_mic_with_aec(samples)

        barge_in = aec_active and (
            self.session.state.assistant_speaking or self.session.state.response_active
        )

        # Transcript streaming normally closes this gate while Billy speaks.
        # A working AEC path must override it so user speech can interrupt him.
        if not self.session.state.allow_mic_input and not barge_in:
            return

        if barge_in and not self._logged_barge_in_active:
            logger.info(
                "AEC barge-in listening while Billy speaks.",
                "👂",
            )
            self._logged_barge_in_active = True
            self._barge_in_started_at = time.time()
            self._barge_in_evidence.clear()
            self._barge_in_interrupt_requested = False
            self._barge_in_raw_peak = 0.0
            self._barge_in_cleaned_peak = 0.0
            self._barge_in_last_level_log = time.time()
            self._barge_in_residual_window.clear()
            self._clear_barge_in_prebuffer()
        elif not barge_in:
            self._logged_barge_in_active = False
            self._barge_in_started_at = 0.0
            self._barge_in_evidence.clear()
            self._barge_in_interrupt_requested = False
            self._barge_in_residual_window.clear()
            self._clear_barge_in_prebuffer()

        # Without a working AEC engine, preserve the safe half-duplex behavior.
        if self.session.state.assistant_speaking and not barge_in:
            return

        # Don't send audio while response is active (prevents echo from buffered audio)
        if self.session.state.response_active and not barge_in:
            return

        if (
            not TEXT_ONLY_MODE
            and not audio.playback_done_event.is_set()
            and not barge_in
        ):
            if not self._logged_waiting_for_wakeup:
                logger.info("Mic waiting for wake-up sound to finish...", "⏳")
                self._logged_waiting_for_wakeup = True
            return

        rms = calculate_input_rms(samples)

        if barge_in:
            self._store_barge_in_prebuffer(samples)
            # response.created precedes actual speaker playback. Learn the
            # residual only once audio is physically queued for playback.
            if audio.playback_done_event.is_set():
                self._barge_in_started_at = 0.0
                self._barge_in_residual_window.clear()
                return
            if self._barge_in_started_at <= 0.0:
                self._barge_in_started_at = time.time()

            elapsed = time.time() - self._barge_in_started_at
            learning = elapsed < 0.6 or len(self._barge_in_residual_window) < 8
            if learning:
                self._barge_in_residual_window.append(rms)

            ordered_residuals = sorted(self._barge_in_residual_window)
            if ordered_residuals:
                residual_index = int(0.8 * (len(ordered_residuals) - 1))
                residual_floor = max(1.0, ordered_residuals[residual_index])
            else:
                residual_floor = 1.0
            ratio_required = 10 ** (AEC_BARGE_IN_SNR_DB / 20.0)
            threshold = max(20.0, residual_floor * ratio_required)
            snr_db = 20.0 * math.log10(max(1.0, rms) / residual_floor)
            evidence_snr_db = max(3.0, AEC_BARGE_IN_SNR_DB - 3.0)
            if learning:
                self._barge_in_evidence.clear()
            else:
                self._barge_in_evidence.append(snr_db)
            evidence_hits = sum(
                value >= evidence_snr_db for value in self._barge_in_evidence
            )
            evidence_peak = max(self._barge_in_evidence, default=snr_db)
            evidence_required = max(2, math.ceil(self._barge_in_evidence.maxlen * 0.6))
            self._barge_in_raw_peak = max(self._barge_in_raw_peak, raw_rms)
            self._barge_in_cleaned_peak = max(self._barge_in_cleaned_peak, rms)
            if time.time() - self._barge_in_last_level_log >= 1.0:
                logger.info(
                    (
                        "AEC barge-in levels "
                        f"raw_peak={self._barge_in_raw_peak:.1f}, "
                        f"cleaned_peak={self._barge_in_cleaned_peak:.1f}, "
                        f"residual_floor={residual_floor:.1f}, "
                        f"snr={snr_db:.1f}dB, required={AEC_BARGE_IN_SNR_DB:.1f}dB, "
                        f"evidence={evidence_hits}/{self._barge_in_evidence.maxlen}, "
                        f"evidence_peak={evidence_peak:.1f}dB."
                    ),
                    "🎚️",
                )
                self._barge_in_raw_peak = 0.0
                self._barge_in_cleaned_peak = 0.0
                self._barge_in_last_level_log = time.time()
            # Continue tracking normal residual echo, but never learn from a
            # possible near-end speaker or it would raise the bar mid-sentence.
            possible_speech = any(
                value >= evidence_snr_db for value in self._barge_in_evidence
            )
            if not learning and rms < threshold and not possible_speech:
                self._barge_in_residual_window.append(rms)

            if (
                len(self._barge_in_evidence) == self._barge_in_evidence.maxlen
                and evidence_hits >= evidence_required
                and evidence_peak >= AEC_BARGE_IN_SNR_DB
                and not self._barge_in_interrupt_requested
            ):
                self._barge_in_interrupt_requested = True
                logger.info(
                    (
                        "Confirmed local AEC barge-in "
                        f"(peak_snr={evidence_peak:.1f}dB, "
                        f"required={AEC_BARGE_IN_SNR_DB:.1f}dB, "
                        f"evidence={evidence_hits}/{self._barge_in_evidence.maxlen})."
                    ),
                    "🗣️",
                )
                self._flush_barge_in_prebuffer()
                if self.session.loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        self.session.interrupt_to_user_turn(), self.session.loop
                    )
            # Never expose unconfirmed residual echo to server-side VAD.
            return

        if not self._mic_data_started and not TEXT_ONLY_MODE:
            logger.info("Mic data now being sent", "🎤")
            self._mic_data_started = True

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

    def _clear_barge_in_prebuffer(self):
        self._barge_in_prebuffer.clear()
        self._barge_in_prebuffer_samples = 0

    def _store_barge_in_prebuffer(self, samples):
        chunk = samples.copy()
        self._barge_in_prebuffer.append(chunk)
        self._barge_in_prebuffer_samples += len(chunk)
        max_samples = int((audio.MIC_RATE or audio.PROVIDER_MIC_RATE) * 0.6)
        while self._barge_in_prebuffer_samples > max_samples:
            removed = self._barge_in_prebuffer.popleft()
            self._barge_in_prebuffer_samples -= len(removed)

    def _flush_barge_in_prebuffer(self):
        if self.session.ws is None or self.session.loop is None:
            return
        chunks = len(self._barge_in_prebuffer)
        while self._barge_in_prebuffer:
            audio.send_mic_audio(
                self.session.ws,
                self._barge_in_prebuffer.popleft(),
                self.session.loop,
            )
        self._barge_in_prebuffer_samples = 0
        logger.info(f"Forwarded {chunks} buffered interruption chunks.", "🎤")

    async def timeout_checker(self):
        """Monitor mic activity and timeout if idle too long."""
        from ..config import MIC_TIMEOUT_SECONDS
        from ..mood import mood_manager
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
                speech_started_at = self.session.state._server_input_speech_started_at
                speech_open_seconds = (
                    now - speech_started_at if speech_started_at > 0.0 else 0.0
                )
                if speech_open_seconds >= 8.0 and now >= self._local_activity_until:
                    logger.warning(
                        (
                            "Server VAD speech remained open for "
                            f"{speech_open_seconds:.1f}s after local activity ended; "
                            "releasing stale speech state."
                        ),
                        "⚠️",
                    )
                    self.session.state._server_input_speaking = False
                    self.session.state._server_input_speech_started_at = 0.0
                    self.session.last_activity[0] = now
                    continue
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
                    motion = mood_manager.get_motion_profile()
                    move_tail_async(duration=motion.get("tail_duration", 0.2))
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
