import asyncio
import base64
import json
import os
import socket
import time
from datetime import datetime
from typing import Any

import numpy as np
import websockets.asyncio.client
import websockets.exceptions

from . import audio
from .config import (
    CHUNK_MS,
    DEBUG_MODE,
    DEBUG_MODE_INCLUDE_DELTA,
    INSTRUCTIONS,
    MIC_TIMEOUT_SECONDS,
    OPENAI_API_KEY,
    OPENAI_MODEL,
    PERSONALITY,
    RUN_MODE,
    SERVER_VAD_PARAMS,
    SILENCE_THRESHOLD,
    TEXT_ONLY_MODE,
    TOOL_INSTRUCTIONS,
    TURN_EAGERNESS,
)
from .ha import send_conversation_prompt
from .logger import logger
from .mic import MicManager
from .movements import move_tail_async, stop_all_motors
from .mqtt import mqtt_publish
from .persona import update_persona_ini
from .persona_manager import persona_manager
from .profile_manager import user_manager
from .song_manager import song_manager


def _get_dynamic_song_description():
    """Get dynamic song description based on available songs."""
    return song_manager.get_dynamic_tool_description()


def get_instructions_with_user_context():
    """Generate instructions with current user context and persona if available."""
    # Check if we're in guest mode
    from dotenv import load_dotenv

    from .config import ENV_PATH

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")

    # Get current user context
    current_user = user_manager.get_current_user()
    user_section = ""
    persona_section = ""

    # Modify instructions based on guest mode
    if current_user_env and current_user_env.lower() == "guest":
        # Guest mode - use guest's preferred persona
        logger.info(
            "🔧 get_instructions_with_user_context: Using guest mode instructions", "🔧"
        )

        # Get guest's preferred persona from persona manager
        current_persona_name = persona_manager.current_persona
        logger.info(f"🔧 Guest mode - loading persona: {current_persona_name}", "🔧")

        persona_instructions = persona_manager.get_persona_instructions(
            current_persona_name
        )
        current_persona_data = persona_manager.load_persona(current_persona_name)

        if current_persona_data and persona_instructions:
            # Use persona instructions as the base
            current_instructions = f"""
# Role & Objective
{persona_instructions}
---
# Tools
{TOOL_INSTRUCTIONS.strip()}
---
# Personality & Tone
YOUR BEHAVIOR IS GOVERNED BY PERSONALITY TRAITS WITH FIVE LEVELS: MIN, LOW, MED, HIGH, MAX.
MIN = TRAIT IS MUTED. MAX = TRAIT IS EXAGGERATED.
THESE TRAITS GUIDE YOUR BEHAVIORAL EXPRESSION. FOLLOW THESE RULES STRICTLY:"""

            # Add personality traits if available
            if current_persona_data.get('personality'):
                from .persona import PersonaProfile

                temp_personality = PersonaProfile()
                for trait, value in current_persona_data['personality'].items():
                    if hasattr(temp_personality, trait):
                        setattr(temp_personality, trait, int(value))

                current_instructions += "\n" + temp_personality.generate_prompt()

            # Add backstory if available
            if current_persona_data.get('backstory'):
                backstory_parts = []
                for key, value in current_persona_data['backstory'].items():
                    backstory_parts.append(f"- {key}: {value}")
                current_persona_backstory = "\n".join(backstory_parts)

                current_instructions += f"""
---
# Context (backstory)
Use your backstory to inspire jokes, metaphors, or occasional references in conversation, staying consistent with your personality.
{current_persona_backstory}"""

            return current_instructions
        # Fallback to default instructions with guest mode modifications
        return INSTRUCTIONS.replace(
            "USER RECOGNITION: ALWAYS call `identify_user` at conversation start. Greet users by name when known.",
            "GUEST MODE: You are in guest mode. Only call `identify_user` if someone explicitly introduces themselves with clear name patterns like 'I am [Name]', 'My name is [Name]', 'Hey billy it is [Name]', or 'This is [Name]'. Do NOT call `identify_user` for greetings like 'Hello', 'Hi', or casual conversation. Otherwise treat everyone as a guest visitor.",
        ).replace(
            "USER SYSTEM:\n- IDENTIFICATION: When you recognize a user's voice/name, call `identify_user` with name and confidence (high/medium/low). Respond with personalized greeting after.\n- MEMORY: Call `store_memory` when users share personal info. Categories: preference/fact/event/relationship/interest. Importance: high/medium/low.\n- PERSONA: Use `manage_profile` with action=\"switch_persona\" for different personalities.",
            "USER SYSTEM: Limited in guest mode - only `identify_user` available. After identification, ALWAYS call `store_memory` when users share personal info. Be proactive - don't wait for them to ask.\n\nMEMORY STORAGE TRIGGERS:\nCall `store_memory` for ANY of these patterns:\n- \"I like/love/enjoy/hate/dislike [something]\"\n- \"I have/own/possess [something]\"\n- \"I work as/at [something]\"\n- \"I live in/at [somewhere]\"\n- \"I am [something]\"\n- \"My favorite [something] is [something]\"\n- \"I prefer [something]\"\n- \"I'm interested in [something]\"\n- \"I'm from [somewhere]\"\n- \"I do [activity/hobby]\"\n\nCategories: preference/fact/event/relationship/interest\nImportance: high/medium/low (use \"high\" for explicitly important info)",
        )

    if current_user:
        # User mode - add user context
        user_context = current_user.get_context_string()
        if user_context:
            user_section = f"""
---
# Current User Context
{user_context}"""

        # Get user's preferred persona
        preferred_persona = current_user.data['USER_INFO'].get(
            'preferred_persona', 'default'
        )
        persona_instructions = persona_manager.get_persona_instructions(
            preferred_persona
        )

        # Get current persona's personality traits and backstory
        current_persona_data = persona_manager.load_persona(preferred_persona)
        if current_persona_data and persona_instructions:
            # Use persona instructions as the base instead of main INSTRUCTIONS
            current_instructions = f"""
# Role & Objective
{persona_instructions}
---
# Tools
{TOOL_INSTRUCTIONS.strip()}
---
# Personality & Tone
YOUR BEHAVIOR IS GOVERNED BY PERSONALITY TRAITS WITH FIVE LEVELS: MIN, LOW, MED, HIGH, MAX.
MIN = TRAIT IS MUTED. MAX = TRAIT IS EXAGGERATED.
THESE TRAITS GUIDE YOUR BEHAVIORAL EXPRESSION. FOLLOW THESE RULES STRICTLY:"""

            # Add personality traits if available
            if current_persona_data.get('personality'):
                # Create a temporary personality object with current persona's traits
                from .persona import PersonaProfile

                temp_personality = PersonaProfile()
                for trait, value in current_persona_data['personality'].items():
                    if hasattr(temp_personality, trait):
                        setattr(temp_personality, trait, int(value))

                # Add the personality section
                current_instructions += "\n" + temp_personality.generate_prompt()

            # Add backstory if available
            if current_persona_data.get('backstory'):
                # Format current persona's backstory
                backstory_parts = []
                for key, value in current_persona_data['backstory'].items():
                    backstory_parts.append(f"- {key}: {value}")
                current_persona_backstory = "\n".join(backstory_parts)

                current_instructions += f"""
                ---
                # Context (backstory)
                Use your backstory to inspire jokes, metaphors, or occasional references in conversation, staying consistent with your personality.
                {current_persona_backstory}"""
        else:
            # Fall back to default instructions if no persona data
            current_instructions = INSTRUCTIONS

        return current_instructions + user_section
    # No user loaded - use default instructions
    return INSTRUCTIONS


