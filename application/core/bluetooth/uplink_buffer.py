"""A bounded, demand-driven PCM reservoir for Bluetooth calls only.

The writer must be paced by the consumer's pipe backpressure, not a Python
10 ms timer. During startup/rebuffering it supplies silence so pw-cat never
has to wait for a microphone callback inside its processing cycle.
"""

import threading

import numpy as np


class UplinkBuffer:
    rate = 48000
    frame_samples = 480
    target_samples = 2880  # 60 ms in this reservoir, excluding pipe/PipeWire.
    prefill_samples = 3840  # 80 ms: margin for 40 ms capture / 20 ms pipe bursts.
    capacity = 9600  # Never retain more than 200 ms of processed microphone PCM.
    max_adjust_ppm = 2000.0

    def __init__(self):
        self._lock = threading.Lock()
        self._pcm = np.empty(self.capacity, dtype=np.int16)
        self._start = 0
        self._size = 0
        self._phase = 0.0
        self._buffering = True
        self._started = False
        self._fade_in = False
        self._last_sample = 0
        self._level = float(self.target_samples)
        self._adjust_ppm = 0.0
        self._frames = 0
        self._counts = dict(
            buffer_underruns=0,
            buffer_overruns=0,
            buffer_dropped_ms=0.0,
            startup_silence_ms=0.0,
            rebuffer_silence_ms=0.0,
        )
        self._minimum = self.capacity
        self._maximum = 0

    def push(self, samples):
        samples = np.asarray(samples, dtype=np.int16).reshape(-1)
        with self._lock:
            excess = max(0, self._size + len(samples) - self.capacity)
            if excess:
                self._counts['buffer_overruns'] += 1
                self._counts['buffer_dropped_ms'] += excess / 48.0
                discard = min(excess, self._size)
                self._start = (self._start + discard) % self.capacity
                self._size -= discard
                samples = samples[max(0, excess - discard) :]
                self._phase = 0.0
                self._fade_in = True
            end = (self._start + self._size) % self.capacity
            indices = (end + np.arange(len(samples))) % self.capacity
            self._pcm[indices] = samples
            self._size += len(samples)
            self._maximum = max(self._maximum, self._size)

    def _silence(self):
        key = 'rebuffer_silence_ms' if self._started else 'startup_silence_ms'
        self._counts[key] += 10.0
        output = np.zeros(self.frame_samples, dtype=np.int16)
        # Fade only a real discontinuity, never every normal 10 ms boundary.
        if self._last_sample:
            output[:240] = np.linspace(self._last_sample, 0, 240).astype(np.int16)
        self._last_sample = 0
        return output.tobytes()

    def read(self):
        """Return exactly 10 ms, without waiting for capture or replaying speech."""
        with self._lock:
            self._minimum = min(self._minimum, self._size)
            if self._buffering:
                if self._size < self.prefill_samples:
                    return self._silence()
                self._buffering = False
                self._fade_in = self._started
                self._started = True
                self._level = float(self.target_samples)
                self._frames = 0
                self._adjust_ppm = 0.0

            # Average over roughly one second, rather than reacting to each
            # hardware/stdio burst. Wait two seconds after prefill to settle.
            self._level += 0.01 * (self._size - self._level)
            self._frames += 1
            if self._frames > 200:
                error_seconds = (self._level - self.target_samples) / self.rate
                self._adjust_ppm = float(
                    np.clip(
                        error_seconds * 50000.0,
                        -self.max_adjust_ppm,
                        self.max_adjust_ppm,
                    )
                )
            step = 1.0 + self._adjust_ppm / 1e6
            end = self._phase + self.frame_samples * step
            consumed = int(end)
            positions = self._phase + np.arange(self.frame_samples) * step
            left = positions.astype(np.int64)
            fraction = positions - left
            right = left + (fraction > 1e-9)
            needed = max(consumed, int(right[-1]) + 1)
            if self._size < needed:
                self._counts['buffer_underruns'] += 1
                self._buffering = True
                self._phase = 0.0
                self._adjust_ppm = 0.0
                return self._silence()

            # Continuous fractional position across blocks: no independent
            # per-block resampling filters or periodic whole-frame drops.
            a = self._pcm[(self._start + left) % self.capacity].astype(np.float64)
            b = self._pcm[(self._start + right) % self.capacity].astype(np.float64)
            output = np.rint(a + fraction * (b - a)).astype(np.int16)
            self._phase = end - consumed
            self._start = (self._start + consumed) % self.capacity
            self._size -= consumed
            if self._fade_in:
                output[:240] = np.rint(output[:240] * np.linspace(0, 1, 240))
                self._fade_in = False
            self._last_sample = int(output[-1])
            return output.tobytes()

    def snapshot(self):
        """Interval counters and occupancy, independent of capture diagnostics."""
        with self._lock:
            result = dict(
                self._counts,
                buffer_ms=self._size / 48.0,
                buffer_min_ms=min(self._minimum, self._size) / 48.0,
                buffer_max_ms=max(self._maximum, self._size) / 48.0,
                clock_adjust_ppm=self._adjust_ppm,
                buffering=self._buffering,
            )
            self._counts = dict.fromkeys(self._counts, 0)
            self._minimum = self._size
            self._maximum = self._size
            return result
