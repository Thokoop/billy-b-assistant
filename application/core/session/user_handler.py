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
        self._pending_new_user_name = None
        self._pending_identity_choices = frozenset()
        # The user already welcomed in this conversation. A model asked again
        # about an introduction it already handled must not greet twice.
        self._greeted_user = None

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

        pending_name = self._pending_new_user_name
        pending_choices = self._pending_identity_choices
        self._pending_new_user_name = None
        self._pending_identity_choices = frozenset()
        existing = user_manager.find_user_by_name_or_display_name(name)
        candidates = user_manager.find_similar_users(name)
        confirmed_new = (
            args.get("new_user_confirmed") is True
            and pending_name == name.casefold()
            and confidence != "low"
        )
        confirmed_existing = (
            existing is not None
            and args.get("existing_user_confirmed") is True
            and existing.casefold() in pending_choices
            and name.casefold() == existing.casefold()
            and confidence != "low"
        )
        # An exact transcription is not proof of identity when, for example,
        # both Hank and Henk exist. Ask before selecting either profile too.
        ambiguous_existing = existing is not None and any(
            candidate['name'].casefold() != existing.casefold()
            for candidate in candidates
        )
        if (ambiguous_existing and not confirmed_existing) or (
            not existing and candidates and not confirmed_new
        ):
            self._pending_new_user_name = name.casefold() if not existing else None
            self._pending_identity_choices = frozenset(
                candidate['name'].casefold() for candidate in candidates
            )
            choices = ', '.join(
                candidate['display_name']
                if candidate['display_name'].casefold() == candidate['name'].casefold()
                else f"{candidate['display_name']} ({candidate['name']})"
                for candidate in candidates
            )
            question = (
                f"I have similar existing profiles: {choices}. Which one is yours? Please confirm or spell your name."
                if existing
                else f"Do you mean the existing profile {choices}, or are you a new user named {name}?"
            )
            logger.info(
                "Asking for clarification before selecting or creating a user profile",
                "👤",
            )
            await self._request_identity_clarification(
                name,
                question,
                call_id,
                candidates=candidates,
            )
            return

        if confidence == "low":
            await self._ask_spelling_confirmation(name, call_id)
            return

        current_user = user_manager.get_current_user()
        if (
            current_user
            and context
            and name.casefold() == current_user.name.casefold()
            and f"not {current_user.name.casefold()}" in context.casefold()
        ):
            await self._handle_user_denial(current_user.name, call_id)
            return

        if (
            self._greeted_user
            and name.casefold() == self._greeted_user
            and current_user
            and current_user.name.casefold() == self._greeted_user
        ):
            await self._confirm_already_identified(current_user, call_id)
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
                self._greeted_user = profile.name.casefold()
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
        self._greeted_user = None  # A new conversation greets again.
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

    async def _confirm_already_identified(self, profile, call_id=None):
        """Answer a repeated identification without a second greeting."""
        logger.info(f"{profile.name} is already identified; not greeting again", "👤")
        if call_id:
            await self.session._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({
                        "status": "success",
                        "user": profile.name,
                        "message": (
                            f"{profile.name} was already identified and welcomed in "
                            "this conversation. Do not greet or welcome them again; "
                            "continue the conversation."
                        ),
                    }),
                },
            })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    async def save_current_user_to_env(self, user_name: str):
        """Save current user to .env file."""
        try:
            from ..config import ENV_PATH
            from ..env_utils import set_env_key

            set_env_key(ENV_PATH, "CURRENT_USER", user_name.lower())
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
            self.session.mark_mood_instructions_current()
            logger.info(
                f"Updated session with user context (persona={persona_manager.current_persona}, voice={session_voice})",
                "👤",
            )
        except Exception as e:
            logger.warning(f"Failed to update session: {e}")

    async def _request_identity_clarification(
        self, name, question, call_id=None, *, candidates=None
    ):
        """Return a real tool outcome and ask before any profile side effects."""
        result = {
            "status": "clarification_required",
            "proposed_name": name,
            "candidates": candidates or [],
            "question": question,
            "instruction": (
                "No profile has been created or selected. Ask the user this question "
                "and wait for their answer in a later turn. For an existing user, "
                "call identify_user with the chosen candidate's exact profile name "
                "and existing_user_confirmed=true after they confirm that choice. Only "
                "after they explicitly confirm they are a different, new user, "
                "call identify_user with the proposed name and new_user_confirmed=true. "
                "Do not infer confirmation from the original introduction."
            ),
        }
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
                "content": [{"type": "output_text", "text": question}],
            }
        await self.session._ws_send_json({
            "type": "conversation.item.create",
            "item": item,
        })
        self.session.state._triggered_new_response = True
        await self.session._ws_send_json({"type": "response.create"})

    async def _ask_spelling_confirmation(self, name: str, call_id=None):
        """Ask user to confirm name spelling and complete the pending tool call."""
        await self._request_identity_clarification(
            name,
            f"I think I heard '{name}' - is that spelled correctly? Please confirm or spell it out for me.",
            call_id,
        )

    async def _handle_user_denial(self, current_name: str, call_id=None):
        """Handle user saying they're not the current user."""
        logger.info(f"User denies being {current_name}", "👤")
        self._waiting_for_name_after_denial = True
        await self._request_identity_clarification(
            current_name,
            f"I understand you're not {current_name}. What is your name?",
            call_id,
        )

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
