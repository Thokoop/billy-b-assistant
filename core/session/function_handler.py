"""Function call handler for routing AI function calls to implementations."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..config import PERSONALITY, TEXT_ONLY_MODE
from ..ha import send_conversation_prompt
from ..knowledge_manager import knowledge_manager
from ..logger import logger
from ..mood import MODEL_REPORTED_MOOD_EVENTS, mood_manager
from ..news_digest import get_news_digest
from ..persona import update_persona_ini
from ..persona_manager import persona_manager
from ..vision import describe_scene


class FunctionHandler:
    """Handles routing and execution of function calls."""

    def __init__(self, session):
        self.session = session

    @staticmethod
    def _conversation_state_tool() -> dict[str, Any]:
        return {
            "type": "function",
            "name": "conversation_state",
            "description": "Call this internal function after speaking to indicate whether a follow-up is expected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expects_follow_up": {"type": "boolean"},
                    "suggested_prompt": {"type": "string"},
                    "reason": {"type": "string"},
                    "mood_event": {
                        "type": "string",
                        "enum": list(MODEL_REPORTED_MOOD_EVENTS),
                    },
                    "mood_event_reason": {"type": "string"},
                },
                "required": ["expects_follow_up"],
            },
        }

    async def handle(
        self, function_name: str, raw_args: str | None, call_id: str | None = None
    ):
        """Route function call to appropriate handler."""
        handlers = {
            "conversation_state": self._handle_conversation_state,
            "update_personality": self._handle_update_personality,
            "play_song": self._handle_play_song,
            "smart_home_command": self._handle_smart_home_command,
            "get_mood": self._handle_get_mood,
            "set_mood": self._handle_set_mood,
            "identify_user": self._handle_identify_user,
            "store_memory": self._handle_store_memory,
            "manage_profile": self._handle_manage_profile,
            "switch_persona": self._handle_switch_persona,
            "get_news_digest": self._handle_get_news_digest,
            "describe_scene": self._handle_describe_scene,
            "search_local_knowledge": self._handle_search_local_knowledge,
        }

        handler = handlers.get(function_name)
        if not handler:
            logger.warning(f"No handler for function: {function_name}")
            return

        try:
            if logger.get_level().name == "VERBOSE":
                logger.verbose(
                    f"tool_call:start name={function_name} call_id={call_id} raw_args={raw_args!r}",
                    "🧰",
                )
            started_at = time.perf_counter()
            await handler(raw_args, call_id)
            if logger.get_level().name == "VERBOSE":
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                logger.verbose(
                    f"tool_call:done name={function_name} call_id={call_id} elapsed_ms={elapsed_ms:.1f}",
                    "🧰",
                )
        except Exception as e:
            logger.error(f"Function {function_name} failed: {e}")

    def _parse_json_args(self, raw_args: str | None, tool_name: str) -> dict:
        """Parse JSON arguments with fallback for malformed JSON."""
        raw_args = raw_args or "{}"
        try:
            return json.loads(raw_args)
        except Exception as e:
            try:
                import re

                fixed_json = raw_args
                fixed_json = re.sub(
                    r'{"([^"]*):([^"}]*)([}])', r'{"\\1": \\2\\3', fixed_json
                )
                fixed_json = re.sub(r':(true|false)([},])', r': \\1\\2', fixed_json)
                args = json.loads(fixed_json)
                logger.info(
                    f"{tool_name}: fixed malformed JSON | original={raw_args!r} | fixed={fixed_json!r}",
                    "🔧",
                )
                return args
            except Exception as fix_e:
                logger.warning(
                    f"{tool_name}: failed to parse arguments: {e} | raw={raw_args!r} | fix also failed: {fix_e}"
                )
                return {}

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        """Safely coerce common JSON-ish bool representations."""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                return True
            if normalized in {"false", "0", "no", "n", "off", ""}:
                return False
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    async def _handle_conversation_state(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle conversation state (internal)."""
        args = self._parse_json_args(raw_args, "conversation_state")
        self.session.state.follow_up_expected = self._coerce_bool(
            args.get("expects_follow_up", False), default=False
        )
        self.session.state.follow_up_prompt = args.get("suggested_prompt") or None
        self.session.state._saw_follow_up_call = True
        mood_event = str(args.get("mood_event") or "none").strip()
        if mood_event != "none":
            result = mood_manager.apply_model_event(mood_event)
            if result.get("ok"):
                logger.verbose(
                    f"conversation_state mood_event={mood_event} reason={args.get('mood_event_reason')!r}",
                    "🎭",
                )
            else:
                logger.warning(
                    f"Ignoring unsupported conversation_state mood_event={mood_event!r}",
                    "🎭",
                )

        if logger.get_level().name == "VERBOSE":
            logger.verbose(
                f"conversation_state | expects_follow_up={self.session.state.follow_up_expected}"
                f" | suggested_prompt={self.session.state.follow_up_prompt!r}"
                f" | reason={args.get('reason')!r}"
                f" | mood_event={mood_event!r}",
                "🧭",
            )

    async def _handle_update_personality(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle personality update."""
        args = self._parse_json_args(raw_args, "update_personality")
        changes = []

        current_persona = persona_manager.current_persona
        if current_persona == "default":
            persona_file_path = "persona.ini"
        else:
            from pathlib import Path

            personas_dir = Path("personas")
            persona_file_path = personas_dir / current_persona / "persona.ini"
            if not persona_file_path.exists():
                persona_file_path = personas_dir / f"{current_persona}.ini"

        logger.info(
            f"Updating personality for persona: {current_persona}, file: {persona_file_path}",
            "🎛️",
        )

        level_to_value = {'min': 7, 'low': 24, 'med': 49, 'high': 74, 'max': 92}

        for trait, val in args.items():
            if hasattr(PERSONALITY, trait):
                if isinstance(val, int):
                    numeric_val = val
                elif isinstance(val, str) and val.lower() in level_to_value:
                    numeric_val = level_to_value[val.lower()]
                else:
                    continue

                setattr(PERSONALITY, trait, numeric_val)
                update_persona_ini(trait, numeric_val, str(persona_file_path))
                changes.append((trait, numeric_val))

        if changes:
            print("\n🎛️ Personality updated via function_call:")
            for trait, val in changes:
                level = PERSONALITY._bucket(val)
                print(f"  - {trait.capitalize()}: {val}% ({level.upper()})")

            self.session.state.full_response_text = ""
            self.session.last_activity[0] = time.time()

            if call_id:
                changes_summary = ", ".join([
                    f"{trait}={PERSONALITY._bucket(val).upper()}"
                    for trait, val in changes
                ])
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "status": "success",
                            "changes": changes_summary,
                        }),
                    },
                })
                await asyncio.sleep(0.1)

            confirmation_text = " ".join([
                f"Okay, {trait} is now set to {PERSONALITY._bucket(val).upper()}."
                for trait, val in changes
            ])
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": confirmation_text}],
                },
            })
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})

    async def _handle_play_song(self, raw_args: str | None, call_id: str | None = None):
        """Handle song playback."""
        from .. import audio

        mood_manager.apply_event("song_requested")
        args = self._parse_json_args(raw_args, "play_song")
        song_name = args.get("song")
        if song_name:
            logger.info(f"Assistant requested to play song: {song_name}", "🎵")
            await self.session.stop_session()
            await asyncio.sleep(1.0)
            await audio.play_song(
                song_name, interrupt_event=self.session.interrupt_event
            )

    async def _handle_get_mood(self, raw_args: str | None, call_id: str | None = None):
        """Handle current mood lookup."""
        _ = self._parse_json_args(raw_args, "get_mood")
        result = {"ok": True, "mood": mood_manager.snapshot()}
        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            })
            await asyncio.sleep(0.1)

        prompt = (
            "Tell the user Billy's current mood in one short spoken sentence. "
            f"Use this mood state: {json.dumps(result)}. "
            "Do not list raw numbers unless the user asked for details. "
            "Do not ask a follow-up question. After speaking, call conversation_state with expects_follow_up=false."
        )
        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    async def _handle_set_mood(self, raw_args: str | None, call_id: str | None = None):
        """Handle temporary mood changes."""
        args = self._parse_json_args(raw_args, "set_mood")
        mood = str(args.get("mood") or "").strip().lower()
        result = mood_manager.set_mood(mood, event="user_set_mood")

        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            })
            await asyncio.sleep(0.1)

        if result.get("ok"):
            prompt = (
                "Confirm in one short spoken sentence that Billy's temporary mood "
                f"is now {result['mood']['label']}. Do not imply the persona changed. "
                "Do not ask a follow-up question. After speaking, call conversation_state with expects_follow_up=false."
            )
        else:
            prompt = (
                f"The requested mood change failed: {json.dumps(result)}. "
                "Briefly say which moods are available. "
                "Do not ask a follow-up question. After speaking, call conversation_state with expects_follow_up=false."
            )

        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    async def _handle_smart_home_command(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle Home Assistant command."""
        args = self._parse_json_args(raw_args, "smart_home_command")
        prompt = args.get("prompt")
        if not prompt:
            return

        logger.info(f"Sending to Home Assistant Conversation API: {prompt}", "🏠")
        ha_response = await send_conversation_prompt(prompt)
        speech_text = None

        if isinstance(ha_response, dict):
            speech_text = ha_response.get("speech", {}).get("plain", {}).get("speech")
            if speech_text:
                logger.verbose(f"HA debug: {ha_response.get('data')}", "🔍")
                print(f"\n📣 Home Assistant says: {speech_text}")

            if call_id:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "status": "success",
                            "response": speech_text,
                        }),
                    },
                })
                await asyncio.sleep(0.1)

            confirmation_prompt = f"Home Assistant completed the task: '{speech_text}'. Confirm this out loud to the user."
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": confirmation_prompt}],
                },
            })
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})
        else:
            logger.warning(f"Failed to parse HA response: {ha_response}")
            if call_id:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "status": "error",
                            "message": "Home Assistant didn't understand the request",
                        }),
                    },
                })
                await asyncio.sleep(0.1)

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Home Assistant didn't understand the request.",
                        }
                    ],
                },
            })
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})

    async def _handle_identify_user(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle user identification."""
        args = self._parse_json_args(raw_args, "identify_user")
        await self.session.user_handler.handle_identify_user(args, call_id)

    async def _handle_store_memory(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle memory storage."""
        args = self._parse_json_args(raw_args, "store_memory")
        await self.session.user_handler.handle_store_memory(args, call_id)

    async def _handle_manage_profile(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle profile management."""
        args = self._parse_json_args(raw_args, "manage_profile")
        await self.session.persona_handler.handle_manage_profile(args)

    async def _handle_switch_persona(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle persona switching mid-session."""
        args = self._parse_json_args(raw_args, "switch_persona")
        await self.session.persona_handler.handle_switch_persona(args)

    async def _handle_get_news_digest(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle location-aware news/weather/sports digest retrieval."""
        args = self._parse_json_args(raw_args, "get_news_digest")
        if logger.get_level().name == "VERBOSE":
            logger.verbose(f"get_news_digest:args {args}", "🗞️")
        result = await asyncio.to_thread(get_news_digest, args)
        if logger.get_level().name == "VERBOSE":
            logger.verbose(
                "get_news_digest:result "
                f"ok={result.get('ok')} "
                f"category={result.get('category')} "
                f"source={result.get('source')} "
                f"items={len(result.get('items') or [])} "
                f"summary={result.get('summary')!r}",
                "🗞️",
            )

        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            })
            await asyncio.sleep(0.1)

        category = str(result.get("category", "news")).strip()
        if result.get("ok"):
            prompt = (
                f"Create a short spoken {category} briefing based on this tool result: "
                f"{json.dumps(result)}. Mention location/source when relevant and keep it under 4 sentences."
            )
        else:
            prompt = (
                f"The news tool failed with this result: {json.dumps(result)}. "
                "Apologize briefly and ask a concise follow-up question to refine location/topic."
            )

        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    async def _handle_describe_scene(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle live camera capture and inject image into the active Realtime session."""
        args = self._parse_json_args(raw_args, "describe_scene")
        if logger.get_level().name == "VERBOSE":
            logger.verbose(f"describe_scene:args {args}", "📷")

        provider_name = self.session.realtime_ai_provider.get_provider_name()
        if provider_name != "openai":
            result = {
                "ok": False,
                "summary": "Camera vision unavailable for this provider.",
                "error": f"Current provider '{provider_name}' does not support this Realtime image flow.",
            }
            if call_id:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    },
                })
                await asyncio.sleep(0.1)

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Camera vision is only available with OpenAI Realtime in this build. Apologize briefly and ask the user to switch provider.",
                        }
                    ],
                },
            })
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})
            return

        try:
            result = await asyncio.to_thread(describe_scene, args)
        except Exception as e:
            logger.warning(f"describe_scene failed: {e}")
            result = {
                "ok": False,
                "summary": "Camera scene check failed.",
                "error": str(e),
            }

        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "ok": bool(result.get("ok")),
                        "summary": result.get("summary", ""),
                        "error": result.get("error"),
                    }),
                },
            })
            await asyncio.sleep(0.1)

        if result.get("ok"):
            user_prompt = str(result.get("prompt") or "").strip()
            max_words = int(result.get("max_words") or 80)
            image_url = str(result.get("image_url") or "").strip()
            if not image_url:
                prompt = (
                    "Camera capture returned no image bytes. "
                    "Apologize briefly and ask the user to retry."
                )
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    },
                })
                self.session.state._triggered_new_response = True
                await self.session._ws_send_json({"type": "response.create"})
                return

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"{user_prompt} Keep the answer under {max_words} words. "
                                "Be concrete and avoid speculation. Use the attached image in this message. "
                                "Do not call describe_scene or any other tool again for this image."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": image_url,
                        },
                    ],
                },
            })
        else:
            prompt = (
                "You could not describe the camera scene. "
                f"Error details: {result.get('error', 'unknown error')}. "
                "Apologize briefly and ask the user to retry."
            )
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({
            "type": "response.create",
            "response": {
                "tools": [self._conversation_state_tool()],
                "tool_choice": "auto",
            },
        })

    async def _handle_search_local_knowledge(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle retrieval from Billy's uploaded local knowledge index."""
        args = self._parse_json_args(raw_args, "search_local_knowledge")
        query = str(args.get("query") or "").strip()
        top_k = int(args.get("top_k") or 3)

        result = await asyncio.to_thread(
            knowledge_manager.search,
            query,
            top_k=top_k,
        )

        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                },
            })
            await asyncio.sleep(0.1)

        if result.get("ok") and result.get("matches"):
            prompt = (
                "Answer the user's question using this local knowledge search result: "
                f"{json.dumps(result)}. Quote or paraphrase only what is supported by the snippets. "
                "Do not mention filenames, documents, PDFs, or internal knowledge sources unless the user explicitly asks where the information came from. "
                "If the snippets are incomplete, say so briefly."
            )
        elif result.get("ok"):
            prompt = (
                "The local knowledge search found no useful matches. "
                f"Search result: {json.dumps(result)}. "
                "Say you could not find that in uploaded knowledge and ask one concise follow-up question if helpful."
            )
        else:
            prompt = (
                "The local knowledge search failed. "
                f"Error result: {json.dumps(result)}. "
                "Apologize briefly and ask the user to try again."
            )

        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    # Helper method (kept for backward compatibility with update_personality handler)
    async def _update_session_with_user_context(self):
        """Update the session with current user context."""
        if not self.session.ws:
            return

        try:
            from ..session_manager import get_instructions_with_user_context

            session_voice = persona_manager.get_current_persona_voice()
            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": get_instructions_with_user_context(),
                },
            }

            if not TEXT_ONLY_MODE:
                session_update["session"]["audio"] = {
                    "output": {
                        "voice": session_voice,
                    }
                }

            await self.session._ws_send_json(session_update)
            logger.info(
                f"Updated session with user context (persona={persona_manager.current_persona}, voice={session_voice})",
                "👤",
            )
        except Exception as e:
            logger.warning(f"Failed to update session with user context: {e}")
