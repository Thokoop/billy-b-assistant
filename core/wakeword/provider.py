from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class WakeWordBackend(ABC):
    """Runtime backend contract for local wake-word engines."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        pass

    @property
    @abstractmethod
    def keyword_label(self) -> str:
        pass

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        pass

    @property
    @abstractmethod
    def frame_length(self) -> int:
        pass

    @abstractmethod
    def initialize(self) -> bool:
        pass

    @abstractmethod
    def process(self, frame: np.ndarray) -> bool:
        pass

    @abstractmethod
    def close(self):
        pass


class WakeWordProviderRegistry:
    """Registry for wake-word backend classes."""

    def __init__(self):
        self.providers: dict[str, type[WakeWordBackend]] = {}
        self.default_provider: str | None = None

    def register_provider(self, name: str, provider_cls: type[WakeWordBackend]):
        key = (name or "").strip().lower()
        if not key:
            raise ValueError("Wake-word provider name cannot be empty")
        self.providers[key] = provider_cls
        if self.default_provider is None:
            self.default_provider = key

    def create_provider(self, name: str | None = None, **kwargs) -> WakeWordBackend:
        key = (name or self.default_provider or "").strip().lower()
        if key not in self.providers:
            raise ValueError(f"Wake-word provider '{key}' not found")
        return self.providers[key](**kwargs)

    def get_available_providers(self) -> list[str]:
        return list(self.providers.keys())

    def set_default_provider(self, name: str):
        key = (name or "").strip().lower()
        if key not in self.providers:
            raise ValueError(f"Wake-word provider '{key}' not found")
        self.default_provider = key


wakeword_provider_registry = WakeWordProviderRegistry()
