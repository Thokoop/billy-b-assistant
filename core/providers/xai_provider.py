import json
from typing import Any, Optional

from ..realtime_ai_provider import RealtimeAIProvider


class XAIProvider(RealtimeAIProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "grok-voice-latest",
        voice: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model.strip() or "grok-voice-latest"
        self.voice = self._normalize_voice(voice) if voice else self.default_voice

    @property
    def default_voice(self) -> str:
        return "rex"

    def _get_websocket_uri(self) -> str:
        uri = f"wss://api.x.ai/v1/realtime?model={self.model}"
        if self.model in {"grok-voice-latest", "grok-voice-think-fast-1.0"}:
            uri += "&reasoning.effort=none"
        return uri

    def get_supported_voices(self) -> list[str]:
        return ["ara", "rex", "sal", "eve", "leo"]

    def _normalize_voice(self, voice: Optional[str]) -> str:
        candidate = str(voice or "").strip().lower()
        return (
            candidate
            if candidate in self.get_supported_voices()
            else self.default_voice
        )

    def get_provider_name(self) -> str:
        return "xai"

    async def generate_audio_clip(
        self,
        prompt: str,
        voice: Optional[str] = None,
        instructions: Optional[str] = None,
        **kwargs,
    ) -> bytes:
        voice = self.default_voice if voice is None else self._normalize_voice(voice)

        # Reserve kwargs for future use
        _ = kwargs

        ws = await self._connect_websocket()
        async with ws:
            # Send session update
            session_instructions = "IMPORTANT: Always respond by speaking the exact user text out loud. Do not add, change or rephrase anything!"
            if instructions:
                session_instructions += "\n\n" + instructions

            await ws.send(
                json.dumps({
                    "type": "session.update",
                    "session": {
                        "voice": voice,
                        "instructions": session_instructions,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                            },
                        },
                    },
                })
            )

            # Send conversation item
            await ws.send(
                json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": prompt,
                            }
                        ],
                    },
                })
            )

            # Create response
            await ws.send(
                json.dumps({"type": "response.create", "response": ["text", "audio"]})
            )

            # Collect audio
            audio_bytes = await self._collect_audio_response(ws)

            if not audio_bytes:
                raise RuntimeError("No audio data received from XAI.")

            return audio_bytes

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    def _normalize_tools(self, tools: list[dict]) -> list[dict]:
        """Convert mixed/OpenAI-style tool shapes into xAI Voice Agent tool schema."""
        normalized_tools: list[dict] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue

            tool_type = tool.get("type")
            if tool_type != "function":
                normalized_tools.append(tool)
                continue

            function_spec = (
                tool.get("function") if isinstance(tool.get("function"), dict) else tool
            )
            name = function_spec.get("name")
            if not name:
                continue

            normalized_tool = {
                "type": "function",
                "name": name,
                "description": function_spec.get("description", ""),
                "parameters": function_spec.get(
                    "parameters",
                    {"type": "object", "properties": {}, "additionalProperties": False},
                ),
            }
            normalized_tools.append(normalized_tool)

        return normalized_tools

    def _get_initial_session_config(
        self, instructions: str, tools: list[dict], **kwargs
    ) -> dict[str, Any]:
        server_vad_params = dict(kwargs.get("server_vad_params", {}))
        text_only_mode = bool(kwargs.get("text_only_mode", False))
        requested_voice = kwargs.get("voice", self.default_voice)
        voice = self._normalize_voice(requested_voice)

        turn_detection = {"type": "server_vad"}
        for key in (
            "threshold",
            "silence_duration_ms",
            "prefix_padding_ms",
            "idle_timeout_ms",
        ):
            if key in server_vad_params:
                turn_detection[key] = server_vad_params[key]

        session_config = {
            "voice": voice,
            "instructions": instructions,
            "turn_detection": turn_detection,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}}
                if not text_only_mode
                else {},
            },
        }

        if tools:
            session_config["tools"] = self._normalize_tools(tools)

        return {
            "type": "session.update",
            "session": session_config,
        }

    # Abstract method implementations
    async def connect(self, instructions: str, tools: list[dict[str, Any]], **kwargs):
        """Connect to the provider's websocket, send initial config, and return the connection"""
        ws = await self._connect_websocket()
        config = self._get_initial_session_config(instructions, tools, **kwargs)
        await ws.send(json.dumps(config))
        return ws

    async def send_message(self, ws, payload: dict[str, Any]):
        """Send a JSON payload over the websocket"""
        await ws.send(json.dumps(payload))

    def get_provider_tools(self) -> list[dict]:
        # Keep xAI provider-side tool payload empty until the voice-agent flow is stable.
        return []
