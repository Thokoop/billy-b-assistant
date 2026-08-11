"""WebRTC AEC3 audio front end for full-duplex conversations."""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
from scipy.signal import correlate, resample_poly

from .logger import logger


class _WebRtcAudioProcessor:
    """Small adapter around the bundled WebRTC Audio Processing binding."""

    def __init__(self, sample_rate: int):
        import webrtcvad
        from aec_audio_processing import AudioProcessor

        self._processor = AudioProcessor(
            enable_aec=True,
            enable_ns=True,
            ns_level=1,
            enable_agc=True,
            agc_mode=1,
            # Version 1.0.1 accepts this argument but its bundled VAD
            # initialization is commented out upstream. Run the maintained
            # standalone WebRTC VAD on AEC3's output instead.
            enable_vad=False,
        )
        self._sample_rate = sample_rate
        self._voice_detector = webrtcvad.Vad(1)
        self._last_voice_detected = False
        self._processor.set_stream_format(sample_rate, 1, sample_rate, 1)
        self._processor.set_reverse_stream_format(sample_rate, 1)
        self._processor.set_stream_delay(0)

    def process_render(self, frame: np.ndarray) -> None:
        self._processor.process_reverse_stream(frame.tobytes())

    def process_capture(self, frame: np.ndarray) -> np.ndarray:
        output = self._processor.process_stream(frame.tobytes())
        # WebRTC VAD accepts a 10 ms 16-bit mono frame at 48 kHz. Running it on
        # AEC3's output is important: running before AEC would classify Billy's
        # own far-end voice as speech too.
        self._last_voice_detected = bool(
            self._voice_detector.is_speech(output, self._sample_rate)
        )
        return np.frombuffer(output, dtype=np.int16).copy()

    def voice_detected(self) -> bool:
        return self._last_voice_detected

    def set_stream_delay(self, delay_ms: int) -> None:
        self._processor.set_stream_delay(int(delay_ms))


