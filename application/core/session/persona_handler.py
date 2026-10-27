"""Persona management for Billy session."""

import json

from ..config import TEXT_ONLY_MODE
from ..logger import logger
from ..persona_manager import persona_manager


class PersonaHandler:
    """Handles persona switching and management."""

    def __init__(self, session):
        self.session = session

    async def reload_persona_from_profile(self):
        """Reload persona from current user's profile."""
        try:
            import os

            from dotenv import load_dotenv

            from ..config import ENV_PATH
            from ..profile_manager import user_manager

            load_dotenv(ENV_PATH, override=True)
            current_user_env = (
                os.getenv("CURRENT_USER", "").strip().strip("'\"").lower()
            )

            if current_user_env == "guest" or not current_user_env:
                guest_profile = user_manager.identify_user("guest", "high")
                if guest_profile:
                    preferred_persona = guest_profile.data["USER_INFO"].get(
                        "preferred_persona", "default"
                    )
                    persona_manager.switch_persona(preferred_persona)
                    logger.info(f"🎭 Reloaded guest persona: {preferred_persona}", "🎭")
            else:
                current_user = user_manager.get_current_user()
                if current_user:
                    preferred_persona = current_user.data["USER_INFO"].get(
                        "preferred_persona", "default"
                    )
                    persona_manager.switch_persona(preferred_persona)
                    logger.info(f"🎭 Reloaded user persona: {preferred_persona}", "🎭")
        except Exception as e:
            logger.warning(f"Failed to reload persona: {e}", "⚠️")

    async def _finish_tool(self, result: dict, call_id: str | None):
        """Report actual outcomes instead of inferring success from current state."""
        if call_id:
            item = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            }
        else:
            item = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": result["message"]}],
            }
        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": item,
        })
        if call_id:
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})

    async def handle_switch_persona(self, args: dict, call_id: str | None = None):
        """Switch the current persona, reporting failures to the calling model."""
        persona_name = args.get("persona", "")
        try:
            names = [p["name"] for p in persona_manager.get_available_personas()]
            if persona_name not in names:
                result = {
                    "ok": False,
                    "message": f"Persona unavailable: {persona_name}",
                    "available_personas": names,
                }
            elif not persona_manager.switch_persona(persona_name):
                result = {"ok": False, "message": "Could not switch persona."}
            else:
                await self._update_session_with_persona()
                result = {
                    "ok": True,
                    "persona": persona_name,
                    "message": f"Switched to persona: {persona_name}",
                }
                logger.info(result["message"], "🎭")
        except Exception as exc:
            logger.warning(f"Failed to switch persona: {exc}")
            result = {
                "ok": False,
                "message": "Could not complete the persona switch. Check the current persona before retrying.",
            }
        await self._finish_tool(result, call_id)

    async def handle_manage_profile(self, args: dict, call_id: str | None = None):
        """Persist a preferred persona and report the actual save outcome."""
        from ..profile_manager import user_manager

        current_user = user_manager.get_current_user()
        new_persona = args.get("preferred_persona", "default")
        try:
            names = [p["name"] for p in persona_manager.get_available_personas()]
            if args.get("action") != "switch_persona":
                result = {"ok": False, "message": "Unsupported profile action."}
            elif not current_user:
                result = {
                    "ok": False,
                    "message": "No current user profile. Identify the user first.",
                }
            elif new_persona not in names:
                result = {
                    "ok": False,
                    "message": f"Persona unavailable: {new_persona}",
                    "available_personas": names,
                }
            else:
                current_user.set_preferred_persona(new_persona)
                if not persona_manager.switch_persona(new_persona):
                    result = {
                        "ok": False,
                        "message": "Preference saved, but the current persona could not be switched.",
                    }
                else:
                    await self._update_session_with_persona()
                    result = {
                        "ok": True,
                        "persona": new_persona,
                        "user": current_user.name,
                        "message": f"Saved preferred persona and switched to {new_persona}.",
                    }
        except Exception as exc:
            logger.warning(f"Failed to update profile persona: {exc}")
            result = {
                "ok": False,
                "message": "Could not complete the profile change. Check the profile before retrying.",
            }
        await self._finish_tool(result, call_id)

    async def _update_session_with_persona(self):
        """Update session with new persona context."""
        if not self.session.ws:
            return

        try:
            from ..session_manager import get_instructions_with_user_context

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
                        "voice": persona_manager.get_current_persona_voice(),
                    }
                }

            await self.session._ws_send_json(session_update)
            self.session.mark_mood_instructions_current()
            logger.info("Updated session with persona context", "🎭")
        except Exception as e:
            logger.warning(f"Failed to update session: {e}")

    def _check_voice_change(self, new_persona: str) -> bool:
        """Check if persona has different voice."""
        try:
            current_voice = persona_manager.get_current_persona_voice()
            new_voice = persona_manager.get_persona_voice(new_persona)
            return current_voice != new_voice
        except Exception as e:
            logger.warning(f"Failed to check voice change: {e}")
            return False

    async def _restart_for_voice_change(self, new_persona: str):
        """Restart session for voice change."""
        try:
            logger.info(f"Voice changed, restarting for {new_persona}", "🔄")

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Switching to {new_persona} persona. "
                            "This requires a quick restart to change my voice...",
                        }
                    ],
                },
            })

            await self.session._ws_send_json({"type": "session.close"})
            import asyncio

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Failed to restart for voice change: {e}")