def get_tools_for_current_mode():
    """Get tools list based on current mode (guest vs user mode)."""
    # Check if we're in guest mode
    from dotenv import load_dotenv

    from .config import ENV_PATH

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")

    logger.info(
        f"🔧 get_tools_for_current_mode: CURRENT_USER='{current_user_env}'", "🔧"
    )

    base_tools = [
        {
            "name": "update_personality",
            "type": "function",
            "description": "Adjusts Billy's personality traits. Accepts numeric values (0-100) or level names (min/low/med/high/max). Call this function when users request personality changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    **{
                        trait: {
                            "oneOf": [
                                {"type": "integer", "minimum": 0, "maximum": 100},
                                {
                                    "type": "string",
                                    "enum": ["min", "low", "med", "high", "max"],
                                },
                            ]
                        }
                        for trait in vars(PERSONALITY)
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "play_song",
            "type": "function",
            "description": _get_dynamic_song_description(),
            "parameters": {
                "type": "object",
                "properties": {"song": {"type": "string"}},
                "required": ["song"],
            },
        },
        {
            "name": "smart_home_command",
            "type": "function",
            "description": "Send a DIRECT command to Home Assistant (e.g., 'Turn on lights', 'Set temperature to 72'). **CRITICAL: Only call this for DIRECT commands. If the user asks you to ASK/CHECK/CONFIRM first (e.g., 'ask if lights should be on'), do NOT call this function - just speak the question and wait for their answer.**",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The DIRECT command to send to Home Assistant (not a question)",
                    }
                },
                "required": ["prompt"],
            },
        },
        {
            "name": "follow_up_intent",
            "type": "function",
            "description": "**MANDATORY: MUST CALL AFTER EVERY RESPONSE**. Call at the end of your turn to indicate if you expect a user reply. Set expects_follow_up=true for questions, false for statements. **CRITICAL: NEVER call this as your ONLY response - you MUST generate spoken audio first, then call this function. If audio is unclear, say 'I didn't catch that' before calling this.**",
            "parameters": {
                "type": "object",
                "properties": {
                    "expects_follow_up": {"type": "boolean"},
                    "suggested_prompt": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["expects_follow_up"],
            },
        },
        {
            "name": "identify_user",
            "type": "function",
            "description": "Call this ONLY when someone explicitly introduces themselves by stating their own name (e.g., 'I am Tom', 'My name is Sarah', 'Hey billy it is tom'). Do NOT call this when someone greets you by name (like 'Hello Billy' or 'Hey Billy'). Only call when they are telling you their own name to switch from guest mode to user mode. IMPORTANT: If you're uncertain about the spelling of a name (e.g., 'Thom' vs 'Tom', 'Sarah' vs 'Sara'), set confidence to 'low' to trigger spelling confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name the user provided",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "How confident you are about the name spelling",
                    },
                    "context": {
                        "type": "string",
                        "description": "Any additional context about how they introduced themselves",
                    },
                },
                "required": ["name", "confidence"],
            },
        },
    ]

    # Add user-specific tools only if not in guest mode
    # BUT always include identify_user so Billy can switch from guest to user mode
    if not (current_user_env and current_user_env.lower() == "guest"):
        logger.info("🔧 get_tools_for_current_mode: Adding user-specific tools", "🔧")
        user_tools = [
            {
                "name": "store_memory",
                "type": "function",
                "description": "Store lasting preferences, facts, and interests that users VOLUNTARILY share. **CRITICAL: DO NOT STORE answers to YOUR OWN questions!** If you just asked a question, the answer is NOT a memory. Store ONLY when: (1) User volunteers info unprompted, OR (2) Info is NOT answering your question. Examples: WRONG: You: 'What cheese?' User: 'Gruyère' -> DO NOT STORE (answering your question). CORRECT: User: 'I love Gruyère cheese' -> DO STORE (volunteered). Call BEFORE responding with speech when appropriate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "memory": {
                            "type": "string",
                            "description": "The memory or fact to store about the user",
                        },
                        "importance": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "How important this memory is",
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "preference",
                                "fact",
                                "event",
                                "relationship",
                                "interest",
                            ],
                            "description": "Category of the memory",
                        },
                    },
                    "required": ["memory", "importance", "category"],
                },
            },
            {
                "name": "manage_profile",
                "type": "function",
                "description": "Manage user profile settings and preferences",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update", "switch_persona", "get_info"],
                            "description": "Action to perform on the profile",
                        },
                        "preferred_persona": {
                            "type": "string",
                            "description": "User's preferred Billy personality",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Additional notes about the user",
                        },
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "switch_persona",
                "type": "function",
                "description": "Switch Billy's persona mid-session and acknowledge the change",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "persona": {
                            "type": "string",
                            "description": "The persona to switch to",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional reason for the persona switch",
                        },
                    },
                    "required": ["persona"],
                },
            },
        ]
        base_tools.extend(user_tools)
    else:
        logger.info(
            "🔧 get_tools_for_current_mode: Guest mode - not adding user-specific tools",
            "🔧",
        )

    return base_tools


