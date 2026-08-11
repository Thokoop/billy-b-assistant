from __future__ import annotations

import contextlib
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from .config import ROOT_DIR
from .logger import logger
from .wakeword_provider import WakeWordBackend, wakeword_provider_registry


class LocalWakeWordListener:
    """Background local wake-word listener with pluggable backend."""

    def __init__(
        self,
        *,
        backend: str = "porcupine",
        callback,
        is_session_active,
        cooldown_seconds: float = 4.0,
        device_index: int | None = None,
        capture_rate: int | None = None,
        capture_channels: int = 1,
        porcupine_access_key: str = "",
        porcupine_keyword_path: str = "",
        porcupine_sensitivity: float = 0.65,
        openwakeword_model_path: str = "",
        openwakeword_threshold: float = 0.5,
        openwakeword_melspec_model_path: str = "",
        openwakeword_embedding_model_path: str = "",
    ):
        self.backend = backend.strip().lower()
        self.callback = callback
        self.is_session_active = is_session_active
        self.cooldown_seconds = float(cooldown_seconds)
        self.device_index = device_index
        self.capture_rate = int(capture_rate) if capture_rate else None
        self.capture_channels = max(1, int(capture_channels))
        self.porcupine_access_key = porcupine_access_key.strip()
        self.porcupine_keyword_path = porcupine_keyword_path.strip()
        self.porcupine_sensitivity = float(porcupine_sensitivity)
        self.openwakeword_model_path = openwakeword_model_path.strip()
        self.openwakeword_threshold = float(openwakeword_threshold)
        self.openwakeword_melspec_model_path = openwakeword_melspec_model_path.strip()
        self.openwakeword_embedding_model_path = (
            openwakeword_embedding_model_path.strip()
        )

        self._thread = None
        self._stop_event = threading.Event()
        self._backend_impl: WakeWordBackend | None = None
        self._last_status_log_at = 0.0
        self._open_error_backoff_seconds = 0.5

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        backend_impl = self._build_backend()
        if backend_impl is None:
            return
        if not backend_impl.initialize():
            return
        self._backend_impl = backend_impl

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(
            f'Wake-word listener enabled ({backend_impl.backend_name}): "{backend_impl.keyword_label}".',
            "👂",
        )

    def stop(self):
        self._stop_event.set()
        if (
            self._thread
            and self._thread.is_alive()
            # If stop() is invoked from the wake-word worker itself (e.g. direct
            # callback path), joining current thread raises RuntimeError.
            and threading.current_thread() is not self._thread
        ):
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._backend_impl is not None:
            self._backend_impl.close()
            self._backend_impl = None

    def _build_backend(self) -> WakeWordBackend | None:
        try:
            return wakeword_provider_registry.create_provider(
                self.backend,
                root_dir=ROOT_DIR,
                porcupine_access_key=self.porcupine_access_key,
                porcupine_keyword_path=self.porcupine_keyword_path,
                porcupine_sensitivity=self.porcupine_sensitivity,
                openwakeword_model_path=self.openwakeword_model_path,
                openwakeword_threshold=self.openwakeword_threshold,
                openwakeword_melspec_model_path=self.openwakeword_melspec_model_path,
                openwakeword_embedding_model_path=self.openwakeword_embedding_model_path,
            )
        except ValueError:
            logger.warning(
                f'Unsupported wake-word backend "{self.backend}". Implement provider in core/providers/*.py and register it in core/providers/__init__.py.',
                "⚠️",
            )
            return None

    def _run(self):
        while not self._stop_event.is_set():
            if self.is_session_active():
                time.sleep(0.2)
                continue

            try:
                detected = self._listen_until_detected_or_paused()
                # Successful listen cycle (detected or paused) resets backoff.
                self._open_error_backoff_seconds = 0.5
            except Exception as e:
                msg = str(e).lower()
                if "device unavailable" in msg or "-9985" in msg:
                    sleep_for = min(self._open_error_backoff_seconds, 8.0)
                    logger.warning(
                        f"Wake-word mic unavailable (-9985). Backing off for {sleep_for:.1f}s: {e}",
                        "⚠️",
                    )
                    self._open_error_backoff_seconds = min(
                        self._open_error_backoff_seconds * 2.0, 8.0
                    )
                else:
                    sleep_for = 1.0
                    logger.warning(f"Wake-word listen loop error: {e}", "⚠️")
                time.sleep(sleep_for)
                continue

            if not detected:
                continue

            label = (
                self._backend_impl.keyword_label if self._backend_impl else "wakeword"
            )
            logger.info(f'Wake-word detected: "{label}"', "🗣️")
            try:
                self.callback()
            except Exception as e:
                logger.warning(f"Wake-word callback failed: {e}", "⚠️")

            cooldown_until = time.time() + self.cooldown_seconds
            while time.time() < cooldown_until and not self._stop_event.is_set():
                time.sleep(0.1)

    def _listen_until_detected_or_paused(self) -> bool:
        if self._backend_impl is None:
            return False

        audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=128)
        frame_length = int(self._backend_impl.frame_length)
        model_rate = int(self._backend_impl.sample_rate)
        capture_rate = int(self.capture_rate or model_rate)
        capture_channels = int(self.capture_channels or 1)
        capture_frame_length = max(
            1, int(round(frame_length * capture_rate / model_rate))
        )
        mono_buffer = np.array([], dtype=np.int16)

        def _on_audio(indata, frames, time_info, status):
            if status:
                now = time.time()
                if now - self._last_status_log_at >= 3.0:
                    logger.warning(f"Wake-word stream status: {status}", "⚠️")
                    self._last_status_log_at = now
            try:
                audio_queue.put_nowait(bytes(indata))
            except queue.Full:
                # Drop oldest chunk to keep latest audio flowing.
                with contextlib.suppress(queue.Empty):
                    audio_queue.get_nowait()
                with contextlib.suppress(queue.Full):
                    audio_queue.put_nowait(bytes(indata))

        with sd.RawInputStream(
            samplerate=capture_rate,
            device=self.device_index,
            channels=capture_channels,
            dtype="int16",
            blocksize=0,
            latency="high",
            callback=_on_audio,
        ):
            while not self._stop_event.is_set():
                if self.is_session_active():
                    return False
                try:
                    chunk = audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                pcm = np.frombuffer(chunk, dtype=np.int16)
                if pcm.size == 0:
                    continue

                if capture_channels > 1:
                    usable = (pcm.size // capture_channels) * capture_channels
                    if usable == 0:
                        continue
                    pcm = (
                        pcm[:usable]
                        .reshape(-1, capture_channels)
                        .mean(axis=1)
                        .astype(np.int16)
                    )

                mono_buffer = np.concatenate((mono_buffer, pcm))

                while mono_buffer.size >= capture_frame_length:
                    capture_frame = mono_buffer[:capture_frame_length]
                    mono_buffer = mono_buffer[capture_frame_length:]

                    if (
                        capture_rate != model_rate
                        or capture_frame_length != frame_length
                    ):
                        frame = self._resample_frame(
                            capture_frame,
                            target_length=frame_length,
                            source_rate=capture_rate,
                            target_rate=model_rate,
                        )
                    else:
                        frame = capture_frame

                    if self._backend_impl.process(frame):
                        return True
        return False

    @staticmethod
    def _resample_frame(
        samples: np.ndarray,
        *,
        target_length: int,
        source_rate: int,
        target_rate: int,
    ) -> np.ndarray:
        # Fast path for common 48kHz -> 16kHz decimation.
        if source_rate > 0 and target_rate > 0 and source_rate % target_rate == 0:
            step = source_rate // target_rate
            needed = target_length * step
            if samples.size >= needed:
                decimated = samples[:needed:step]
                if decimated.size == target_length:
                    return decimated.astype(np.int16, copy=False)

        if samples.size == target_length:
            return samples.astype(np.int16, copy=False)
        if samples.size <= 1:
            return np.zeros(target_length, dtype=np.int16)

        x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_length, endpoint=False)
        resampled = np.interp(x_new, x_old, samples.astype(np.float32))
        return np.clip(resampled, -32768, 32767).astype(np.int16)
