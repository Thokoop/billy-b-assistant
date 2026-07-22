"""SpeexDSP acoustic echo cancellation for full-duplex conversations."""

import threading
import time
from collections import deque

import numpy as np
from scipy.signal import resample_poly

from .logger import logger


class EchoCanceller:
    """Bridge Billy's variable-rate chunks to SpeexDSP's 10 ms AEC frames."""

    sample_rate = 48000
    frame_samples = 480

    def __init__(self, enabled: bool, processor_factory=None):
        self.requested = enabled
        self._processor_factory = processor_factory
        self._processor = None
        self._lock = threading.Lock()
        self._render_history = deque(maxlen=200)
        self._failure_logged = False
        self._initialization_attempted = False

    @property
    def active(self) -> bool:
        return self._processor is not None

    def initialize(self) -> bool:
        if not self.requested:
            return False
        if self._processor is not None:
            return True
        if self._initialization_attempted:
            return False
        self._initialization_attempted = True
        try:
            factory = self._processor_factory
            if factory is None:
                from pyaec import Aec

                factory = Aec
            processor = factory(
                frame_size=self.frame_samples,
                filter_length=int(self.sample_rate * 0.25),
                sample_rate=self.sample_rate,
                enable_preprocess=True,
            )
            self._processor = processor
            logger.success("SpeexDSP echo cancellation enabled (adaptive delay).")
            return True
        except Exception as exc:
            if not self._failure_logged:
                logger.error(
                    "AEC was requested but could not start; keeping half-duplex mic "
                    f"gating enabled. Error: {exc}"
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
            samples, target_rate // divisor, source_rate // divisor
        )
        return np.clip(converted, -32768, 32767).astype(np.int16)

    def _frames(self, samples: np.ndarray):
        usable = len(samples) - (len(samples) % self.frame_samples)
        for offset in range(0, usable, self.frame_samples):
            yield samples[offset : offset + self.frame_samples]

    def feed_render(self, samples, source_rate: int) -> None:
        """Provide the exact PCM about to be written to Billy's speaker."""
        if not self.initialize():
            return
        mono = self._to_mono_int16(samples)
        render = self._resample(mono, source_rate, self.sample_rate)
        started_at = time.monotonic()
        with self._lock:
            for index, frame in enumerate(self._frames(render)):
                played_at = started_at + (index * 0.01)
                self._render_history.append((played_at, frame.copy()))

    def _reference_for(self, captured_at: float) -> np.ndarray:
        # Speex's adaptive filter models the actual speaker-to-microphone delay.
        # Pair by wall-clock time here; applying that delay a second time selects
        # the wrong far-end audio and makes Billy's own voice survive AEC.
        target = captured_at
        reference = None
        for played_at, frame in reversed(self._render_history):
            if played_at <= target:
                reference = frame
                break
        if reference is None:
            return np.zeros(self.frame_samples, dtype=np.int16)
        while self._render_history and self._render_history[0][0] < target - 1.0:
            self._render_history.popleft()
        return reference

    def process_capture(self, samples, source_rate: int) -> np.ndarray:
        """Return echo-cancelled mono PCM at the microphone's original rate."""
        mono = self._to_mono_int16(samples)
        if not self.initialize():
            return mono
        capture = self._resample(mono, source_rate, self.sample_rate)
        output = []
        duration = len(capture) / self.sample_rate
        captured_at = time.monotonic() - duration
        try:
            with self._lock:
                for index, frame in enumerate(self._frames(capture)):
                    reference = self._reference_for(captured_at + (index * 0.01))
                    cleaned = self._processor.cancel_echo(
                        frame.tolist(), reference.tolist()
                    )
                    output.append(np.asarray(cleaned, dtype=np.int16))
        except Exception as exc:
            logger.warning(f"AEC capture processing failed: {exc}")
            return mono
        if not output:
            return mono
        cleaned = np.concatenate(output)
        restored = self._resample(cleaned, self.sample_rate, source_rate)
        if len(restored) != len(mono):
            adjusted = np.zeros(len(mono), dtype=np.int16)
            copied = min(len(restored), len(adjusted))
            adjusted[:copied] = restored[:copied]
            restored = adjusted
        return restored