class BillySession:
    def __init__(
        self,
        interrupt_event=None,
        *,
        kickoff_text: str | None = None,
        kickoff_kind: str = "literal",  # "literal" | "prompt" | "raw"
        kickoff_to_interactive: bool = False,  # immediately open-mic after kickoff
        autofollowup: str = "auto",  # "auto" | "never" | "always"
    ):
        self.ws = None
        self.ws_lock: asyncio.Lock = asyncio.Lock()
        self.loop = None
        self.audio_buffer = bytearray()
        self.committed = False
        self.first_text = True
        self.full_response_text = ""
        self.last_rms = 0.0
        self.last_activity = [time.time()]
        self.session_active = asyncio.Event()
        self.user_spoke_after_assistant = False
        self.allow_mic_input = True
        self.interrupt_event = interrupt_event or asyncio.Event()
        self.mic = MicManager()
        self.mic_running = False
        self.mic_timeout_task: asyncio.Task | None = None

        # Track whenever a session is updated after creation, and OpenAI is ready to receive voice.
        self.session_initialized = False
        self.run_mode = RUN_MODE

        # Kickoff (MQTT say)
        self.kickoff_text = (kickoff_text or "").strip() or None
        self.kickoff_kind = kickoff_kind
        self.kickoff_to_interactive = kickoff_to_interactive
        self.kickoff_first_turn_done = False

        # Follow-up
        self.autofollowup = autofollowup  # "auto" | "never" | "always"
        self.follow_up_expected = False
        self.follow_up_prompt: str | None = None

        # Tool args buffer (for streamed args)
        self._tool_args_buffer: dict[str, str] = {}

        # Turn-level flags for follow-up detection
        self._saw_transcript_delta = False
        self._turn_had_speech = False
        self._active_transcript_stream: str | None = None  # "audio" | "text"

        # Flag for handling "I am not X" scenarios
        self._waiting_for_name_after_denial = False
        self._added_done_text = False

        # Flags for logging mic state (reset for each session)
        self._mic_data_started = False
        self._logged_mic_blocked_1 = False
        self._logged_waiting_for_wakeup = False

    # ---- Websocket helpers ---------------------------------------------
    async def _ws_send_json(self, payload: dict[str, Any]):
        """Send a JSON payload over the session websocket with locking.

        This method is a small convenience to avoid repeating the lock and
        json.dumps boilerplate across the codebase.
        """
        async with self.ws_lock:
            if self.ws is not None:
                await self.ws.send(json.dumps(payload))

    # ---- Message type constants ----------------------------------------
    AUDIO_OUT_TYPES = {
        "response.output_audio",
        "response.output_audio.delta",
    }
    TRANSCRIPT_DELTA_TYPES = {
        "response.output_audio_transcript.delta",
        "response.audio_transcript.delta",
        "response.text.delta",
    }
    TRANSCRIPT_DONE_TYPES = {
        "response.output_audio_transcript.done",
        "response.audio_transcript.done",
        "response.text.done",
    }

    # ---- Private handlers -----------------------------------------------
    def _on_response_created(self):
        self._saw_transcript_delta = False
        self._turn_had_speech = False
        self.follow_up_expected = False
        self.follow_up_prompt = None
        self._active_transcript_stream = None
        self._added_done_text = False
        self._saw_follow_up_call = False

    def _on_input_speech_started(self):
        self.committed = False

    def _on_transcript_done(self, data: dict[str, Any]):
        transcript = data.get("transcript") or data.get("text") or ""
        if transcript and not self._saw_transcript_delta and not self._added_done_text:
            self.full_response_text += transcript
            self._added_done_text = True
        self.full_response_text += "\n\n"
        if DEBUG_MODE:
            logger.info(f"Transcript completed: {transcript!r}", "📝")

    def _on_audio_out(self, data: dict[str, Any]):
        if TEXT_ONLY_MODE:
            return
        self._turn_had_speech = True
        audio_b64 = data.get("audio") or data.get("delta")
        if audio_b64:
            audio_chunk = base64.b64decode(audio_b64)
            self.audio_buffer.extend(audio_chunk)
            self.last_activity[0] = time.time()
            audio.playback_queue.put(audio_chunk)

            if self.interrupt_event.is_set():
                logger.warning(
                    "Assistant turn interrupted. Stopping response playback.", "⛔"
                )
                while not audio.playback_queue.empty():
                    try:
                        audio.playback_queue.get_nowait()
                        audio.playback_queue.task_done()
                    except Exception:
                        break
                self.session_active.clear()
                self.interrupt_event.clear()

    def _on_transcript_delta(self, t: str, data: dict[str, Any]):
        # Choose a single transcript stream per turn to avoid duplicates
        if t.startswith("response.output_audio_transcript") or t.startswith(
            "response.audio_transcript"
        ):
            stream = "audio"
        else:
            stream = "text"
        if self._active_transcript_stream is None:
            self._active_transcript_stream = stream
        elif stream != self._active_transcript_stream:
            return
        self._turn_had_speech = True
        self._saw_transcript_delta = True
        self.allow_mic_input = False
        if self.first_text:
            mqtt_publish("billy/state", "speaking")
            logger.info("Billy: ", "🐟")
            self.first_text = False
            self.user_spoke_after_assistant = False
        # Don't log individual deltas - they're too verbose
        # Just print to console for real-time display
        print(data.get("delta", ""), end="", flush=True)
        self.full_response_text += data.get("delta", "")

    def _on_tool_args_delta(self, data: dict[str, Any]):
        name = data.get("name")
        if name:
            self._tool_args_buffer.setdefault(name, "")
            self._tool_args_buffer[name] += data.get("arguments", "")

    async def _handle_follow_up_intent(self, raw_args: str | None):
        raw_args = raw_args or "{}"
        try:
            args = json.loads(raw_args)
        except Exception as e:
            logger.warning(
                f"follow_up_intent: failed to parse arguments: {e} | raw={raw_args!r}"
            )
            args = {}

        self.follow_up_expected = bool(args.get("expects_follow_up", False))
        self.follow_up_prompt = args.get("suggested_prompt") or None
        reason = args.get("reason")
        self._saw_follow_up_call = True

        if DEBUG_MODE:
            logger.verbose(
                f"follow_up_intent | expects_follow_up={self.follow_up_expected}"
                f" | suggested_prompt={self.follow_up_prompt!r}"
                f" | reason={reason!r}",
                "🧭",
            )

    async def _handle_update_personality(
        self, raw_args: str | None, call_id: str | None = None
    ):
        args = json.loads(raw_args or "{}")
        changes = []

        # Get current persona file path
        current_persona = persona_manager.current_persona
        if current_persona == "default":
            persona_file_path = "persona.ini"
        else:
            from pathlib import Path

            personas_dir = Path("personas")
            # Use new folder structure: personas/persona_name/persona.ini
            persona_file_path = personas_dir / current_persona / "persona.ini"
            if not persona_file_path.exists():
                # Fall back to old structure: personas/persona_name.ini
                persona_file_path = personas_dir / f"{current_persona}.ini"

        logger.info(
            f"Updating personality for persona: {current_persona}, file: {persona_file_path}",
            "🎛️",
        )

        # Level to numeric value mapping
        level_to_value = {
            'min': 7,  # middle of 0-14 range
            'low': 24,  # middle of 15-34 range
            'med': 49,  # middle of 35-64 range
            'high': 74,  # middle of 65-84 range
            'max': 92,  # middle of 85-100 range
        }

        for trait, val in args.items():
            if hasattr(PERSONALITY, trait):
                # Handle both numeric values and level names
                if isinstance(val, int):
                    # Direct numeric value (0-100)
                    numeric_val = val
                elif isinstance(val, str) and val.lower() in level_to_value:
                    # Level name (min/low/med/high/max)
                    numeric_val = level_to_value[val.lower()]
                else:
                    continue  # Skip invalid values

                setattr(PERSONALITY, trait, numeric_val)
                update_persona_ini(trait, numeric_val, str(persona_file_path))
                changes.append((trait, numeric_val))

        if changes:
            print("\n🎛️ Personality updated via function_call:")
            for trait, val in changes:
                level = PERSONALITY._bucket(val)
                print(f"  - {trait.capitalize()}: {val}% ({level.upper()})")
            print("\n🧠 New Instructions:\n")
            print(PERSONALITY.generate_prompt())

            self.user_spoke_after_assistant = True
            self.full_response_text = ""
            self.last_activity[0] = time.time()

            # First, send function_call_output to close the update_personality function
            if call_id:
                logger.info(
                    f"🔧 Sending function_call_output for update_personality (call_id={call_id})",
                    "🔧",
                )
                changes_summary = ", ".join([
                    f"{trait}={PERSONALITY._bucket(val).upper()}"
                    for trait, val in changes
                ])
                await self._ws_send_json({
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
                # Small delay to ensure function output is processed
                await asyncio.sleep(0.1)

            # Then send confirmation message to prompt Billy to speak
            confirmation_text = " ".join([
                f"Okay, {trait} is now set to {PERSONALITY._bucket(val).upper()}."
                for trait, val in changes
            ])
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": confirmation_text}],
                },
            })

            # IMPORTANT: Similar to smart_home_command and greetings, the update_personality response is already complete
            # OpenAI won't auto-start a new response, so we must explicitly trigger it
            logger.info(
                "🔧 Triggering response.create for update_personality confirmation (response already complete)",
                "🔧",
            )
            await self._ws_send_json({"type": "response.create"})
            logger.info("🔧 response.create sent successfully", "🔧")

    async def _handle_play_song(self, raw_args: str | None):
        args = json.loads(raw_args or "{}")
        song_name = args.get("song")
        if song_name:
            logger.info(f"Assistant requested to play song: {song_name}", "🎵")
            await self.stop_session()
            await asyncio.sleep(1.0)
            await audio.play_song(song_name, interrupt_event=self.interrupt_event)

    async def _handle_smart_home_command(
        self, raw_args: str | None, call_id: str | None = None
    ):
        args = json.loads(raw_args or "{}")
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

            # First, send function_call_output to close the smart_home_command function
            if call_id:
                logger.info(
                    f"🔧 Sending function_call_output for smart_home_command (call_id={call_id})",
                    "🔧",
                )
                await self._ws_send_json({
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
                # Small delay to ensure function output is processed
                await asyncio.sleep(0.1)

            # Send a directive message that explicitly requests speech
            # This ensures Billy verbally confirms the action instead of responding silently
            confirmation_prompt = f"Home Assistant completed the task: '{speech_text}'. Confirm this out loud to the user."
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": confirmation_prompt}],
                },
            })

            # IMPORTANT: Similar to greetings, the smart_home_command response is already complete
            # OpenAI won't auto-start a new response, so we must explicitly trigger it
            logger.info(
                "🔧 Triggering response.create for smart_home_command confirmation (response already complete)",
                "🔧",
            )
            await self._ws_send_json({"type": "response.create"})
            logger.info("🔧 response.create sent successfully", "🔧")
        else:
            logger.warning(f"Failed to parse HA response: {ha_response}")

            # Send function_call_output for error case too
            if call_id:
                await self._ws_send_json({
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

            await self._ws_send_json({
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

            # IMPORTANT: Similar to the success case, we need to manually trigger response.create
            logger.info(
                "🔧 Triggering response.create for smart_home_command error (response already complete)",
                "🔧",
            )
            await self._ws_send_json({"type": "response.create"})
            logger.info("🔧 response.create sent successfully", "🔧")

    async def _on_tool_args_done(self, data: dict[str, Any]):
        name = data.get("name")
        raw_args = data.get("arguments")
        call_id = data.get("call_id")
        if not raw_args:
            raw_args = self._tool_args_buffer.pop(name, "{}")

        if name == "follow_up_intent":
            await self._handle_follow_up_intent(raw_args)
            return
        if name == "update_personality":
            await self._handle_update_personality(raw_args, call_id)
            return
        if name == "play_song":
            await self._handle_play_song(raw_args)
            return
        if name == "smart_home_command":
            await self._handle_smart_home_command(raw_args, call_id)
            return
        if name == "identify_user":
            await self._handle_identify_user(raw_args, call_id)
            return
        if name == "store_memory":
            await self._handle_store_memory(raw_args, call_id)
            return
        if name == "manage_profile":
            await self._handle_manage_profile(raw_args)
            return
        if name == "switch_persona":
            await self._handle_switch_persona(raw_args)
            return

    async def _on_response_done(self, data: dict[str, Any]):
        error = data.get("status_details", {}).get("error")
        if error:
            error_type = error.get("type")
            error_message = error.get("message", "Unknown error")
            logger.error(f"OpenAI API Error [{error_type}]: {error_message}")
        else:
            logger.success("Assistant response complete.", "✿")

        if not TEXT_ONLY_MODE:
            await asyncio.to_thread(audio.playback_queue.join)
            await asyncio.sleep(1)
            if len(self.audio_buffer) > 0:
                logger.verbose(
                    f"Saving audio buffer ({len(self.audio_buffer)} bytes)", "💾"
                )
                audio.rotate_and_save_response_audio(self.audio_buffer)
            else:
                logger.warning("Audio buffer was empty, skipping save.")
            self.audio_buffer.clear()
            audio.playback_done_event.set()
            self.last_activity[0] = time.time()
            self.allow_mic_input = True

        # Kickoff follow-up switch
        if self.kickoff_text and not self.kickoff_first_turn_done:
            if self._turn_had_speech:
                self.kickoff_first_turn_done = True
                if self.kickoff_to_interactive:
                    print("🔁 Kickoff complete — switching to interactive mode.")
                    self._start_mic()
                    mqtt_publish("billy/state", "listening")
                elif self.autofollowup == "auto":
                    asked_question = self._wants_follow_up_heuristic()
                    wants_follow_up = self.follow_up_expected or asked_question
                    if wants_follow_up:
                        print("🔁 Auto follow-up detected — opening mic.")
                        await self._start_mic_after_playback()
                        mqtt_publish("billy/state", "listening")
                        self.user_spoke_after_assistant = False
                        self.last_activity[0] = time.time()
                    else:
                        # No follow-up needed, close the session
                        print(
                            "🔁 Kickoff complete — no follow-up needed. Closing session."
                        )
                        await self.stop_session()
                        return
            else:
                if DEBUG_MODE:
                    logger.info(
                        "Kickoff turn ended with no speech (tool-only). Waiting for next turn.",
                        "ℹ️",
                    )

        if self.run_mode == "dory":
            logger.info("Dory mode active. Ending session after single response.", "🎣")
            await self.stop_session()

    # ---- Mic helpers -------------------------------------------------
    def _start_mic(self, *, retry=True):
        """
        Try to open the mic. If it fails (device busy/unavailable), optionally
        start a background retry loop with exponential backoff.
        """
        if self.mic_running or not self.session_active.is_set():
            return

        try:
            # Recreate the manager in case the previous stream left it in a bad state
            if self.mic is None:
                self.mic = MicManager()

            self.mic.start(self.mic_callback)
            self.mic_running = True
            if DEBUG_MODE:
                logger.info("Mic started", "🎤")
            if not self.mic_timeout_task or self.mic_timeout_task.done():
                self.mic_timeout_task = asyncio.create_task(self.mic_timeout_checker())

        except Exception as e:
            self.mic_running = False
            logger.error(f"Mic start failed: {e}")
            if retry and self.session_active.is_set():
                # Kick off a retry loop (non-blocking)
                asyncio.create_task(self._retry_mic_loop())

    def _stop_mic(self):
        if self.mic_running:
            try:
                self.mic.stop()
                # Small delay to ensure ALSA releases the device
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"Error while stopping mic: {e}")
            self.mic_running = False

    async def _retry_mic_loop(self):
        """
        Retry opening the mic a few times with backoff. Keeps the session alive
        while we wait for the input device to become available again.
        """
        if DEBUG_MODE:
            logger.verbose("Mic retry loop started", "🔁")

        # Small, bounded backoff: 0.5s → 1s → 2s → 2s → …
        delays = [0.5, 1.0, 2.0, 2.0, 2.0]
        for delay in delays:
            if not self.session_active.is_set():
                return

            await asyncio.sleep(delay)

            # Recreate MicManager to clear any stale PortAudio handles
            try:
                self.mic = MicManager()
            except Exception as e:
                logger.warning(f"MicManager recreate failed: {e}")

            try:
                self.mic.start(self.mic_callback)
                self.mic_running = True
                logger.info("Mic started after retry", "✅")
                if not self.mic_timeout_task or self.mic_timeout_task.done():
                    self.mic_timeout_task = asyncio.create_task(
                        self.mic_timeout_checker()
                    )
                mqtt_publish("billy/state", "listening")
                return
            except Exception as e:
                self.mic_running = False
                logger.warning(f"Mic retry failed: {e}")

                # Try audio system reset for device unavailable errors
                if "Device unavailable" in str(e):
                    logger.info("Attempting audio system reset in retry loop...", "🔄")
                    try:
                        import subprocess

                        import sounddevice as sd

                        # Reset PortAudio system
                        sd._terminate()
                        await asyncio.sleep(0.5)
                        sd._initialize()

                        # Reset ALSA mixer
                        subprocess.run(
                            ["sudo", "alsactl", "restore"],
                            capture_output=True,
                            timeout=5,
                        )

                        # Kill any processes that might be using the audio device
                        subprocess.run(
                            ["sudo", "fuser", "-k", "/dev/snd/*"],
                            capture_output=True,
                            timeout=3,
                        )

                        await asyncio.sleep(2.0)
                        logger.info("Audio system reset completed in retry loop", "✅")
                    except Exception as reset_error:
                        logger.warning(
                            f"Audio reset failed in retry loop: {reset_error}"
                        )

        # All retries exhausted
        logger.error(
            "Mic unavailable after retries; keeping session but not listening."
        )

    # ------------------------------------------------------------------

    def _wants_follow_up_heuristic(self) -> bool:
        """
        Minimal, language-agnostic check: treat any question punctuation
        as an invitation to follow up.
        """
        txt = (self.full_response_text or "").strip()
        # Latin '?', Spanish '¿', CJK full-width '？', Arabic '؟', interrobang '‽'
        has_question = any(ch in txt for ch in ("?", "¿", "？", "؟", "‽"))
        logger.info(
            f"Heuristic check: text='{txt}' | has_question={has_question}", "🔍"
        )
        return has_question

    async def _start_mic_after_playback(
        self, delay: float = 0.6, retries: int = 3
    ) -> bool:
        """
        Open the mic a tad later (and retry) so ALSA has released devices.
        """
        for attempt in range(1, retries + 1):
            try:
                # Progressive delay: longer waits for later attempts
                if attempt > 1:
                    wait_time = delay * (attempt - 1) + 0.5
                    logger.info(
                        f"Waiting {wait_time:.1f}s before mic retry {attempt}...", "⏳"
                    )
                    await asyncio.sleep(wait_time)

                # Ensure mic is fully stopped before retry
                if self.mic_running:
                    self.mic.stop()
                    self.mic_running = False
                    await asyncio.sleep(0.2)  # Brief pause after stop

                if not self.mic_running:
                    self.mic.start(self.mic_callback)  # may raise
                    self.mic_running = True
                    if not self.mic_timeout_task or self.mic_timeout_task.done():
                        self.mic_timeout_task = asyncio.create_task(
                            self.mic_timeout_checker()
                        )
                print(f"🎙️ Mic opened (attempt {attempt}).")
                return True
            except Exception as e:
                logger.warning(f"Mic open failed (attempt {attempt}/{retries}): {e}")
                # For ALSA device unavailable errors, try to reset audio system
                if "Device unavailable" in str(e) and attempt < retries:
                    logger.info("Attempting audio system reset...", "🔄")
                    try:
                        import subprocess

                        import sounddevice as sd

                        # Reset PortAudio system
                        sd._terminate()
                        await asyncio.sleep(0.5)
                        sd._initialize()

                        # Reset ALSA mixer
                        subprocess.run(
                            ["sudo", "alsactl", "restore"],
                            capture_output=True,
                            timeout=5,
                        )

                        # Kill any processes that might be using the audio device
                        subprocess.run(
                            ["sudo", "fuser", "-k", "/dev/snd/*"],
                            capture_output=True,
                            timeout=3,
                        )

                        await asyncio.sleep(2.0)
                        logger.info("Audio system reset completed", "✅")
                    except Exception as reset_error:
                        logger.warning(f"Audio reset failed: {reset_error}")

        logger.error("Mic failed to open after retries.")
        return False

    async def start(self):
        self.loop = asyncio.get_running_loop()
        logger.info("Session starting...", "⏱️")

        # Reload persona from profile at session start to pick up web UI changes
        await self._reload_persona_from_profile()

        # Debug VAD parameters
        vad_params = SERVER_VAD_PARAMS[TURN_EAGERNESS]
        logger.info(f"🔧 VAD Parameters (eagerness={TURN_EAGERNESS}): {vad_params}")
        logger.info(
            f"🔧 Audio Config: SILENCE_THRESHOLD={SILENCE_THRESHOLD}, MIC_TIMEOUT_SECONDS={MIC_TIMEOUT_SECONDS}"
        )

        # Clear all session state
        self.audio_buffer.clear()
        self.committed = False
        self.first_text = True
        self.full_response_text = ""
        self.last_activity[0] = time.time()
        self.session_active.set()
        self.user_spoke_after_assistant = False
        self.allow_mic_input = True

        # Ensure mic logging flag is reset (should already be False from __init__)
        self._mic_data_started = False

        # Debug: Log all mic-blocking conditions at session start
        logger.info(
            f"🔧 Mic state check: allow_mic_input={self.allow_mic_input}, "
            f"session_active={self.session_active.is_set()}, "
            f"playback_done_event={'SET' if audio.playback_done_event.is_set() else 'CLEAR (waiting for wake-up)'}, "
            f"TEXT_ONLY_MODE={TEXT_ONLY_MODE}",
            "🔧",
        )

        async with self.ws_lock:
            if self.ws is None:
                uri = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
                headers = {
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                }

                try:
                    self.ws = await websockets.asyncio.client.connect(
                        uri, additional_headers=headers
                    )
                    await self.ws.send(
                        json.dumps({
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "instructions": get_instructions_with_user_context(),
                                "tools": get_tools_for_current_mode(),
                                "audio": {
                                    "input": {
                                        "format": {"type": "audio/pcm", "rate": 24000},
                                        "turn_detection": {
                                            "type": "server_vad",
                                            **SERVER_VAD_PARAMS[TURN_EAGERNESS],
                                            "create_response": True,
                                            "interrupt_response": True,
                                        },
                                    },
                                    **(
                                        {
                                            "output": {
                                                "format": {
                                                    "type": "audio/pcm",
                                                    "rate": 24000,
                                                },
                                                "voice": persona_manager.get_current_persona_voice(),
                                                "speed": 1.0,
                                            }
                                        }
                                        if not TEXT_ONLY_MODE
                                        else {}
                                    ),
                                },
                            },
                        })
                    )

                    # Kickoff message (from MQTT say)
                    if self.kickoff_text:
                        if self.kickoff_kind == "prompt":
                            kickoff_payload = self.kickoff_text
                        elif self.kickoff_kind == "literal":
                            kickoff_payload = (
                                "Say the user's message **verbatim**, word for word, with no additions or reinterpretation.\n"
                                "Maintain personality, but do NOT rephrase or expand.\n\n"
                                f"Repeat this literal message sent via MQTT: {self.kickoff_text}"
                                "\n\n"
                                "After you finish speaking, call `follow_up_intent` once. "
                                "If the line is not a question and needs no reply, set expects_follow_up=false."
                            )
                        else:
                            kickoff_payload = self.kickoff_text

                        await self.ws.send(
                            json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": kickoff_payload}
                                    ],
                                },
                            })
                        )
                        await self.ws.send(json.dumps({"type": "response.create"}))

                except websockets.exceptions.ConnectionClosedError as e:
                    reason = getattr(e, "reason", str(e))
                    if "invalid_api_key" in reason:
                        await self._play_error_sound("noapikey", reason)
                    else:
                        await self._play_error_sound("error", reason)
                    return

                except socket.gaierror:
                    await self._play_error_sound(
                        "nowifi", "Network unreachable or DNS failed"
                    )
                    return

                except Exception as e:
                    await self._play_error_sound("error", str(e))
                    return

        if not TEXT_ONLY_MODE:
            audio.playback_done_event.clear()
            audio.ensure_playback_worker_started(CHUNK_MS)

        await self.run_stream()

    def mic_callback(self, indata, *_):
        # Check 1: Mic input allowed and session active
        if not self.allow_mic_input or not self.session_active.is_set():
            if not hasattr(self, '_logged_mic_blocked_1'):
                logger.warning(
                    f"🔇 Mic blocked: allow_mic_input={self.allow_mic_input}, "
                    f"session_active={self.session_active.is_set()}",
                    "⚠️",
                )
                self._logged_mic_blocked_1 = True
            return

        # Check 2: Wait for wake-up sound to finish
        if not TEXT_ONLY_MODE and not audio.playback_done_event.is_set():
            if not hasattr(self, '_logged_waiting_for_wakeup'):
                logger.info("🔇 Mic waiting for wake-up sound to finish...", "⏳")
                self._logged_waiting_for_wakeup = True
            return

        # Log once when mic data starts being sent after wake-up sound
        if not self._mic_data_started and not TEXT_ONLY_MODE:
            logger.info("Mic data now being sent (wake-up sound finished)", "🎤")
            self._mic_data_started = True

        samples = indata[:, 0]
        rms = np.sqrt(np.mean(np.square(samples.astype(np.float32))))
        self.last_rms = rms

        if DEBUG_MODE:
            print(f"\r🎙️ Mic Volume: {rms:.1f}", end="", flush=True)

        if rms > SILENCE_THRESHOLD:
            self.last_activity[0] = time.time()
            self.user_spoke_after_assistant = True

        audio.send_mic_audio(self.ws, samples, self.loop)

    async def run_stream(self):
        if not TEXT_ONLY_MODE and audio.playback_done_event.is_set():
            await asyncio.to_thread(audio.playback_done_event.wait)

        logger.info(
            "Mic stream active. Say something..."
            if not self.kickoff_text
            else "Announcing kickoff...",
            "🎙️" if not self.kickoff_text else "📣",
        )
        mqtt_publish(
            "billy/state", "listening" if not self.kickoff_text else "speaking"
        )

        try:
            # Auto-identify default user in background (non-blocking)
            asyncio.create_task(self._auto_identify_default_user())

            # Start mic immediately only for non-kickoff sessions
            if not self.kickoff_text:
                self._start_mic()

            async for message in self.ws:
                if not self.session_active.is_set():
                    print("🚪 Session marked as inactive, stopping stream loop.")
                    print()  # Add newline to end the mic volume display line
                    break
                data = json.loads(message)
                if DEBUG_MODE and (
                    DEBUG_MODE_INCLUDE_DELTA
                    or not (data.get("type") or "").endswith("delta")
                ):
                    logger.verbose(f"Raw message: {data}", "🔁")

                if data.get("type") in ("session.updated", "session_updated"):
                    self.session_initialized = True

                await self.handle_message(data)

        except Exception as e:
            logger.error(f"Error opening mic input: {e}")
            self.session_active.clear()

        finally:
            try:
                self._stop_mic()
                logger.info("Mic stream closed.", "🎙️")
            except Exception as e:
                logger.warning(f"Error while stopping mic: {e}")

            try:
                await self.post_response_handling()
            except Exception as e:
                logger.warning(f"Error in post_response_handling: {e}")

    async def handle_message(self, data):
        t = data.get("type") or ""

        if t == "response.created":
            self._on_response_created()
            return
        if t == "input_audio_buffer.speech_started":
            self._on_input_speech_started()
            return
        if t == "input_audio_buffer.speech_stopped":
            return
        if t in self.TRANSCRIPT_DONE_TYPES:
            self._on_transcript_done(data)
            return
        if t in self.AUDIO_OUT_TYPES:
            self._on_audio_out(data)
            return
        if t == "input_audio_buffer.committed":
            self.committed = True
            return
        if t in self.TRANSCRIPT_DELTA_TYPES and "delta" in data:
            self._on_transcript_delta(t, data)
            return
        if t == "response.function_call_arguments.delta":
            self._on_tool_args_delta(data)
            return
        if t == "response.function_call_arguments.done":
            await self._on_tool_args_done(data)
            return
        if t == "response.done":
            await self._on_response_done(data)
            return
        if t == "error":
            error: dict[str, Any] = data.get("error") or {}
            code = error.get("code", "error").lower()
            message = error.get("message", "Unknown error")
            code = "noapikey" if "invalid_api_key" in code else "error"
            logger.error(f"API Error ({code}): {message}")
            await self._play_error_sound(code, message)
            return
        # else: ignore unrecognized messages silently

    async def mic_timeout_checker(self):
        logger.info("Mic timeout checker active", "🛡️")
        last_tail_move = 0

        while self.session_active.is_set():
            if not self.mic_running:
                await asyncio.sleep(0.2)
                continue

            now = time.time()
            idle_seconds = now - max(self.last_activity[0], audio.last_played_time)
            timeout_offset = 2

            if idle_seconds - timeout_offset > 0.5:
                elapsed = idle_seconds - timeout_offset
                progress = min(elapsed / MIC_TIMEOUT_SECONDS, 1.0)
                bar_len = 20
                filled = int(bar_len * progress)
                bar = "█" * filled + "-" * (bar_len - filled)
                print(
                    f"\r👂 {MIC_TIMEOUT_SECONDS}s timeout: [{bar}] {elapsed:.1f}s "
                    f"| Mic Volume:: {self.last_rms:.4f} / Threshold: {SILENCE_THRESHOLD:.4f}",
                    end="",
                    flush=True,
                )

                if now - last_tail_move > 1.0:
                    move_tail_async(duration=0.2)
                    last_tail_move = now

                if elapsed > MIC_TIMEOUT_SECONDS:
                    logger.info(
                        f"No mic activity for {MIC_TIMEOUT_SECONDS}s. Ending input...",
                        "⏱️",
                    )
                    await self.stop_session()
                    break

            await asyncio.sleep(0.5)

    async def post_response_handling(self):
        logger.verbose(f"Full response: {self.full_response_text.strip()}", "🧠")

        if not self.session_active.is_set():
            print()  # Add newline to end the mic volume display line
            logger.info(
                "Session inactive after timeout or interruption. Not restarting.", "🚪"
            )
            mqtt_publish("billy/state", "idle")
            stop_all_motors()
            async with self.ws_lock:
                if self.ws:
                    await self.ws.close()
                    await self.ws.wait_closed()
                    self.ws = None
            return

        # Heuristic fallback (punctuation only)
        asked_question = self._wants_follow_up_heuristic()

        # Always log follow-up decision for debugging
        logger.info(
            f"Follow-up decision | mode={self.autofollowup}"
            f" | tool_expects={self.follow_up_expected}"
            f" | qmark={asked_question}"
            f" | had_speech={self._turn_had_speech}"
            f" | saw_follow_up_call={self._saw_follow_up_call}",
            "🧪",
        )

        if self.autofollowup == "always":
            wants_follow_up = True
        elif self.autofollowup == "never":
            wants_follow_up = False
        else:
            wants_follow_up = self.follow_up_expected or asked_question

        if not self._saw_follow_up_call:
            logger.warning(
                "follow_up_intent not called this turn; using heuristic instead."
            )

        if wants_follow_up:
            logger.info("Follow-up expected. Keeping session open.", "🔁")
            mqtt_publish("billy/state", "listening")
            await self._start_mic_after_playback()  # <-- changed
            self.user_spoke_after_assistant = False
            self.full_response_text = ""
            self.last_activity[0] = time.time()
            return

        logger.info("No follow-up. Ending session.", "🛑")
        mqtt_publish("billy/state", "idle")
        stop_all_motors()
        async with self.ws_lock:
            if self.ws:
                await self.ws.close()
                await self.ws.wait_closed()
                self.ws = None

    async def stop_session(self):
        logger.info("Stopping session...", "🛑")

        # Increment interaction count for current user at end of session
        user_manager.increment_current_user_interaction_count()

        self.session_active.clear()
        self._stop_mic()

        async with self.ws_lock:
            if self.ws:
                try:
                    await self.ws.close()
                    # Add timeout to prevent hanging
                    try:
                        await asyncio.wait_for(self.ws.wait_closed(), timeout=2.0)
                    except asyncio.TimeoutError:
                        logger.warning("Websocket close timeout, forcing cleanup")
                except Exception as e:
                    logger.warning(f"Error closing websocket: {e}")
                finally:
                    self.ws = None

    async def request_stop(self):
        logger.info("Stop requested via external signal.", "🛑")
        self.session_active.clear()

    async def _play_error_sound(self, code: str = "error", message: str | None = None):
        """
        Play an error sound based on the provided code.
        Example:
          - "error"     → sounds/error.wav
          - "nowifi"    → sounds/nowifi.wav
          - "noapikey"  → sounds/noapikey.wav
        """
        stop_all_motors()

        filename = f"{code}.wav"
        sound_path = os.path.join("sounds", filename)

        logger.error(f"Error ({code}): {message or 'No message'}")
        logger.info(f"Attempting to play {filename}...", "🔊")

        if os.path.exists(sound_path):
            await asyncio.to_thread(audio.enqueue_wav_to_playback, sound_path)
            await asyncio.to_thread(audio.playback_queue.join)
        else:
            logger.warning(f"{sound_path} not found, skipping audio playback.")

        await self.stop_session()

    async def _reload_persona_from_profile(self):
        """Reload the persona from the current user's profile to pick up web UI changes."""
        try:
            # Reload environment variables to get latest CURRENT_USER
            from dotenv import load_dotenv

            from .config import ENV_PATH

            load_dotenv(ENV_PATH, override=True)
            current_user_env = (
                os.getenv("CURRENT_USER", "").strip().strip("'\"").lower()
            )

            # Determine which profile to load persona from
            if current_user_env == "guest" or not current_user_env:
                # Guest mode - load guest profile's preferred persona
                guest_profile = user_manager.identify_user("guest", "high")
                if guest_profile:
                    preferred_persona = guest_profile.data['USER_INFO'].get(
                        'preferred_persona', 'default'
                    )
                    persona_manager.switch_persona(preferred_persona)
                    logger.info(f"🎭 Reloaded guest persona: {preferred_persona}", "🎭")
            else:
                # User mode - load user's preferred persona
                current_user = user_manager.get_current_user()
                if current_user:
                    preferred_persona = current_user.data['USER_INFO'].get(
                        'preferred_persona', 'default'
                    )
                    persona_manager.switch_persona(preferred_persona)
                    logger.info(f"🎭 Reloaded user persona: {preferred_persona}", "🎭")
        except Exception as e:
            logger.warning(f"Failed to reload persona from profile: {e}", "⚠️")

    async def _auto_identify_default_user(self):
        """Automatically identify the current user if set and trigger a greeting."""
        try:
            # Reload environment variables to get latest values
            from dotenv import load_dotenv

            from .config import ENV_PATH

            load_dotenv(ENV_PATH, override=True)

            # Get fresh values from environment
            current_user_env = (
                os.getenv("CURRENT_USER", "").strip().strip("'\"")
            )  # Remove quotes and whitespace
            default_user_env = os.getenv("DEFAULT_USER", "guest").strip().strip("'\"")

            current_user = user_manager.get_current_user()

            # Use CURRENT_USER from .env if available, otherwise fall back to DEFAULT_USER
            user_to_identify = (
                current_user_env
                if current_user_env and current_user_env.lower() != "guest"
                else default_user_env
            )

            logger.info(
                f"Auto-identify check: CURRENT_USER='{current_user_env}', DEFAULT_USER='{default_user_env}', current_user={current_user.name if current_user else None}",
                "👤",
            )

            # Handle guest mode explicitly
            if current_user_env and current_user_env.lower() == "guest":
                if current_user:
                    # Clear current user for guest mode
                    logger.info("Switching to guest mode - clearing current user", "👤")
                    user_manager.clear_current_user()
                return  # Don't identify or greet anyone in guest mode

            # If we have a user to identify, ensure they're loaded (but don't greet yet)
            if user_to_identify and user_to_identify.lower() != "guest":
                if (
                    not current_user
                    or current_user.name.lower() != user_to_identify.lower()
                ):
                    # Load the user profile silently (no greeting)
                    logger.info(f"Auto-loading user profile: {user_to_identify}", "👤")
                    await self._load_user_profile_silently(user_to_identify)
                else:
                    # User is already loaded, no greeting needed
                    logger.info(f"User already loaded: {current_user.name}", "👤")
            else:
                logger.info(
                    f"No default user set or guest mode: DEFAULT_USER='{default_user_env}'",
                    "👤",
                )
        except Exception as e:
            logger.warning(f"Failed to auto-identify default user: {e}", "⚠️")

    async def _load_user_profile_silently(self, user_name):
        """Load a user profile without sending a greeting."""
        try:
            # Load or create profile
            profile = user_manager.identify_user(user_name, "high")
            if profile:
                # Save current user to .env file
                await self._save_current_user_to_env(profile.name)

                # Switch to user's preferred persona
                await self._switch_to_user_persona(profile)

                # Update session with user context (but no greeting)
                await self._update_session_with_user_context()

                logger.info(f"Silently loaded profile for {profile.name}", "👤")
            else:
                logger.warning(f"Failed to load profile for {user_name}", "⚠️")
        except Exception as e:
            logger.warning(f"Failed to load user profile silently: {e}", "⚠️")

    async def _send_user_greeting(self, profile, call_id: str | None = None):
        """Send a personalized greeting to the current user."""
        try:
            # Generate greeting context for Billy's personality to use
            greeting_context = self._generate_dynamic_greeting(profile)

            logger.info(
                f"Sending greeting context to {profile.name}: {greeting_context}", "👤"
            )

            # Send function_call_output to close the identify_user function
            # Then send a user message to prompt Billy to speak (same pattern as smart_home_command)
            if call_id:
                logger.info(
                    f"🔧 Sending function_call_output for identify_user (call_id={call_id})",
                    "🔧",
                )

                await self._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({
                            "status": "success",
                            "user": profile.name,
                            "message": f"Identified and loaded profile for {profile.name}",
                        }),
                    },
                })
                # Small delay to ensure function output is processed
                await asyncio.sleep(0.1)

            # Now send a user message that prompts Billy to speak
            # This follows the same pattern as smart_home_command
            time_info = f"{greeting_context['current_time']}"
            recency_info = greeting_context.get('time_since_last_seen', 'recently')

            if greeting_context['is_first_meeting']:
                # System instruction for greeting - prefix with [SYSTEM:] to prevent memory storage
                greeting_prompt = (
                    f"[SYSTEM: Speak your greeting now to welcome the new user]"
                )
            else:
                # System instruction for greeting - prefix with [SYSTEM:] to prevent memory storage
                greeting_prompt = f"[SYSTEM: Speak your greeting now to acknowledge the returning user]"

            # Send the greeting prompt as a user message
            logger.info(f"🔧 Sending greeting prompt: {greeting_prompt}", "🔧")
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": greeting_prompt}],
                },
            })

            # IMPORTANT: Unlike other function calls, greetings happen AFTER a response is complete
            # OpenAI won't auto-start a new response, so we must explicitly trigger it
            logger.info(
                "🔧 Triggering response.create for greeting (response already complete)",
                "🔧",
            )
            await self._ws_send_json({"type": "response.create"})
            logger.info("🔧 response.create sent successfully", "🔧")

            logger.info(
                f"Greeting context sent and audio triggered for {profile.name}", "👤"
            )
        except Exception as e:
            logger.warning(f"Failed to send user greeting: {e}", "⚠️")

    def _generate_dynamic_greeting(self, profile):
        """Generate a dynamic greeting based on user profile and Billy's personality."""
        from datetime import datetime

        interaction_count = int(profile.data['USER_INFO'].get('interaction_count', '0'))
        preferred_persona = profile.data['USER_INFO'].get(
            'preferred_persona', 'default'
        )
        last_seen = profile.data['USER_INFO'].get('last_seen', '')

        # Get current time for time-based context (uses Pi's local time)
        current_time = datetime.now()
        current_hour = current_time.hour

        # Determine time of day
        if 5 <= current_hour < 12:
            time_period = 'morning'
        elif 12 <= current_hour < 17:
            time_period = 'afternoon'
        elif 17 <= current_hour < 22:
            time_period = 'evening'
        else:
            time_period = 'night'

        # Calculate recency for more natural greetings
        recency = "recent"
        time_since_last_seen = None
        if last_seen:
            try:
                last_seen_time = datetime.fromisoformat(
                    last_seen.replace('Z', '+00:00')
                )
                time_diff = current_time - last_seen_time

                if time_diff.days > 7:
                    recency = "long_time"
                    time_since_last_seen = f"{time_diff.days} days"
                elif time_diff.days > 1:
                    recency = "few_days"
                    time_since_last_seen = f"{time_diff.days} days"
                elif time_diff.hours > 12:
                    recency = "yesterday"
                    time_since_last_seen = "yesterday"
                elif time_diff.hours > 2:
                    recency = "earlier"
                    time_since_last_seen = f"{time_diff.hours} hours"
                else:
                    recency = "recent"
                    time_since_last_seen = "recently"
            except Exception:
                recency = "recent"

        # Let Billy's personality generate the greeting naturally
        # This will be handled by the AI model with these context variables
        context = {
            "user_name": profile.name,
            "is_first_meeting": interaction_count == 0,
            "time_of_day": time_period,
            "current_time": current_time.strftime("%I:%M %p"),  # e.g., "03:45 PM"
            "day_of_week": current_time.strftime("%A"),  # e.g., "Saturday"
            "date": current_time.strftime("%B %d"),  # e.g., "November 02"
            "recency": recency,
            "interaction_count": interaction_count,
            "preferred_persona": preferred_persona,
        }

        # Add time since last seen if available
        if time_since_last_seen:
            context["time_since_last_seen"] = time_since_last_seen

        return context

    async def _handle_identify_user(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle user identification via tool calling."""
        args = json.loads(raw_args or "{}")
        name = args.get("name", "").strip().title()
        confidence = args.get("confidence", "medium")
        context = args.get("context", "")

        # Log the function call details in verbose mode
        logger.verbose(
            f"🔧 identify_user function called: name='{name}', confidence='{confidence}', context='{context}'",
            "🔧",
        )

        if not name:
            return

        # Check if we're uncertain about spelling
        if confidence == "low":
            await self._ask_for_spelling_confirmation(name)
            return

        # Handle "I am not X" scenarios
        current_user = user_manager.get_current_user()
        if (
            current_user
            and context
            and ("not" in context.lower() or "am not" in context.lower())
        ):
            # Someone is saying they're not the current user
            logger.info(
                f"User says they're not {current_user.name}, asking for their name",
                "👤",
            )
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"I understand you're not {current_user.name}. Who are you then? Please tell me your name so I can switch to your profile. If you don't want to say your name, I'll switch to guest mode.",
                        }
                    ],
                },
            })
            # Set a flag to track this situation
            self._waiting_for_name_after_denial = True
            return

        # Load or create profile
        profile = user_manager.identify_user(name, confidence)
        if profile:
            # Clear the waiting flag since we got a name
            self._waiting_for_name_after_denial = False

            # Save current user to .env file
            await self._save_current_user_to_env(profile.name)

            # Switch to user's preferred persona
            await self._switch_to_user_persona(profile)

            # Profile loaded - trigger a response with user context
            await self._update_session_with_user_context()

            # Only send greeting if user is actually introducing themselves
            # Don't greet for auto-loading contexts like "current user" or "default user"
            if context not in ["current user", "default user"]:
                await self._send_user_greeting(profile, call_id)
            else:
                logger.info(
                    f"Profile loaded for {profile.name} but no greeting sent (auto-load context)",
                    "👤",
                )
        elif self._waiting_for_name_after_denial:
            # User didn't provide a valid name after saying they're not the current user
            # Fall back to guest mode
            logger.info(
                "User didn't provide a valid name, switching to guest mode", "👤"
            )
            user_manager.clear_current_user()
            await self._save_current_user_to_env("guest")
            self._waiting_for_name_after_denial = False

            # Send a message acknowledging the switch to guest mode
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "No problem! I've switched to guest mode. You can always tell me your name later if you'd like a personalized experience.",
                        }
                    ],
                },
            })

    async def _handle_store_memory(
        self, raw_args: str | None, call_id: str | None = None
    ):
        """Handle memory storage via tool calling."""
        current_user = user_manager.get_current_user()
        if not current_user:
            logger.warning("🔧 store_memory: No current user found", "🔧")
            return

        args = json.loads(raw_args or "{}")
        logger.info(f"🔧 store_memory called with raw_args: {raw_args}", "🔧")
        logger.verbose(f"🔧 store_memory parsed args: {args}", "🔧")

        # Handle malformed memory data
        memory = args.get("memory", "")
        if isinstance(memory, dict):
            # If memory is a dict, try to extract the actual memory text
            memory = memory.get("fact", str(memory))
            logger.warning(
                f"🔧 store_memory: Memory was a dict, extracted: {memory}", "🔧"
            )

        importance = args.get("importance", "medium")
        category = args.get("category", "fact")

        if not memory:
            logger.warning(f"🔧 store_memory: No memory provided in args: {args}", "🔧")
            return

        # Log the function call details in verbose mode
        logger.verbose(
            f"🔧 store_memory function called: memory='{memory}', importance='{importance}', category='{category}'",
            "🔧",
        )

        current_user.add_memory(memory, importance, category)

        # Update session with new memory context
        await self._update_session_with_user_context()

        # Instead of just sending function output, send a user message that prompts Billy to acknowledge
        # This is similar to how the greeting works - it forces Billy to speak
        if call_id:
            # Send function output first
            logger.info(f"🔧 Sending function_call_output for store_memory", "🔧")
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps({"status": "success", "stored": memory}),
                },
            })

            # Wait a moment for the function output to be processed
            await asyncio.sleep(0.1)

            # Send a user message to prompt Billy to acknowledge the memory
            # OpenAI will automatically generate a response after function_call_output + user message
            logger.info(f"🔧 Sending prompt message to acknowledge memory", "🔧")
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[System: Memory stored. Briefly acknowledge storing '{memory}' and continue the conversation naturally.]",
                        }
                    ],
                },
            })
            # No need to manually call response.create - OpenAI handles it automatically

    async def _handle_manage_profile(self, raw_args: str | None):
        """Handle profile management via tool calling."""
        args = json.loads(raw_args or "{}")
        action = args.get("action", "")

        if action == "switch_persona":
            current_user = user_manager.get_current_user()
            if current_user:
                new_persona = args.get("preferred_persona", "default")

                # Validate persona exists
                available_personas = persona_manager.get_available_personas()
                if new_persona not in available_personas:
                    await self._ws_send_json({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": f"Sorry, I don't have a '{new_persona}' persona. Available personas: {', '.join(available_personas)}",
                                }
                            ],
                        },
                    })
                    return

                # Check if voice change requires session restart
                if self._check_voice_change(new_persona):
                    # Voice change requires session restart
                    await self._restart_session_for_voice_change(new_persona)
                    return

                # Set user's preferred persona
                current_user.set_preferred_persona(new_persona)

                # Switch persona manager to new persona
                persona_manager.switch_persona(new_persona)

                # Update session with new persona context
                await self._update_session_with_user_context()

                # Notify frontend of persona change for UI update
                await self._notify_persona_change(new_persona)

                # Get persona description for response
                persona_data = persona_manager.get_current_persona_data()
                persona_desc = (
                    persona_data.get("meta", {}).get("description", new_persona)
                    if persona_data
                    else new_persona
                )

                await self._ws_send_json({
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

    async def _handle_switch_persona(self, raw_args: str | None):
        """Handle persona switching mid-session."""
        args = json.loads(raw_args or "{}")
        persona_name = args.get("persona", "")
        reason = args.get("reason", "")

        # Log the function call details in verbose mode
        logger.verbose(
            f"🔧 switch_persona function called: persona='{persona_name}', reason='{reason}'",
            "🔧",
        )

        if not persona_name:
            return

        try:
            # Validate persona exists
            available_personas = persona_manager.get_available_personas()
            persona_names = [p["name"] for p in available_personas]

            if persona_name not in persona_names:
                await self._ws_send_json({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": f"Sorry, I don't have a '{persona_name}' persona. Available personas: {', '.join(persona_names)}",
                            }
                        ],
                    },
                })
                return

            # Switch persona
            persona_manager.switch_persona(persona_name)

            # Update session with new persona context
            await self._update_session_with_user_context()

            # Get persona description for response
            persona_data = persona_manager.get_current_persona_data()
            persona_desc = (
                persona_data.get("meta", {}).get("description", persona_name)
                if persona_data
                else persona_name
            )

            # Create acknowledgment message
            if reason:
                message = f"Right then! Switching to {persona_desc} mode. {reason}"
            else:
                message = f"Alright, switching to {persona_desc} mode now!"

            await self._ws_send_json({
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
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Sorry, couldn't switch personas right now. Something went wrong!",
                        }
                    ],
                },
            })

    async def _ask_for_spelling_confirmation(self, name: str):
        """Ask user to confirm name spelling."""
        response = f"I think I heard '{name}' - is that spelled correctly? Please say 'yes' or spell it out for me."
        await self._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": response}],
            },
        })

    async def _save_current_user_to_env(self, user_name: str):
        """Save the current user to the .env file."""
        try:
            from dotenv import set_key

            from .config import ENV_PATH

            set_key(ENV_PATH, "CURRENT_USER", user_name.lower(), quote_mode='never')
            logger.info(f"Saved current user to .env: {user_name}", "👤")
        except Exception as e:
            logger.warning(f"Failed to save current user to .env: {e}")

    async def _switch_to_user_persona(self, profile):
        """Switch to the user's preferred persona."""
        try:
            preferred_persona = profile.data['USER_INFO'].get(
                'preferred_persona', 'default'
            )
            persona_manager.switch_persona(preferred_persona)
            logger.info(
                f"Switched to user's preferred persona: {preferred_persona}", "🎭"
            )
        except Exception as e:
            logger.warning(f"Failed to switch to user persona: {e}")

    async def _update_session_with_user_context(self):
        """Update the session with current user context."""
        if not self.ws:
            return

        try:
            # Only update instructions, not voice (voice changes cause API errors during active conversations)
            await self._ws_send_json({
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": get_instructions_with_user_context(),
                },
            })
            logger.info("Updated session with user context", "👤")
        except Exception as e:
            logger.warning(f"Failed to update session with user context: {e}")

    async def _notify_persona_change(self, persona_name: str):
        """Notify frontend of persona change for UI updates."""
        try:
            # Send a custom message to the frontend to update the UI
            await self._ws_send_json({
                "type": "persona_changed",
                "persona": persona_name,
                "timestamp": datetime.now().isoformat(),
            })
            logger.info(f"Notified frontend of persona change to {persona_name}", "🎭")
        except Exception as e:
            logger.warning(f"Failed to notify frontend of persona change: {e}")

    def _check_voice_change(self, new_persona: str) -> bool:
        """Check if the new persona has a different voice that requires session restart."""
        try:
            current_voice = persona_manager.get_current_persona_voice()
            new_voice = persona_manager.get_persona_voice(new_persona)
            return current_voice != new_voice
        except Exception as e:
            logger.warning(f"Failed to check voice change: {e}")
            return False

    async def _restart_session_for_voice_change(self, new_persona: str):
        """Gracefully restart the session when voice changes."""
        try:
            logger.info(f"Voice changed, restarting session for {new_persona}", "🔄")

            # Send a message to the user explaining the restart
            await self._ws_send_json({
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"Switching to {new_persona} persona. This requires a quick restart to change my voice...",
                        }
                    ],
                },
            })

            # Close the current session gracefully
            await self._ws_send_json({"type": "session.close"})

            # Trigger a new session start
            from .button import start_new_session

            await asyncio.sleep(1)  # Brief pause for graceful shutdown
            start_new_session()

        except Exception as e:
            logger.error(f"Failed to restart session for voice change: {e}")

    async def _greet_user(self, profile, context: str = ""):
        """Greet a user based on their profile."""
        name = profile.name
        interaction_count = int(profile.data['USER_INFO'].get('interaction_count', '0'))

        if interaction_count == 0:
            # New user
            response = f"Hey {name}! Nice to meet you! I'm Billy, your new AI fish friend. I'll remember you from now on!"
        else:
            # Returning user
            response = f"Hey {name}! Good to see you again! We've talked {interaction_count} times now."

        # Add greeting to conversation and trigger response
        await self._ws_send_json({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": response}],
            },
        })

        # Trigger audio generation for the greeting
        await self._ws_send_json({"type": "response.create"})
