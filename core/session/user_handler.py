"""User and profile management for Billy session."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

from ..config import TEXT_ONLY_MODE
from ..logger import logger
from ..persona_manager import persona_manager
from ..profile_manager import user_manager


class UserHandler:
    """Handles user identification, profile management, and greetings."""

    def __init__(self, session):
        self.session = session
        self._waiting_for_name_after_denial = False

    async def handle_identify_user(self, args: dict, call_id: str | None = None):
        """Handle user identification via tool calling."""
        name = args.get("name", "").strip().title()
        confidence = args.get("confidence", "medium")
        context = args.get("context", "")

        logger.verbose(
            f"🔧 identify_user: name='{name}', confidence='{confidence}', context='{context}'",
            "🔧",
        )

        if not name:
            return

        if confidence == "low":
            await self._ask_spelling_confirmation(name)
            return

        current_user = user_manager.get_current_user()
        if (
            current_user
            and context
            and ("not" in context.lower() or "am not" in context.lower())
        ):
            await self._handle_user_denial(current_user.name)
            return

        profile = user_manager.identify_user(name, confidence)
        if profile:
            self._waiting_for_name_after_denial = False
            await self.save_current_user_to_env(profile.name)
            await self.switch_to_user_persona(profile)
            await self.update_session_with_user_context()

            if context not in ["current user", "default user"]:
                # Mark that we're triggering a new response BEFORE sending greeting
                self.session.state._triggered_new_response = True
                await self.send_user_greeting(profile, call_id)
            else:
                logger.info(
                    f"Profile loaded for {profile.name} (auto-load, no greeting)", "👤"
                )
        elif self._waiting_for_name_after_denial:
            await self._fallback_to_guest()

    async def handle_store_memory(self, args: dict, call_id: str | None = None):
        """Handle memory storage via tool calling."""
        current_user = user_manager.get_current_user()
        if not current_user:
            logger.warning("store_memory: No current user", "🔧")
            return

        memory = args.get("memory", "")
        if isinstance(memory, dict):
            memory = memory.get("fact", str(memory))

        importance = args.get("importance", "medium")
        category = args.get("category", "fact")

        if not memory:
            return

        logger.verbose(
            f"store_memory: '{memory}', importance={importance}, category={category}",
            "🔧",
        )

        current_user.add_memory(memory, importance, category)
        await self.update_session_with_user_context()

        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"status": "success", "stored": memory}),
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
                            "text": f"[System: Memory stored. Briefly acknowledge storing '{memory}' and continue naturally.]",
                        }
                    ],
                },
            })
            self.session.state._triggered_new_response = True
            await self.session._ws_send_json({"type": "response.create"})

    async def auto_identify_default_user(self):
        """Automatically identify the current user if set."""
        try:
            from dotenv import load_dotenv

            from ..config import ENV_PATH

            load_dotenv(ENV_PATH, override=True)
            current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")
            default_user_env = os.getenv("DEFAULT_USER", "guest").strip().strip("'\"")
            current_user = user_manager.get_current_user()

            user_to_identify = (
                current_user_env
                if current_user_env and current_user_env.lower() != "guest"
                else default_user_env
            )

            logger.info(
                f"Auto-identify: CURRENT_USER='{current_user_env}', DEFAULT_USER='{default_user_env}', "
                f"current={current_user.name if current_user else None}",
                "👤",
            )

            if current_user_env and current_user_env.lower() == "guest":
                if current_user:
                    logger.info("Switching to guest mode", "👤")
                    user_manager.clear_current_user()
                return

            if user_to_identify and user_to_identify.lower() != "guest":
                if (
                    not current_user
                    or current_user.name.lower() != user_to_identify.lower()
                ):
                    logger.info(f"Auto-loading user: {user_to_identify}", "👤")
                    await self.load_user_profile_silently(user_to_identify)
                else:
                    logger.info(f"User already loaded: {current_user.name}", "👤")
        except Exception as e:
            logger.warning(f"Failed to auto-identify: {e}", "⚠️")

    async def load_user_profile_silently(self, user_name: str):
        """Load a user profile without greeting."""
        try:
            profile = user_manager.identify_user(user_name, "high")
            if profile:
                await self.save_current_user_to_env(profile.name)
                await self.switch_to_user_persona(profile)
                await self.update_session_with_user_context()
                logger.info(f"Silently loaded profile: {profile.name}", "👤")
            else:
                logger.warning(f"Failed to load profile: {user_name}", "⚠️")
        except Exception as e:
            logger.warning(f"Failed to load profile silently: {e}", "⚠️")

    async def send_user_greeting(self, profile, call_id: str | None = None):
        """Send a personalized greeting."""
        try:
            greeting_context = self._generate_greeting_context(profile)

            if call_id:
                await self.session._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "status": "success",
                            "user": profile.name,
                            "message": f"Identified {profile.name}",
                        }),
                    },
                })
                await asyncio.sleep(0.1)

            greeting_prompt = (
                "[SYSTEM: Speak your greeting now to welcome the new user]"
                if greeting_context["is_first_meeting"]
                else "[SYSTEM: Speak your greeting now to acknowledge the returning user]"
            )

            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": greeting_prompt}],
                },
            })

            await self.session._ws_send_json({"type": "response.create"})
            logger.info(f"Greeting sent for {profile.name}", "👤")
        except Exception as e:
            logger.warning(f"Failed to send greeting: {e}", "⚠️")

    async def save_current_user_to_env(self, user_name: str):
        """Save current user to .env file."""
        try:
            from dotenv import set_key

            from ..config import ENV_PATH

            set_key(ENV_PATH, "CURRENT_USER", user_name.lower(), quote_mode="never")
            logger.info(f"Saved to .env: {user_name}", "👤")
        except Exception as e:
            logger.warning(f"Failed to save to .env: {e}")

    async def switch_to_user_persona(self, profile):
        """Switch to user's preferred persona."""
        try:
            preferred_persona = profile.data["USER_INFO"].get(
                "preferred_persona", "default"
            )
            persona_manager.switch_persona(preferred_persona)
            logger.info(f"Switched to persona: {preferred_persona}", "🎭")
        except Exception as e:
            logger.warning(f"Failed to switch persona: {e}")

    async def update_session_with_user_context(self):
        """Update session with current user context."""
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
            logger.warning(f"Failed to update session: {e}")

    async def _ask_spelling_confirmation(self, name: str):
        """Ask user to confirm name spelling."""
        response = f"I think I heard '{name}' - is that spelled correctly? Please say 'yes' or spell it out for me."
        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": response}],
            },
        })

    async def _handle_user_denial(self, current_name: str):
        """Handle user saying they're not the current user."""
        logger.info(f"User denies being {current_name}", "👤")
        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"I understand you're not {current_name}. Who are you? "
                        "Tell me your name or I'll switch to guest mode.",
                    }
                ],
            },
        })
        self._waiting_for_name_after_denial = True

    async def _fallback_to_guest(self):
        """Fall back to guest mode."""
        logger.info("Falling back to guest mode", "👤")
        user_manager.clear_current_user()
        await self.save_current_user_to_env("guest")
        self._waiting_for_name_after_denial = False

        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "No problem! I've switched to guest mode. "
                        "Tell me your name later if you'd like a personalized experience.",
                    }
                ],
            },
        })

    def _generate_greeting_context(self, profile) -> dict:
        """Generate greeting context based on profile."""
        interaction_count = int(profile.data["USER_INFO"].get("interaction_count", "0"))
        preferred_persona = profile.data["USER_INFO"].get(
            "preferred_persona", "default"
        )
        last_seen = profile.data["USER_INFO"].get("last_seen", "")

        current_time = datetime.now()
        current_hour = current_time.hour

        if 5 <= current_hour < 12:
            time_period = "morning"
        elif 12 <= current_hour < 17:
            time_period = "afternoon"
        elif 17 <= current_hour < 22:
            time_period = "evening"
        else:
            time_period = "night"

        recency = "recent"
        time_since_last_seen = None
        if last_seen:
            try:
                last_seen_time = datetime.fromisoformat(
                    last_seen.replace("Z", "+00:00")
                )
                time_diff = current_time - last_seen_time

                if time_diff.days > 7:
                    recency = "long_time"
                    time_since_last_seen = f"{time_diff.days} days"
                elif time_diff.days > 1:
                    recency = "few_days"
                    time_since_last_seen = f"{time_diff.days} days"
                elif time_diff.total_seconds() > 12 * 3600:
                    recency = "yesterday"
                    time_since_last_seen = "yesterday"
                elif time_diff.total_seconds() > 2 * 3600:
                    recency = "earlier"
                    time_since_last_seen = (
                        f"{int(time_diff.total_seconds() / 3600)} hours"
                    )
                else:
                    recency = "recent"
                    time_since_last_seen = "recently"
            except Exception:
                recency = "recent"

        context = {
            "user_name": profile.name,
            "is_first_meeting": interaction_count == 0,
            "time_of_day": time_period,
            "current_time": current_time.strftime("%I:%M %p"),
            "day_of_week": current_time.strftime("%A"),
            "date": current_time.strftime("%B %d"),
            "recency": recency,
            "interaction_count": interaction_count,
            "preferred_persona": preferred_persona,
        }

        if time_since_last_seen:
            context["time_since_last_seen"] = time_since_last_seen

        return context
