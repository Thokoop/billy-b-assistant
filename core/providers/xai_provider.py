import json
from typing import Any

from ..realtime_ai_provider import RealtimeAIProvider


class XAIProvider(RealtimeAIProvider):
    def __init__(
        self,
        api_key: str,
        voice: str | None = None,
    ):
        self.api_key = api_key
        if voice and voice in self.get_supported_voices():
            self.voice = voice
        else:
            self.voice = self.default_voice

    @property
    def default_voice(self) -> str:
        return "Rex"

    def _get_websocket_uri(self) -> str:
        return "wss://api.x.ai/v1/realtime"

    def get_supported_voices(self) -> list[str]:
        return ["Ara", "Rex", "Sal", "Eve", "Leo"]

    def get_provider_name(self) -> str:
        return "xai"

    async def generate_audio_clip(
        self,
        prompt: str,
        voice: str | None = None,
        instructions: str | None = None,
        **kwargs,
    ) -> bytes:
        if voice is None:
            voice = self.default_voice

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
                                "text": "Repeat this literal message:" + prompt,
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

    def _get_initial_session_config(
        self, instructions: str, tools: list[dict], **kwargs
    ) -> dict[str, Any]:
        server_vad_params = kwargs.get("server_vad_params", {})
        text_only_mode = kwargs.get("text_only_mode", False)
        requested_voice = kwargs.get("voice", self.default_voice)
        # Validate voice is supported, otherwise use default
        voice = (
            requested_voice
            if requested_voice in self.get_supported_voices()
            else self.default_voice
        )
        interrupt_response = bool(kwargs.get("interrupt_response", False))

        session_config = {
            "voice": voice,
            "instructions": instructions,
            "turn_detection": {
                "type": "server_vad",
                **server_vad_params,
                "create_response": True,
                "interrupt_response": interrupt_response,
            },
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 24000}},
                "output": {"format": {"type": "audio/pcm", "rate": 24000}}
                if not text_only_mode
                else {},
            },
        }

        if tools:
            session_config["tools"] = tools

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
        # XAI server-side tools
        return [
            {
                "type": "web_search",
            },
            {
                "type": "x_search",
                "allowed_x_handles": ["elonmusk", "xai"],
            },
        ]
