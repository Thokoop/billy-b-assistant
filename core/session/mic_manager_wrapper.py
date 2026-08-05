"""Microphone management wrapper for Billy session."""

import asyncio
import math
import time
from collections import deque

from .. import audio
from ..audio import calculate_input_rms, mic_input_samples_for_meter
from ..config import (
    CHUNK_MS,
    MIC_TIMEOUT_TAIL_FLAP,
    SILENCE_THRESHOLD,
    TEXT_ONLY_MODE,
)
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
        self._timeout_motor_guard_until = 0.0
        self._mic_data_started = False
        self._logged_waiting_for_wakeup = False
        self._timeout_countdown_active = False
        self._local_activity_until = 0.0
        self._last_local_activity_rms = 0.0
        self._last_timeout_progress_log = 0.0
        self._timeout_countdown_started_at = 0.0
        self._timeout_head_retracted = False
        self._logged_barge_in_active = False
        self._prebuffered_audio = deque()
        self._prebuffered_audio_samples = 0
        self._prebuffer_seconds = 3.0
        self._barge_in_rms_window = deque(maxlen=40)
        self._barge_in_voice_window = deque(maxlen=40)
        self._barge_in_similarity_window = deque(maxlen=40)
        self._barge_in_residual_samples = deque(maxlen=25)
        # Preserve a slowly adapting residual estimate across responses. AEC can
        # briefly produce near-zero cleaned samples after a response starts; if
        # those samples replace the baseline immediately, ordinary speaker
        # leakage looks like near-end speech.
        self._barge_in_residual_floor = max(100.0, SILENCE_THRESHOLD * 0.5)
        self._barge_in_candidate_floor = None

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
            self._timeout_head_retracted = False
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
        self._timeout_head_retracted = False
        self._clear_prebuffer()

    def _clear_prebuffer(self):
        self._prebuffered_audio.clear()
        self._prebuffered_audio_samples = 0

    def reset_barge_in_evidence(self):
        """Start a fresh response window while preserving the learned floor."""
        self._barge_in_rms_window.clear()
        self._barge_in_voice_window.clear()
        self._barge_in_similarity_window.clear()
        self._barge_in_residual_samples.clear()
        self._barge_in_candidate_floor = None
        # Follow a later reduction in speaker volume or microphone gain without
        # letting a single quiet response erase useful calibration.
        base_floor = max(100.0, SILENCE_THRESHOLD * 0.5)
        self._barge_in_residual_floor = max(
            base_floor,
            0.95 * self._barge_in_residual_floor + 0.05 * base_floor,
        )

    def observe_barge_in_residual(self, rms: float):
        """Learn AEC residue only while provider VAD reports no speech."""
        if self._barge_in_candidate_floor is not None:
            return
        self._barge_in_residual_samples.append(float(rms))
        if len(self._barge_in_residual_samples) < 5:
            return
        ordered = sorted(self._barge_in_residual_samples)
        # A median resists isolated mouth/motor and near-end transients while
        # still following a genuine speaker-volume change.
        observed_floor = ordered[len(ordered) // 2]
        alpha = 0.08 if observed_floor > self._barge_in_residual_floor else 0.03
        base_floor = max(100.0, SILENCE_THRESHOLD * 0.5)
        self._barge_in_residual_floor = max(
            base_floor,
            (1.0 - alpha) * self._barge_in_residual_floor + alpha * observed_floor,
        )

    def start_barge_in_candidate(self):
        """Scope local evidence to the provider's current speech event."""
        # OpenAI reports speech_started with 300 ms of prefix audio. Preserve
        # the matching local frames: clearing the whole window here discarded
        # the spoken onset which caused provider VAD to fire, so short barge-in
        # phrases could finish before local confirmation even began.
        prefix_chunks = max(1, math.ceil(300 / max(1, CHUNK_MS)))
        recent_rms = list(self._barge_in_rms_window)[-prefix_chunks:]
        recent_voice = list(self._barge_in_voice_window)[-prefix_chunks:]
        recent_similarity = list(self._barge_in_similarity_window)[-prefix_chunks:]
        self._barge_in_rms_window.clear()
        self._barge_in_rms_window.extend(recent_rms)
        self._barge_in_voice_window.clear()
        self._barge_in_voice_window.extend(recent_voice)
        self._barge_in_similarity_window.clear()
        self._barge_in_similarity_window.extend(recent_similarity)
        # Freeze calibration for this complete provider candidate. Candidate
        # samples include the user's voice and must never raise their own gate.
        self._barge_in_candidate_floor = max(
            SILENCE_THRESHOLD * 0.35,
            self._barge_in_residual_floor,
        )

    def has_barge_in_evidence(self, sensitivity_db: float) -> tuple[bool, dict]:
        """Confirm post-AEC voice and energy without disturbing playback."""
        values = list(self._barge_in_rms_window)
        candidate = values[-6:]
        voice_candidate = list(self._barge_in_voice_window)[-6:]
        similarity_candidate = list(self._barge_in_similarity_window)[-6:]
        if len(similarity_candidate) < len(candidate):
            # No score is a valid state at playback onset or in a quiet render
            # gap. Align those chunks with the other evidence instead of
            # shifting later similarity scores onto earlier audio.
            similarity_candidate = [None] * (
                len(candidate) - len(similarity_candidate)
            ) + similarity_candidate
        residual_floor = max(
            SILENCE_THRESHOLD * 0.35,
            self._barge_in_candidate_floor
            if self._barge_in_candidate_floor is not None
            else self._barge_in_residual_floor,
        )

        adaptive_threshold = max(
            150.0,
            residual_floor * math.pow(10.0, float(sensitivity_db) / 20.0),
            SILENCE_THRESHOLD * 0.65,
        )
        evidence = sum(rms >= adaptive_threshold for rms in candidate)
        # High and Balanced both need three energetic chunks. Balanced remains
        # stricter through its higher relative energy threshold and its extra
        # post-AEC voice frame; requiring a fourth energy chunk made short,
        # clearly voiced phrases fail after both VADs had already agreed.
        required = 3 if sensitivity_db <= 9.0 else 4
        voice_evidence = sum(voice_candidate)
        voice_required = (
            2 if sensitivity_db <= 6.0 else 3 if sensitivity_db <= 9.0 else 4
        )
        voice_confirmed = (
            len(voice_candidate) >= voice_required and voice_evidence >= voice_required
        )
        # VADs detect speech, not the speaker. Require the energetic voiced
        # chunks to be dissimilar to Billy's exact outgoing audio before they
        # may stop playback. A missing score means there was no usable render
        # reference, so the sound cannot be explained as current speaker echo.
        playback_similarity_limit = 0.30
        independent_evidence = sum(
            rms >= adaptive_threshold
            and voice
            and (similarity is None or similarity < playback_similarity_limit)
            for rms, voice, similarity in zip(
                candidate, voice_candidate, similarity_candidate
            )
        )
        independent_required = voice_required
        independent_confirmed = (
            len(similarity_candidate) >= independent_required
            and independent_evidence >= independent_required
        )
        measured_similarities = [
            value for value in similarity_candidate if value is not None
        ]
        details = {
            "evidence": evidence,
            "required": required,
            "voice_evidence": voice_evidence,
            "voice_required": voice_required,
            "voice_frames": len(voice_candidate),
            "independent_evidence": independent_evidence,
            "independent_required": independent_required,
            "playback_similarity": max(measured_similarities, default=None),
            "playback_similarity_min": min(measured_similarities, default=None),
            "playback_similarity_limit": playback_similarity_limit,
            "threshold": adaptive_threshold,
            "residual_floor": residual_floor,
            "peak": max(candidate, default=0.0),
            "learning": len(values) < 6,
        }
        return (
            len(values) >= 6
            and evidence >= required
            and voice_confirmed
            and independent_confirmed
        ), details

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
        playback_active = not audio.playback_done_event.is_set()
        if aec_active:
            # AEC3 receives the exact speaker PCM from the output worker and the
            # corresponding capture frames here. Its internal delay estimator and
            # double-talk handling decide what belongs to Billy and what belongs
            # to the near-end speaker. Silent render frames keep the complete
            # WebRTC front end (including NS/AGC) continuous between responses.
            samples = audio.process_mic_with_aec(
                samples,
                render_active=playback_active,
            )

        full_duplex_barge_in = (
            aec_active
            and self.session._server_barge_in_enabled
            and (
                self.session.state.assistant_speaking
                or self.session.state.response_active
            )
        )

        # With AEC3 active, the provider receives the cleaned mic continuously
        # and owns speech-start/end detection. Without it, retain safe half-duplex
        # gating so Billy's speaker cannot become user input.
        if not self.session.state.allow_mic_input and not full_duplex_barge_in:
            return

        if full_duplex_barge_in and not self._logged_barge_in_active:
            logger.info(
                "WebRTC AEC3 full-duplex barge-in active; post-AEC voice and "
                "provider VAD are listening.",
                "👂",
            )
            self._logged_barge_in_active = True
        elif not full_duplex_barge_in:
            self._logged_barge_in_active = False

        if self.session.state.assistant_speaking and not full_duplex_barge_in:
            return

        if self.session.state.response_active and not full_duplex_barge_in:
            return

        if (
            not TEXT_ONLY_MODE
            and not audio.playback_done_event.is_set()
            and not full_duplex_barge_in
        ):
            if not self._logged_waiting_for_wakeup:
                logger.info("Mic waiting for wake-up sound to finish...", "⏳")
                self._logged_waiting_for_wakeup = True
            return

        rms = calculate_input_rms(samples)

        if not self._mic_data_started and not TEXT_ONLY_MODE:
            logger.info("Mic data now being sent", "🎤")
            self._mic_data_started = True

        self.last_rms = rms
        now = time.time()

        # Ignore the short mechanical burst after opening/repositioning Billy.
        if now < self._timeout_motor_guard_until:
            return

        self.session.state.observe_rms(rms)
        if full_duplex_barge_in:
            if not self.session.state._server_input_speaking:
                self.observe_barge_in_residual(rms)
            self._barge_in_rms_window.append(rms)
            self._barge_in_voice_window.append(audio.aec_voice_detected() is True)
            self._barge_in_similarity_window.append(audio.aec_playback_similarity())
        else:
            self._barge_in_rms_window.clear()
            self._barge_in_voice_window.clear()
            self._barge_in_similarity_window.clear()
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
        if full_duplex_barge_in and self.session.state._server_input_speaking:
            self.session.schedule_server_barge_in_confirmation()
        if self.session.ws is None or not self.session.session_initialized:
            self._store_prebuffer(samples)
            return
        if self._prebuffered_audio:
            self.flush_prebuffer()
        audio.send_mic_audio(self.session.ws, samples, self.session.loop)

    async def timeout_checker(self):
        """Monitor mic activity and timeout if idle too long."""
        from ..config import MIC_TIMEOUT_SECONDS
        from ..mood import mood_manager
        from ..movements import move_head, move_tail_async
        from ..status_led import (
            clear_status_led_timeout_progress,
            set_status_led_timeout_progress,
        )

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
                if not self._timeout_head_retracted:
                    move_head("off")
                    self._timeout_head_retracted = True
                    self._timeout_motor_guard_until = max(
                        self._timeout_motor_guard_until, now + 0.6
                    )
                    self.session.last_activity[0] = now
                    await asyncio.sleep(0.6)
                    continue
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
                set_status_led_timeout_progress(progress)
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

                if MIC_TIMEOUT_TAIL_FLAP and now - last_tail_move > 1.0:
                    motion = mood_manager.get_motion_profile()
                    move_tail_async(duration=motion.get("tail_duration", 0.2))
                    self._timeout_motor_guard_until = max(
                        self._timeout_motor_guard_until, now + 0.35
                    )
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
                    clear_status_led_timeout_progress()
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
                self._timeout_head_retracted = False
                clear_status_led_timeout_progress()

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
