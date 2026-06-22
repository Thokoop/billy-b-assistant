from __future__ import annotations

import contextlib
from pathlib import Path

import numpy as np

from ..logger import logger
from ..wakeword_provider import WakeWordBackend
from .porcupine_wakeword_provider import _resolve_keyword_path


class OpenWakeWordBackend(WakeWordBackend):
    def __init__(
        self,
        *,
        root_dir: str,
        openwakeword_model_path: str,
        openwakeword_threshold: float,
        openwakeword_melspec_model_path: str = "",
        openwakeword_embedding_model_path: str = "",
        **_,
    ):
        self.root_dir = root_dir
        self.model_path_raw = openwakeword_model_path.strip()
        self.melspec_model_path_raw = openwakeword_melspec_model_path.strip()
        self.embedding_model_path_raw = openwakeword_embedding_model_path.strip()
        raw_threshold = float(openwakeword_threshold)
        self.threshold = max(0.0, min(1.0, raw_threshold))
        if self.threshold != raw_threshold:
            logger.warning(
                f"openWakeWord threshold {raw_threshold} is out of range; clamped to {self.threshold}.",
                "⚠️",
            )

        self._model = None
        self._keyword_label = ""

    @property
    def backend_name(self) -> str:
        return "openwakeword"

    @property
    def keyword_label(self) -> str:
        return self._keyword_label

    @property
    def sample_rate(self) -> int:
        return 16000

    @property
    def frame_length(self) -> int:
        return 1280

    def _resolve_optional_path(self, value: str) -> Path | None:
        if not value:
            return None
        path = _resolve_keyword_path(self.root_dir, value)
        return path if path.exists() else None

    def _resolve_support_model_paths(
        self,
        *,
        expected_suffix: str,
        package_defaults: tuple[str | None, str | None] = (None, None),
    ) -> tuple[Path | None, Path | None]:
        explicit_melspec = self._resolve_optional_path(self.melspec_model_path_raw)
        explicit_embedding = self._resolve_optional_path(self.embedding_model_path_raw)
        if explicit_melspec and explicit_melspec.suffix.lower() != expected_suffix:
            logger.warning(
                f"Ignoring openWakeWord melspectrogram support model with unexpected type: {explicit_melspec.name}",
                "⚠️",
            )
            explicit_melspec = None
        if explicit_embedding and explicit_embedding.suffix.lower() != expected_suffix:
            logger.warning(
                f"Ignoring openWakeWord embedding support model with unexpected type: {explicit_embedding.name}",
                "⚠️",
            )
            explicit_embedding = None
        if explicit_melspec and explicit_embedding:
            return explicit_melspec, explicit_embedding

        candidate_dirs = [
            Path(self.root_dir) / "wakewords" / "openwakeword",
            Path(self.root_dir) / "wakewords",
        ]
        for base in candidate_dirs:
            melspec = base / f"melspectrogram{expected_suffix}"
            embedding = base / f"embedding_model{expected_suffix}"
            if melspec.exists() and embedding.exists():
                return melspec, embedding

        default_melspec, default_embedding = package_defaults
        if default_melspec and default_embedding:
            melspec = Path(default_melspec)
            embedding = Path(default_embedding)
            if melspec.exists() and embedding.exists():
                return melspec, embedding

        return explicit_melspec, explicit_embedding

    def initialize(self) -> bool:
        if not self.model_path_raw:
            logger.warning(
                "WAKE_WORD_OPENWAKEWORD_MODEL_PATH is empty; set it to your .onnx model.",
                "⚠️",
            )
            return False

        model_path = _resolve_keyword_path(self.root_dir, self.model_path_raw)
        if not model_path.exists():
            logger.warning(f"openWakeWord model file does not exist: {model_path}", "⚠️")
            return False
        if model_path.suffix.lower() != ".onnx":
            logger.warning(
                f"openWakeWord requires an .onnx model file, got: {model_path.name}",
                "⚠️",
            )
            return False
        self._keyword_label = model_path.stem

        try:
            import openwakeword
            from openwakeword.model import Model
        except ImportError:
            logger.warning(
                "openwakeword is not installed. Install requirements to enable wake-word.",
                "⚠️",
            )
            return False

        feature_models = getattr(openwakeword, "FEATURE_MODELS", {})
        tflite_defaults = (
            (feature_models.get("melspectrogram") or {}).get("model_path"),
            (feature_models.get("embedding") or {}).get("model_path"),
        )
        onnx_defaults = tuple(
            path.replace(".tflite", ".onnx") if isinstance(path, str) else None
            for path in tflite_defaults
        )

        tflite_support = self._resolve_support_model_paths(
            expected_suffix=".tflite",
            package_defaults=tflite_defaults,
        )
        onnx_support = self._resolve_support_model_paths(
            expected_suffix=".onnx",
            package_defaults=onnx_defaults,
        )

        model_kwargs = {
            "wakeword_models": [str(model_path)],
            "inference_framework": "onnx",
        }

        if all(onnx_support):
            model_kwargs["melspec_model_path"] = str(onnx_support[0])
            model_kwargs["embedding_model_path"] = str(onnx_support[1])
            logger.info(
                "Using local/package ONNX support models for openWakeWord preprocessing.",
                "🧠",
            )
        elif all(tflite_support):
            model_kwargs["melspec_model_path"] = str(tflite_support[0])
            model_kwargs["embedding_model_path"] = str(tflite_support[1])
            logger.info(
                "Using local/package TFLite support models for openWakeWord preprocessing.",
                "🧠",
            )

        try:
            self._model = Model(**model_kwargs)
            return True
        except Exception as e:
            error_text = str(e)
            if any(
                marker in error_text
                for marker in (
                    "melspectrogram.tflite",
                    "embedding_model.tflite",
                    "melspectrogram.onnx",
                    "embedding_model.onnx",
                )
            ):
                logger.warning(
                    "openWakeWord support models are missing. Add `melspectrogram.onnx` and "
                    "`embedding_model.onnx` to `wakewords/openwakeword/` or `wakewords/`.",
                    "⚠️",
                )
            logger.warning(f"Failed to initialize openWakeWord: {e}", "⚠️")
            return False

    def process(self, frame: np.ndarray) -> bool:
        if self._model is None:
            return False

        try:
            predictions = self._model.predict(frame)
        except Exception as e:
            logger.warning(f"openWakeWord inference failed: {e}", "⚠️")
            return False

        score = 0.0
        if isinstance(predictions, dict):
            score = max((float(value) for value in predictions.values()), default=0.0)

        return score >= self.threshold

    def close(self):
        if self._model is None:
            return
        with contextlib.suppress(Exception):
            self._model.reset()
        self._model = None
