import asyncio
import base64
import json
from abc import ABC, abstractmethod
from functools import wraps
from typing import Any, Optional

import websockets.asyncio.client
import websockets.exceptions


CLIP_TIMEOUT_SECONDS = 90.0
MAX_CLIP_BYTES = 24000 * 2 * 120


def bounded_audio_generation(method):
    """Bound connect, setup, collection and teardown of a one-shot clip."""

    @wraps(method)
    async def bounded(*args, **kwargs):
        async with asyncio.timeout(CLIP_TIMEOUT_SECONDS):
            return await method(*args, **kwargs)

    return bounded


class RealtimeAIProvider(ABC):
    @abstractmethod
    async def generate_audio_clip(
        self,
        prompt: str,
        voice: Optional[str] = None,
        instructions: Optional[str] = None,
        **kwargs,
    ) -> bytes:
        """Generate audio clip from text prompt using specified voice or default"""
        pass

    async def generate_wakeup_clip(self, prompt, voice=None, instructions=None):
        """Render a wake-up sound; providers may offer a previewable recording."""
        return await self.generate_audio_clip(
            prompt, voice=voice, instructions=instructions, strict_literal=True
        )

    @abstractmethod
    def get_supported_voices(self) -> list[str]:
        """Return list of supported voice names"""
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return provider identifier"""
        pass

    @property
    @abstractmethod
    def default_voice(self) -> str:
        """Return the default voice for this provider"""
        pass

    @abstractmethod
    def get_provider_tools(self) -> list[dict]:
        """Return provider-specific tools (empty for OpenAI)"""
        pass

    @abstractmethod
    def _get_websocket_uri(self) -> str:
        """Return the WebSocket URI for the provider"""
        pass

    @abstractmethod
    def _get_headers(self) -> dict[str, str]:
        """Return headers for WebSocket connection"""
        pass

    async def _connect_websocket(self):
        """Connect to the provider's websocket and return the connection (without config)"""
        uri = self._get_websocket_uri()
        headers = self._get_headers()
        try:
            return await websockets.asyncio.client.connect(
                uri,
                additional_headers=headers,
                open_timeout=30,
                ping_interval=20,
                ping_timeout=60,
                close_timeout=2,
            )
        except websockets.exceptions.InvalidStatus as e:
            response = getattr(e, "response", None)
            body = ""
            if response is not None:
                raw_body = getattr(response, "body", b"") or b""
                if isinstance(raw_body, bytearray):
                    raw_body = bytes(raw_body)
                if isinstance(raw_body, bytes):
                    body = raw_body.decode("utf-8", errors="replace").strip()
                else:
                    body = str(raw_body).strip()

            if body:
                try:
                    payload = json.loads(body)
                    detail = payload.get("error") or payload.get("message") or body
                except json.JSONDecodeError:
                    detail = body
                raise RuntimeError(detail) from e
            raise

    @abstractmethod
    async def connect(
        self, instructions: str, tools: list[dict[str, Any]], **kwargs
    ) -> Any:
        """Connect to the provider's websocket, send initial config, and return the connection"""
        pass

    @abstractmethod
    async def send_message(self, ws, payload: dict[str, Any]):
        """Send a JSON payload over the websocket"""
        pass

    async def _collect_audio_response(self, ws):
        """Return only a completed clip; fail promptly on provider errors or EOF."""
        audio_bytes = bytearray()
        async with asyncio.timeout(CLIP_TIMEOUT_SECONDS):
            async for message in ws:
                data = json.loads(message)
                event = data.get("type") or ""
                if event == "error":
                    error = data.get("error") or {}
                    raise RuntimeError(
                        f"Audio generation failed: {error.get('message', error)}"
                    )
                if event in {
                    "response.output_audio",
                    "response.output_audio.delta",
                    "response.audio.delta",
                }:
                    encoded = data.get("audio") or data.get("delta")
                    if encoded:
                        audio_bytes.extend(base64.b64decode(encoded))
                        if len(audio_bytes) > MAX_CLIP_BYTES:
                            raise RuntimeError(
                                "Generated audio clip exceeded the size limit"
                            )
                elif event == "response.done":
                    response = data.get("response") or {}
                    status = response.get("status")
                    details = response.get("status_details") or {}
                    # Older compatible providers can omit status entirely.
                    if status not in (None, "completed") or details.get("error"):
                        raise RuntimeError(f"Audio response {status}: {details}")
                    if not audio_bytes:
                        raise RuntimeError("Audio response completed without audio")
                    return bytes(audio_bytes)
        raise RuntimeError("Connection closed before audio response completed")


class RealtimeAIProviderRegistry:
    def __init__(self):
        self.providers: dict[str, RealtimeAIProvider] = {}
        self.default_provider: Optional[str] = None

    def register_provider(self, provider: RealtimeAIProvider):
        """Register a realtime AI provider"""
        name = provider.get_provider_name()
        self.providers[name] = provider
        if self.default_provider is None:
            self.default_provider = name

    def get_provider(self, name: Optional[str] = None) -> RealtimeAIProvider:
        """Get a realtime AI provider by name, or default if none specified"""
        if name is None:
            name = self.default_provider
        if name not in self.providers:
            raise ValueError(f"Realtime AI provider '{name}' not found")
        return self.providers[name]

    def get_available_providers(self) -> list[str]:
        """Return list of available provider names"""
        return list(self.providers.keys())

    def set_default_provider(self, name: str):
        """Set the default realtime AI provider"""
        if name not in self.providers:
            raise ValueError(f"Realtime AI provider '{name}' not found")
        self.default_provider = name


# Global registry instance
voice_provider_registry = RealtimeAIProviderRegistry()