class EchoCanceller:
    """Bridge Billy's audio streams to WebRTC AEC3's 10 ms frames."""

    sample_rate = 48000
    frame_samples = 480

    def __init__(self, enabled: bool, processor_factory=None):
        self.requested = enabled
        self._processor_factory = processor_factory
        self._processor = None
        self._lock = threading.Lock()
        self._initialize_lock = threading.Lock()
        self._render_history = deque(maxlen=200)
        self._render_pending = np.empty(0, dtype=np.int16)
        self._render_generation = 0
        self._render_frames = 0
        self._render_first_write_at = None
        self._render_written_samples = 0
        self._render_latency_seconds = 0.0
        self._capture_latency_seconds = 0.0
        self._stream_delay_ms = 0
        self._last_voice_detected: bool | None = None
        self._last_playback_similarity: float | None = None
        self._failure_logged = False
        self._processing_failure_logged = False
        self._initialization_attempted = False

    @property
    def active(self) -> bool:
        return self._processor is not None

    @property
    def engine_name(self) -> str:
        return "WebRTC AEC3"

    @property
    def render_ready(self) -> bool:
        with self._lock:
            return self._render_frames >= 3

    @property
    def voice_detected(self) -> bool | None:
        """Return WebRTC VAD's decision for the latest processed capture frame."""
        with self._lock:
            return self._last_voice_detected

    @property
    def playback_similarity(self) -> float | None:
        """Return normalized similarity between cleaned mic and recent playback."""
        with self._lock:
            return self._last_playback_similarity

    def initialize(self) -> bool:
        if not self.requested:
            return False
        if self._processor is not None:
            return True
        with self._initialize_lock:
            if self._processor is not None:
                return True
            if self._initialization_attempted:
                return False
            self._initialization_attempted = True
            try:
                if self._processor_factory is None:
                    processor = _WebRtcAudioProcessor(self.sample_rate)
                else:
                    processor = self._processor_factory(sample_rate=self.sample_rate)
                self._processor = processor
                logger.success(
                    "WebRTC AEC3 audio processing enabled "
                    "(echo cancellation, noise suppression, automatic gain control, "
                    "post-AEC voice detection)."
                )
                return True
            except Exception as exc:
                if not self._failure_logged:
                    logger.error(
                        "AEC was requested but WebRTC AEC3 could not start; keeping "
                        "half-duplex mic gating enabled. "
                        f"Error: {exc}"
                    )
                    self._failure_logged = True
                return False

    @staticmethod
    def _to_mono_int16(samples) -> np.ndarray:
        data = np.asarray(samples)
        if data.ndim > 1:
            data = data[:, 0]
        if np.issubdtype(data.dtype, np.floating):
            maximum = float(np.max(np.abs(data))) if data.size else 0.0
            if maximum <= 1.5:
                data = data * 32767.0
        return np.clip(data, -32768, 32767).astype(np.int16)

    @staticmethod
    def _resample(samples: np.ndarray, source_rate: int, target_rate: int):
        if source_rate == target_rate:
            return samples
        divisor = np.gcd(source_rate, target_rate)
        converted = resample_poly(
            samples.astype(np.float32, copy=False),
            target_rate // divisor,
            source_rate // divisor,
        )
        return np.clip(converted, -32768, 32767).astype(np.int16)

    def _update_stream_delay_locked(self) -> None:
        delay_ms = int(
            round(
                1000.0
                * max(
                    0.0,
                    self._render_latency_seconds + self._capture_latency_seconds,
                )
            )
        )
        delay_ms = min(500, delay_ms)
        if delay_ms == self._stream_delay_ms or self._processor is None:
            return
        self._processor.set_stream_delay(delay_ms)
        self._stream_delay_ms = delay_ms
        logger.info(
            f"WebRTC AEC3 stream delay updated to {delay_ms} ms.",
            "🔊",
        )

    def set_render_latency(self, seconds: float | None) -> None:
        with self._lock:
            self._render_latency_seconds = max(0.0, float(seconds or 0.0))
            self._update_stream_delay_locked()

    def set_capture_latency(self, seconds: float | None) -> None:
        with self._lock:
            self._capture_latency_seconds = max(0.0, float(seconds or 0.0))
            self._update_stream_delay_locked()

    def begin_render_generation(self) -> int:
        """Start an isolated playback reference without resetting AEC3."""
        with self._lock:
            self._render_generation += 1
            self._render_history.clear()
            self._render_pending = np.empty(0, dtype=np.int16)
            self._render_frames = 0
            self._render_first_write_at = None
            self._render_written_samples = 0
            self._last_playback_similarity = None
            return self._render_generation

    def _playback_similarity_locked(self, cleaned: np.ndarray) -> float | None:
        """Measure how much a cleaned capture still resembles Billy's voice.

        A VAD can only answer "is this speech?"; it cannot tell who spoke.  Use
        normalized cross-correlation against the exact recent speaker PCM as an
        ownership check.  Searching across a short render window absorbs USB,
        PortAudio and acoustic delay without depending on microphone gain or
        speaker volume.
        """
        if cleaned.size < self.frame_samples or not self._render_history:
            return None

        # The measured stream delay is normally around 70 ms, but USB devices
        # can buffer less or considerably more. Half a second covers that
        # variation while keeping the FFT small enough for a Raspberry Pi.
        history_frames = min(len(self._render_history), 50)
        reference = np.concatenate([
            frame for _timestamp, frame in list(self._render_history)[-history_frames:]
        ]).astype(np.float32, copy=False)
        query = np.asarray(cleaned, dtype=np.float32)
        if reference.size < query.size:
            return None

        # Remove DC offsets. The score is normalized by both signal energies,
        # which makes it independent of configured mic gain and playback volume.
        reference = reference - float(np.mean(reference))
        query = query - float(np.mean(query))
        query_energy = float(np.dot(query, query))
        if query_energy <= 1.0:
            return None

        correlation = correlate(reference, query, mode="valid", method="fft")
        squared = reference * reference
        cumulative = np.concatenate((
            np.zeros(1, dtype=np.float64),
            np.cumsum(squared, dtype=np.float64),
        ))
        window_energy = cumulative[query.size :] - cumulative[: -query.size]
        denominator = np.sqrt(np.maximum(window_energy * query_energy, 1.0))
        score = np.abs(correlation) / denominator
        if not score.size:
            return None
        return min(1.0, float(np.max(score)))

    def invalidate_render_generation(self) -> None:
        """Discard references and accounting belonging to interrupted playback."""
        self.begin_render_generation()

    def is_current_render_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._render_generation

    def feed_render(self, samples, source_rate: int, generation=None) -> None:
        """Feed the exact mono PCM that is about to be written to the speaker."""
        if not self.initialize():
            return
        mono = self._to_mono_int16(samples)
        render = self._resample(mono, source_rate, self.sample_rate)
        try:
            with self._lock:
                if generation is not None and generation != self._render_generation:
                    return
                if self._render_pending.size:
                    render = np.concatenate((self._render_pending, render))
                usable = len(render) - (len(render) % self.frame_samples)
                for offset in range(0, usable, self.frame_samples):
                    frame = render[offset : offset + self.frame_samples].copy()
                    self._processor.process_render(frame)
                    self._render_history.append((time.monotonic(), frame))
                    self._render_frames += 1
                self._render_pending = render[usable:].copy()
        except Exception as exc:
            if not self._processing_failure_logged:
                logger.warning(f"WebRTC AEC3 render processing failed: {exc}")
                self._processing_failure_logged = True

    def mark_render_written(
        self,
        sample_count: int,
        sample_rate: int,
        generation=None,
        write_started_at: float | None = None,
    ) -> None:
        """Record speaker writes so interrupted conversation audio can be truncated."""
        if sample_count <= 0 or sample_rate <= 0:
            return
        with self._lock:
            if generation is not None and generation != self._render_generation:
                return
            if self._render_first_write_at is None:
                self._render_first_write_at = write_started_at or time.monotonic()
            self._render_written_samples += int(
                round(sample_count * self.sample_rate / sample_rate)
            )

    def heard_audio_ms(self, generation=None) -> int:
        """Estimate how much generated audio has physically reached the speaker."""
        with self._lock:
            if generation is not None and generation != self._render_generation:
                return 0
            if self._render_first_write_at is None:
                return 0
            written_seconds = self._render_written_samples / self.sample_rate
            elapsed_seconds = max(
                0.0,
                time.monotonic()
                - self._render_first_write_at
                - self._render_latency_seconds,
            )
            heard_seconds = min(written_seconds, elapsed_seconds)
            return max(0, int(round(heard_seconds * 1000.0)))

    def process_capture(
        self,
        samples,
        source_rate: int,
        *,
        render_active: bool = True,
    ) -> np.ndarray:
        """Return AEC3-cleaned mono PCM at the microphone's original rate."""
        mono = self._to_mono_int16(samples)
        if not self.initialize():
            return mono
        capture = self._resample(mono, source_rate, self.sample_rate)
        usable = len(capture) - (len(capture) % self.frame_samples)
        output = []
        voice_decisions = []
        try:
            with self._lock:
                self._update_stream_delay_locked()
                for offset in range(0, usable, self.frame_samples):
                    frame = capture[offset : offset + self.frame_samples]
                    if not render_active:
                        self._processor.process_render(
                            np.zeros(self.frame_samples, dtype=np.int16)
                        )
                    output.append(self._processor.process_capture(frame))
                    detector = getattr(self._processor, "voice_detected", None)
                    if callable(detector):
                        voice_decisions.append(bool(detector()))
                if voice_decisions:
                    # PortAudio normally gives Billy 40 ms capture chunks while
                    # WebRTC evaluates four individual 10 ms frames. Retaining
                    # only the final decision misses a syllable whenever the
                    # last frame happens to be quiet.
                    self._last_voice_detected = any(voice_decisions)
                if render_active and output:
                    self._last_playback_similarity = self._playback_similarity_locked(
                        np.concatenate(output)
                    )
                else:
                    self._last_playback_similarity = None
        except Exception as exc:
            if not self._processing_failure_logged:
                logger.warning(f"WebRTC AEC3 capture processing failed: {exc}")
                self._processing_failure_logged = True
            with self._lock:
                self._last_playback_similarity = None
            return mono
        if not output:
            return mono
        if usable < len(capture):
            output.append(capture[usable:])
        cleaned = np.concatenate(output)
        restored = self._resample(cleaned, self.sample_rate, source_rate)
        if len(restored) != len(mono):
            adjusted = np.zeros(len(mono), dtype=np.int16)
            copied = min(len(restored), len(adjusted))
            adjusted[:copied] = restored[:copied]
            restored = adjusted
        return restored
