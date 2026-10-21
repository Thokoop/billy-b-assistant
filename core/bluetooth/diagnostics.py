"""Bounded, content-free measurements of the microphone-to-pipe path.

Successful writes mean pw-cat accepted bytes, not that the phone received them.
PipeWire/SCO underruns must still be measured on the Pi.
"""

import threading


class UplinkTiming:
    """Capture and output workers plus reporting; no locks in the mic callback."""

    def __init__(self, now):
        self._lock = threading.Lock()
        self._since = now
        self._last_capture = None
        self._last_sent = None
        self._counts = {}
        self._reset()

    def _reset(self):
        self._maxima = dict.fromkeys(
            ("capture_gap_ms", "queue_ms", "process_ms", "write_ms", "send_gap_ms"),
            0.0,
        )
        self._audio_ms = 0.0

    def record_capture(self, captured_at, started_at, processed_at):
        with self._lock:
            self._maxima["queue_ms"] = max(
                self._maxima["queue_ms"], (started_at - captured_at) * 1000
            )
            self._maxima["process_ms"] = max(
                self._maxima["process_ms"], (processed_at - started_at) * 1000
            )
            if self._last_capture is not None:
                self._maxima["capture_gap_ms"] = max(
                    self._maxima["capture_gap_ms"],
                    (captured_at - self._last_capture) * 1000,
                )
            self._last_capture = captured_at

    def record_send(self, started_at, sent_at, audio_seconds):
        with self._lock:
            self._maxima["write_ms"] = max(
                self._maxima["write_ms"], (sent_at - started_at) * 1000
            )
            if self._last_sent is not None:
                self._maxima["send_gap_ms"] = max(
                    self._maxima["send_gap_ms"], (sent_at - self._last_sent) * 1000
                )
            self._last_sent = sent_at
            self._audio_ms += audio_seconds * 1000

    def record(self, captured_at, started_at, processed_at, sent_at, audio_seconds):
        self.record_capture(captured_at, started_at, processed_at)
        self.record_send(processed_at, sent_at, audio_seconds)

    def snapshot(self, now, counters, queued):
        with self._lock:
            elapsed = max(now - self._since, 1e-9)
            counts = {
                key: value - self._counts.get(key, 0) for key, value in counters.items()
            }
            # Also expose an ongoing stall when no block completes this interval.
            since_sent = now - (
                self._last_sent if self._last_sent is not None else self._since
            )
            result = {
                "interval_s": elapsed,
                **counts,
                "sent_per_s": counts.get("sent", 0) / elapsed,
                "audio_ms_per_s": self._audio_ms / elapsed,
                "queued": queued,
                **self._maxima,
                "since_send_ms": max(0.0, since_sent * 1000),
            }
            self._counts = dict(counters)
            self._since = now
            self._reset()
            return result


def format_timing(values, expected_per_s):
    return (
        f"bluetooth mic: interval={values['interval_s']:.2f}s "
        f"sent={values['sent_per_s']:.1f}/{expected_per_s} per s "
        f"audio_ms_per_s={values['audio_ms_per_s']:.1f} "
        f"captured={values.get('captured', 0)} "
        f"overflow={values['overflow']} full={values['full']} "
        f"queued={values['queued']} "
        + " ".join(
            f"{key}={values[key]:.1f}"
            for key in (
                "capture_gap_ms",
                "queue_ms",
                "process_ms",
                "write_ms",
                "send_gap_ms",
                "since_send_ms",
            )
        )
        + "".join(
            f" {key}={values[key]:.1f}"
            for key in (
                "buffer_ms",
                "buffer_min_ms",
                "buffer_max_ms",
                "buffer_underruns",
                "buffer_overruns",
                "buffer_dropped_ms",
                "startup_silence_ms",
                "rebuffer_silence_ms",
                "clock_adjust_ppm",
            )
            if key in values
        )
        + " (timing maxima; delivery to phone unmeasured)"
    )
