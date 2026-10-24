"""Persona management for Billy session."""

from datetime import datetime

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

    async def handle_switch_persona(self, args: dict):
        """Handle persona switching mid-session."""
        persona_name = args.get("persona", "")
        reason = args.get("reason", "")

        logger.verbose(
            f"switch_persona: persona='{persona_name}', reason='{reason}'", "🔧"
        )

        if not persona_name:
            return

        try:
            available_personas = persona_manager.get_available_personas()
            persona_names = [p["name"] for p in available_personas]

            if persona_name not in persona_names:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": f"Sorry, I don't have a '{persona_name}' persona. "
                                f"Available: {', '.join(persona_names)}",
                            }
                        ],
                    },
                })
                return

            persona_manager.switch_persona(persona_name)
            await self._update_session_with_persona()
            await self._notify_persona_change(persona_name)

            persona_data = persona_manager.get_current_persona_data()
            persona_desc = (
                persona_data.get("meta", {}).get("description", persona_name)
                if persona_data
                else persona_name
            )

            message = (
                f"Right then! Switching to {persona_desc} mode. {reason}"
                if reason
                else f"Alright, switching to {persona_desc} mode now!"
            )

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message}],
                },
            })

            logger.info(f"Switched to persona: {persona_name}", "🎭")

        except Exception as e:
            logger.warning(f"Failed to switch persona: {e}")
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Sorry, couldn't switch personas right now!",
                        }
                    ],
                },
            })

    async def handle_manage_profile(self, args: dict):
        """Handle profile management (persona switching)."""
        action = args.get("action", "")

        if action == "switch_persona":
            from ..profile_manager import user_manager

            current_user = user_manager.get_current_user()
            if not current_user:
                return

            new_persona = args.get("preferred_persona", "default")

            available_personas = persona_manager.get_available_personas()
            persona_names = [p["name"] for p in available_personas]
            if new_persona not in persona_names:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": f"Sorry, I don't have a '{new_persona}' persona. "
                                f"Available: {', '.join(persona_names)}",
                            }
                        ],
                    },
                })
                return

            voice_changed = self._check_voice_change(new_persona)
            current_user.set_preferred_persona(new_persona)
            persona_manager.switch_persona(new_persona)
            await self._update_session_with_persona()
            await self._notify_persona_change(new_persona)
            if voice_changed:
                logger.info(
                    f"Applied voice change for persona '{new_persona}' via session.update",
                    "🎭",
                )

            persona_data = persona_manager.get_current_persona_data()
            persona_desc = (
                persona_data.get("meta", {}).get("description", new_persona)
                if persona_data
                else new_persona
            )

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Switched to {persona_desc} mode for you!",
                        }
                    ],
                },
            })

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

    async def _notify_persona_change(self, persona_name: str):
        """Notify frontend of persona change."""
        try:
            await self.session._ws_send_json({
                "type": "persona_changed",
                "persona": persona_name,
                "timestamp": datetime.now().isoformat(),
            })
            logger.info(f"Notified frontend: {persona_name}", "🎭")
        except Exception as e:
            logger.warning(f"Failed to notify frontend: {e}")

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
