from __future__ import annotations

import contextlib
from pathlib import Path

import numpy as np

from ..logger import logger
from ..wakeword_provider import WakeWordBackend


def _resolve_path(root_dir: str, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(root_dir) / path
    return path


def _resolve_keyword_path(root_dir: str, value: str) -> Path:
    if not value:
        return Path("")

    raw = Path(value)
    # Backward compatibility: existing absolute/relative paths continue to work.
    if raw.is_absolute() or raw.parent != Path("."):
        return _resolve_path(root_dir, value)

    # Filename-only mode: resolve from wakewords folder.
    candidate = Path(root_dir) / "wakewords" / raw.name
    if candidate.exists():
        return candidate
    return Path(root_dir) / "wakewords" / raw.name


class PorcupineWakeWordBackend(WakeWordBackend):
    def __init__(
        self,
        *,
        root_dir: str,
        porcupine_access_key: str,
        porcupine_keyword_path: str,
        porcupine_sensitivity: float,
        **_,
    ):
        self.root_dir = root_dir
        self.access_key = porcupine_access_key.strip()
        self.keyword_path_raw = porcupine_keyword_path.strip()
        raw_sensitivity = float(porcupine_sensitivity)
        # Keep sensitivity in Porcupine's expected range.
        self.sensitivity = max(0.0, min(1.0, raw_sensitivity))
        if self.sensitivity != raw_sensitivity:
            logger.warning(
                f"Porcupine sensitivity {raw_sensitivity} is out of range; clamped to {self.sensitivity}.",
                "⚠️",
            )

        self._model = None
        self._keyword_label = ""

    @property
    def backend_name(self) -> str:
        return "porcupine"

    @property
    def keyword_label(self) -> str:
        return self._keyword_label

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._model, "sample_rate", 16000))

    @property
    def frame_length(self) -> int:
        return int(getattr(self._model, "frame_length", 512))

    def initialize(self) -> bool:
        if not self.access_key:
            logger.warning(
                "PORCUPINE_ACCESS_KEY is empty; wake-word listener disabled.", "⚠️"
            )
            return False

        if not self.keyword_path_raw:
            logger.warning(
                "WAKE_WORD_PORCUPINE_KEYWORD_PATH is empty; set it to your .ppn file.",
                "⚠️",
            )
            return False

        keyword_path = _resolve_keyword_path(self.root_dir, self.keyword_path_raw)
        if not keyword_path.exists():
            logger.warning(
                f"Porcupine keyword file does not exist: {keyword_path}", "⚠️"
            )
            return False
        self._keyword_label = keyword_path.stem

        try:
            import pvporcupine
        except ImportError:
            logger.warning(
                "pvporcupine is not installed. Install requirements to enable wake-word.",
                "⚠️",
            )
            return False

        try:
            self._model = pvporcupine.create(
                access_key=self.access_key,
                keyword_paths=[str(keyword_path)],
                sensitivities=[self.sensitivity],
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to initialize Porcupine: {e}", "⚠️")
            return False

    def process(self, frame: np.ndarray) -> bool:
        if self._model is None:
            return False
        return self._model.process(frame.tolist()) >= 0

    def close(self):
        if self._model is None:
            return
        with contextlib.suppress(Exception):
            self._model.delete()
        self._model = None
