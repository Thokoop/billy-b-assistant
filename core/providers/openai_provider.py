import json
from typing import Any, Optional

from ..realtime_ai_provider import RealtimeAIProvider


class OpenAIProvider(RealtimeAIProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-realtime-mini",
        voice: Optional[str] = None,
    ):
        self.api_key = api_key
        self.model = model
        if voice and voice in self.get_supported_voices():
            self.voice = voice
        else:
            self.voice = self.default_voice

    @property
    def default_voice(self) -> str:
        return "ballad"

    async def generate_audio_clip(
        self,
        prompt: str,
        voice: Optional[str] = None,
        instructions: Optional[str] = None,
        **kwargs,
    ) -> bytes:
        if voice is None:
            voice = self.default_voice

        # kwargs reserved for future use
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
                        "type": "realtime",
                        "instructions": session_instructions,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "voice": voice,
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
            await ws.send(json.dumps({"type": "response.create"}))

            # Collect audio
            audio_bytes = await self._collect_audio_response(ws)

            if not audio_bytes:
                raise RuntimeError("No audio data received from OpenAI.")

            return audio_bytes

    def get_supported_voices(self) -> list[str]:
        return [
            "alloy",
            "ash",
            "ballad",
            "echo",
            "sage",
            "shimmer",
            "verse",
            "marin",
            "cedar",
        ]

    def get_provider_name(self) -> str:
        return "openai"

    # Private methods
    def _get_websocket_uri(self) -> str:
        return f"wss://api.openai.com/v1/realtime?model={self.model}"

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    def _get_initial_session_config(
        self, instructions: str, tools: list[dict], **kwargs
    ) -> dict[str, Any]:
        server_vad_params = dict(kwargs.get("server_vad_params", {}))
        text_only_mode = bool(kwargs.get("text_only_mode", False))
        voice = str(kwargs.get("voice", self.default_voice))
        interrupt_response = bool(kwargs.get("interrupt_response", False))

        audio_config = {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "turn_detection": {
                    "type": "server_vad",
                    **server_vad_params,
                    "create_response": True,
                    "interrupt_response": interrupt_response,
                },
            },
        }

        if not text_only_mode:
            audio_config["output"] = {
                "format": {"type": "audio/pcm", "rate": 24000},
                "voice": voice,
                "speed": 1.0,
            }

        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "tools": tools,
                "audio": audio_config,
            },
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
        # OpenAI doesn't have provider-specific tools beyond the base ones
        return []
