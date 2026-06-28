import asyncio
import contextlib
import inspect
import json
import socket
import time
from typing import Any

import websockets.exceptions

from . import audio
from .config import (
    FOLLOW_UP_RETRY_LIMIT,
    MIC_TIMEOUT_SECONDS,
    REALTIME_AI_PROVIDER,
    RUN_MODE,
    SERVER_VAD_PARAMS,
    SILENCE_THRESHOLD,
    TEXT_ONLY_MODE,
    TURN_EAGERNESS,
    is_conversation_state_enabled,
)
from .logger import logger
from .movements import stop_all_motors
from .persona_manager import persona_manager
from .profile_manager import user_manager
from .realtime_ai_provider import voice_provider_registry


def _strip_tools_section_for_provider(instructions: str, provider_name: str) -> str:
    """Hook for provider-specific prompt shaping."""
    _ = provider_name
    return instructions


def get_instructions_with_user_context(provider_name: str | None = None):
    """Generate instructions with current user context and persona if available."""
    import os

    from dotenv import load_dotenv

    from .config import ENV_PATH
    from .session import InstructionContext, instruction_builder

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")
    current_user = user_manager.get_current_user()

    if current_user_env and current_user_env.lower() == "guest":
        mode = "guest"
    elif current_user:
        mode = "user"
    else:
        mode = "guest"

    context = InstructionContext(
        mode=mode,
        persona_name=persona_manager.current_persona,
        user_profile=current_user,
    )

    instructions = instruction_builder.build(context)
    provider_name = (provider_name or REALTIME_AI_PROVIDER or "").strip().lower()
    return _strip_tools_section_for_provider(instructions, provider_name)


def get_tools_for_current_mode(provider_name: str | None = None):
    """Get tools list based on current mode (guest vs user mode)."""
    import os

    from dotenv import load_dotenv

    from .config import ENV_PATH
    from .session import tool_manager

    load_dotenv(ENV_PATH, override=True)
    current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")

    logger.verbose(
        f"get_tools_for_current_mode: CURRENT_USER='{current_user_env}'", "🔧"
    )

    if current_user_env and current_user_env.lower() == "guest":
        mode = "guest"
    else:
        mode = "user"

    tools = tool_manager.get_tools(mode)

    # Add provider-specific tools
    provider_tools = voice_provider_registry.get_provider().get_provider_tools()
    tools.extend(provider_tools)

    return tools


class BillySession:
    def __init__(
        self,
        interrupt_event=None,
        *,
        conversation_provider=None,
        kickoff_text: str | None = None,
        kickoff_kind: str = "literal",
        kickoff_to_interactive: bool = False,
        autofollowup: str = "auto",
    ):
        self.realtime_ai_provider = (
            conversation_provider
            or voice_provider_registry.get_provider(REALTIME_AI_PROVIDER)
        )
        self.ws = None
        self.ws_lock: asyncio.Lock = asyncio.Lock()
        self.loop = None
        self.last_activity = [time.time()]
        self.session_active = asyncio.Event()
        self.interrupt_event = interrupt_event or asyncio.Event()

        # Track session initialization
        self.session_initialized = False
        self.run_mode = RUN_MODE
        self._stopping = False
        self._interaction_count_recorded = False

        # Kickoff (MQTT say)
        self.kickoff_text = (kickoff_text or "").strip() or None
        self.kickoff_kind = kickoff_kind
        self.kickoff_to_interactive = kickoff_to_interactive
        self.kickoff_first_turn_done = False

        # Follow-up
        self.autofollowup = autofollowup
        self.session_intent = self._resolve_session_intent()

        # Tool args buffer (for streamed args)
        self._tool_args_buffer: dict[str, str] = {}

        self._logged_user_transcript_item_ids: set[str] = set()

        # Initialize handlers
        from .session import (
            AudioHandler,
            ErrorHandler,
            FunctionHandler,
            MicManagerWrapper,
            PersonaHandler,
            SessionState,
            UserHandler,
        )

        self.function_handler = FunctionHandler(self)
        self.audio_handler = AudioHandler(self)
        self.state = SessionState(self)
        self.user_handler = UserHandler(self)
        self.persona_handler = PersonaHandler(self)
        self.mic_manager = MicManagerWrapper(self)
        self.error_handler = ErrorHandler(self)

    def _resolve_session_intent(self) -> str:
        """Classify session behavior for follow-up policy."""
        # MQTT announce-only sessions should be one-and-done.
        if (
            self.autofollowup == "never"
            and self.kickoff_text
            and not self.kickoff_to_interactive
        ):
            return "announcement"
        return "interactive"

    def is_assistant_turn(self) -> bool:
        return self.state.is_assistant_turn()

    def is_user_turn(self) -> bool:
        return self.state.is_user_turn()

    def _set_listening_state(self):
        self.state.set_listening_state()

    def _set_speaking_state(self):
        self.state.set_speaking_state()

    def _set_idle_state(self):
        self.state.set_idle_state()

    # ---- Websocket helpers ---------------------------------------------
    async def _ws_send_json(self, payload: dict[str, Any]):
        """Send a JSON payload over the session websocket with locking.

        This method is a small convenience to avoid repeating the lock and
        json.dumps boilerplate across the codebase.
        """
        lock_acquired = False
        try:
            await asyncio.wait_for(self.ws_lock.acquire(), timeout=2.0)
            lock_acquired = True
            if self.ws is not None:
                await self.realtime_ai_provider.send_message(self.ws, payload)
        except asyncio.TimeoutError:
            logger.warning(
                "Timed out acquiring ws_lock for send; dropping payload", "⚠️"
            )
        finally:
            if lock_acquired:
                self.ws_lock.release()

    async def _close_ws(self, timeout: float = 1.0):
        lock_acquired = False
        ws_to_close = None
        try:
            await asyncio.wait_for(self.ws_lock.acquire(), timeout=2.0)
            lock_acquired = True
            ws_to_close = self.ws
            if not ws_to_close:
                return
        except asyncio.TimeoutError:
            # Lock contention during shutdown should not wedge session teardown.
            ws_to_close = self.ws
            logger.warning(
                "Timed out acquiring ws_lock during close; forcing websocket close without lock",
                "⚠️",
            )

        if not ws_to_close:
            return

        try:
            await asyncio.wait_for(ws_to_close.close(), timeout=max(0.5, timeout))
        except asyncio.TimeoutError:
            # Close timeout is common during teardown races; detach quietly.
            logger.info("Websocket close timed out during shutdown; continuing.", "⏱️")
        except websockets.exceptions.ConnectionClosed:
            # Already closed by remote/local side.
            pass
        except Exception as e:
            logger.warning(f"Error closing websocket ({type(e).__name__}): {e!r}", "⚠️")
        finally:
            if self.ws is ws_to_close:
                self.ws = None
            if lock_acquired:
                self.ws_lock.release()

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
    USER_TRANSCRIPT_TYPES = {
        "conversation.item.input_audio_transcription.completed",
    }

    # ---- Private handlers -----------------------------------------------
    def _on_response_created(self):
        self.state.on_response_created()
        # Clear buffered audio only for OpenAI. This was added to prevent echo
        # there, but xAI has different realtime behavior and should not get this
        # extra client event.
        if self.realtime_ai_provider.get_provider_name() == "openai":
            asyncio.create_task(self._clear_input_audio_buffer())

    async def _clear_input_audio_buffer(self):
        """Clear OpenAI's input audio buffer to prevent echo."""
        try:
            await self._ws_send_json({"type": "input_audio_buffer.clear"})
            logger.verbose("Cleared input audio buffer to prevent echo", "🧹")
        except Exception as e:
            logger.warning(f"Failed to clear audio buffer: {e}")

    def _on_input_speech_started(self):
        self.state.on_input_speech_started()

    def _on_input_speech_stopped(self):
        self.state.on_input_speech_stopped()

    def _on_conversation_item_done(self, data: dict[str, Any]):
        self.state.on_conversation_item_done(data)
        self._log_user_transcript_from_item(data)

    def _log_user_transcript_from_item(self, data: dict[str, Any]):
        """Log finalized user transcript when present in conversation.item.done."""
        item = data.get("item") or {}
        if item.get("role") != "user":
            return

        item_id = item.get("id")
        content = item.get("content") or []
        transcript_parts: list[str] = []
        for part in content:
            transcript = (part.get("transcript") or "").strip()
            if transcript:
                transcript_parts.append(transcript)

        transcript = " ".join(transcript_parts).strip()
        if not transcript:
            return
        if item_id and item_id in self._logged_user_transcript_item_ids:
            return

        logger.info(f"User said: {transcript!r}", "🗣️")
        if item_id:
            self._logged_user_transcript_item_ids.add(item_id)

    def _on_user_transcript_done(self, data: dict[str, Any]):
        """Handle direct user transcription completion events."""
        transcript = (data.get("transcript") or "").strip()
        item_id = data.get("item_id")
        if transcript:
            # Meaningful user reply received: clear follow-up retry counter.
            self.state.mark_user_turn_meaningful()
            if not item_id or item_id not in self._logged_user_transcript_item_ids:
                logger.info(f"User said: {transcript!r}", "🗣️")
            if item_id:
                self._logged_user_transcript_item_ids.add(item_id)
            return

        logger.verbose(
            f"User transcription completed but empty (item_id={item_id!r})",
            "ℹ️",
        )
        self.state.on_transcript_done(data)

    def _on_audio_out(self, data: dict[str, Any]):
        self.audio_handler.on_audio_delta(data)

    def _on_transcript_done(self, data: dict[str, Any]):
        self.state.on_transcript_done(data)

    def _on_transcript_delta(self, t: str, data: dict[str, Any]):
        delta = data.get("delta", "")
        self.state.on_transcript_delta(t, delta)

    def _on_tool_args_delta(self, data: dict[str, Any]):
        name = data.get("name")
        if name:
            self._tool_args_buffer.setdefault(name, "")
            self._tool_args_buffer[name] += data.get("arguments", "")

    async def _on_tool_args_done(self, data: dict[str, Any]):
        name = data.get("name")
        raw_args = data.get("arguments")
        call_id = data.get("call_id")
        if not raw_args and name:
            raw_args = self._tool_args_buffer.pop(name, "{}")

        # Delegate to function handler
        await self.function_handler.handle(name, raw_args, call_id)

    async def _on_response_done(self, data: dict[str, Any]):
        if self.state._skip_post_response_once:
            response = data.get("response") or {}
            raw_status_details = response.get("status_details")
            status_details = (
                raw_status_details if isinstance(raw_status_details, dict) else {}
            )
            cancelled_by_client = (
                status_details.get("type") == "cancelled"
                and status_details.get("reason") == "client_cancelled"
            )
            output_items = response.get("output") or []
            has_meaningful_output = bool(
                self.state._turn_had_speech
                or self.state._saw_follow_up_call
                or any(
                    (item.get("type") == "message" and (item.get("content") or []))
                    or item.get("type") == "function_call"
                    for item in output_items
                )
            )

            # Manual turn-handoff interrupts intentionally cancel the current response.
            # Even if partial transcript/audio exists, post-response follow-up logic
            # should be skipped because the mic was already reopened explicitly.
            if cancelled_by_client:
                self.state._skip_post_response_once = False
                self.state.allow_mic_input = True
                self.state.assistant_speaking = False
                self.last_activity[0] = time.time()
                logger.info(
                    "Skipping post-response handling for client-cancelled interrupt; mic handoff already active.",
                    "🔇",
                )
                return

            # We only skip post-response handling when the cancelled turn truly produced
            # no assistant output. If output exists, process it normally to avoid getting
            # stuck in a half-open "listening/retry" state.
            if not has_meaningful_output:
                self.state._skip_post_response_once = False
                self.state.allow_mic_input = True
                self.state.assistant_speaking = False
                self.last_activity[0] = time.time()
                logger.info(
                    "Skipping post-response handling for cancelled short/noise turn; staying in listening mode.",
                    "🔇",
                )
                return

            logger.info(
                "Skip flag was set, but assistant output was present; continuing normal post-response handling.",
                "🔄",
            )
            self.state._skip_post_response_once = False

        response = data.get("response") or {}
        raw_status_details = response.get("status_details")
        status_details = (
            raw_status_details if isinstance(raw_status_details, dict) else {}
        )
        error = status_details.get("error")
        if error:
            error_type = (error.get("type") or error.get("code") or "error").lower()
            error_message = error.get("message", "Unknown error")
            logger.error(f"OpenAI API Error [{error_type}]: {error_message}")
            mapped_code = "noapikey" if "invalid_api_key" in error_type else "error"
            await self.error_handler.play_error_sound(mapped_code, error_message)
            return
        logger.success("Assistant response complete.", "✿")

        if not TEXT_ONLY_MODE:
            await self.audio_handler.wait_for_playback_complete()
            self.audio_handler.save_response_audio()
            self.audio_handler.clear_buffer()
            self.audio_handler.signal_playback_done()
            self.last_activity[0] = time.time()

        # Only mark assistant turn complete after local playback has finished.
        self.state.on_response_done()

        # Check if conversation_state was called - if not, log warning
        # (heuristic will be used in _post_response_handling)
        if (
            is_conversation_state_enabled()
            and not self.state._saw_follow_up_call
            and self.state._turn_had_speech
        ):
            logger.warning(
                "⚠️ conversation_state was NOT called by the model - using heuristic fallback"
            )
            heuristic_result = self.state.wants_follow_up_heuristic()
            logger.verbose(
                f"Using heuristic fallback: follow_up_expected={heuristic_result}",
                "🔍",
            )

        # Kickoff follow-up switch
        if self.kickoff_text and not self.kickoff_first_turn_done:
            if self.state._turn_had_speech:
                self.kickoff_first_turn_done = True
                if self.kickoff_to_interactive:
                    logger.info(
                        "Kickoff complete — switching to interactive mode.", "🔁"
                    )
                    self.mic_manager.start()
                    self.state._saw_follow_up_call = False
                    self.state._last_user_turn_meaningful = False
                    self.state.full_response_text = ""
                    self.last_activity[0] = time.time()
                    return
                if self.autofollowup == "auto":
                    asked_question = self.state.wants_follow_up_heuristic()
                    wants_follow_up, kickoff_source = self._decide_follow_up_policy(
                        asked_question=asked_question,
                        # Kickoff has no prior user turn in this session; this
                        # flag is only relevant for retry accounting.
                        last_user_turn_meaningful=False,
                    )
                    logger.info(
                        f"Kickoff follow-up decision | wants={wants_follow_up} | source={kickoff_source}",
                        "🧭",
                    )
                    if wants_follow_up:
                        logger.info("Auto follow-up detected — opening mic.", "🔁")
                        opened = await self.mic_manager.start_after_playback()
                        if not opened:
                            logger.error(
                                "Failed to reopen mic for kickoff follow-up. Ending session.",
                                "❌",
                            )
                            await self.stop_session(
                                reason="kickoff_followup_open_failed"
                            )
                            return
                        self.last_activity[0] = time.time()
                        # Kickoff path already handled follow-up reopening.
                        # Do not also run generic post-response handling.
                        self.state._saw_follow_up_call = False
                        self.state._last_user_turn_meaningful = False
                        self.state.full_response_text = ""
                        return
                    logger.info(
                        "Kickoff complete — no follow-up needed. Closing session.",
                        "🔁",
                    )
                    await self.stop_session(reason="kickoff_no_followup")
                    return
            else:
                logger.verbose(
                    "Kickoff turn ended with no speech (tool-only). Waiting for next turn.",
                    "ℹ️",
                )

        if self.run_mode == "dory":
            logger.info("Dory mode active. Ending session after single response.", "🎣")
            await self.stop_session(reason="run_mode_dory")
            return

        # Post-response handling: decide whether to reopen mic or end session
        await self._post_response_handling()

    def _decide_follow_up_policy(
        self, *, asked_question: bool, last_user_turn_meaningful: bool
    ) -> tuple[bool, str]:
        """Centralized follow-up policy decision.

        Returns:
            (wants_follow_up, source)
            source is a stable reason label for logging/debugging.
        """
        if self.autofollowup == "always":
            return True, "mode_always"
        if self.autofollowup == "never":
            return False, "mode_never"

        # Announcement sessions (MQTT say one-shot) should never reopen mic.
        if self.session_intent == "announcement":
            return False, "announcement"

        # Honor explicit conversation_state whenever present.
        # This is especially important on models that reliably call the tool
        # (e.g. realtime-1.5), where rhetorical phrasing may contain '?' but
        # still not require a follow-up turn.
        if self.state._saw_follow_up_call:
            if last_user_turn_meaningful:
                return bool(self.state.follow_up_expected), "conversation_state"
            if self.state.follow_up_expected:
                return True, "conversation_state_low_conf"
            return False, "conversation_state_low_conf"

        # Enforce deterministic UX fallback for interactive sessions: if Billy
        # asked a question and no tool hint was provided, open follow-up.
        if self.session_intent == "interactive" and asked_question:
            return True, "interactive_question"

        # Fallback heuristic for models that skipped conversation_state.
        return asked_question, "heuristic"

    async def _post_response_handling(self):
        """Handle post-response logic: reopen mic or end session."""
        last_user_turn_meaningful = self.state._last_user_turn_meaningful
        if not last_user_turn_meaningful:
            total_chunks = int(self.state._last_committed_audio_chunks or 0)
            loud_chunks = int(self.state._last_committed_loud_audio_chunks or 0)
            peak_rms = float(self.state._last_committed_peak_rms or 0.0)
            had_server_speech = bool(self.state._last_committed_had_server_speech)
            soft_speech_floor = max(120.0, SILENCE_THRESHOLD * 0.15)
            audio_evidence_meaningful = (
                peak_rms >= soft_speech_floor
                or loud_chunks >= 2
                or (had_server_speech and total_chunks >= 8)
            )
            if audio_evidence_meaningful:
                last_user_turn_meaningful = True
                self.state._last_user_turn_meaningful = True
                logger.verbose(
                    (
                        "Promoting last user turn to meaningful from audio evidence "
                        f"(chunks={total_chunks}, loud={loud_chunks}, peak_rms={peak_rms:.1f}, "
                        f"server_speech={had_server_speech}, floor={soft_speech_floor:.1f})."
                    ),
                    "🎤",
                )
        if self.state.full_response_text.strip():
            print(
                f"📝 Transcript completed: \"{self.state.full_response_text.strip()}\""
            )
        logger.verbose(f"Full response: {self.state.full_response_text.strip()}", "🧠")

        # If a new response was triggered (greeting, HA command, etc), skip post-response handling
        if self.state._triggered_new_response:
            logger.info("New response triggered, skipping post-response handling", "🔄")
            return

        if not self.session_active.is_set():
            print()  # Add newline to end the mic volume display line
            logger.info(
                "Session inactive after timeout or interruption. Not restarting.", "🚪"
            )
            self._set_idle_state()
            stop_all_motors()
            await self._close_ws()
            return

        # If the model produced no spoken output this turn (tool-only/empty turn),
        # keep listening and avoid follow-up heuristics/retry accounting.
        if not self.state._turn_had_speech:
            logger.info(
                "No assistant speech this turn; staying in listening mode.",
                "🔇",
            )
            self.state._saw_follow_up_call = False
            self.state.full_response_text = ""
            self.state._last_user_turn_meaningful = False
            self.last_activity[0] = time.time()
            opened = await self.mic_manager.start_after_playback()
            if not opened:
                logger.error(
                    "Failed to reopen mic after tool-only turn. Ending session.",
                    "❌",
                )
                self._set_idle_state()
                stop_all_motors()
                await self._close_ws()
            return

        # Determine if follow-up is expected
        asked_question = self.state.wants_follow_up_heuristic()

        # Always log follow-up decision for debugging
        logger.info(
            f"Follow-up decision | mode={self.autofollowup}"
            f" | intent={self.session_intent}"
            f" | tool_expects={self.state.follow_up_expected}"
            f" | qmark={asked_question}"
            f" | had_speech={self.state._turn_had_speech}"
            f" | saw_follow_up_call={self.state._saw_follow_up_call}",
            "🧪",
        )

        wants_follow_up, follow_up_source = self._decide_follow_up_policy(
            asked_question=asked_question,
            last_user_turn_meaningful=last_user_turn_meaningful,
        )
        logger.info(
            f"Follow-up policy result | wants={wants_follow_up} | source={follow_up_source}",
            "🧭",
        )
        if follow_up_source == "interactive_question" and wants_follow_up:
            logger.info(
                "Interactive question detected; forcing follow-up window.", "🔁"
            )

        if is_conversation_state_enabled() and not self.state._saw_follow_up_call:
            logger.warning(
                "conversation_state not called this turn; using heuristic instead."
            )

        if wants_follow_up:
            # Retry budget is only for no-content/silence turns.
            if not last_user_turn_meaningful:
                had_server_speech = bool(self.state._last_committed_had_server_speech)
                loud_chunks = int(self.state._last_committed_loud_audio_chunks or 0)
                credible_server_speech = had_server_speech and loud_chunks >= 2
                if credible_server_speech:
                    logger.info(
                        "Follow-up detected credible server speech with low-confidence transcript; not consuming retry budget.",
                        "🔁",
                    )
                else:
                    if self.state.follow_up_retry_count >= FOLLOW_UP_RETRY_LIMIT:
                        logger.info(
                            f"Follow-up retry limit reached ({FOLLOW_UP_RETRY_LIMIT}). Ending session.",
                            "🛑",
                        )
                        self.state._saw_follow_up_call = False
                        self.state.follow_up_retry_count = 0
                        self.state._last_user_turn_meaningful = False
                        self._set_idle_state()
                        stop_all_motors()
                        await self._close_ws()
                        return

                    self.state.increment_follow_up_retry()
                    logger.info(
                        f"Follow-up expected after empty/noisy turn (source={follow_up_source}). "
                        f"Keeping session open (retry {self.state.follow_up_retry_count}/{FOLLOW_UP_RETRY_LIMIT}).",
                        "🔁",
                    )
            else:
                self.state.follow_up_retry_count = 0
                logger.info(
                    "Follow-up expected after meaningful user input. Keeping session open.",
                    "🔁",
                )
            # Reset the flag after using it
            self.state._saw_follow_up_call = False
            self.state._last_user_turn_meaningful = False
            opened = await self.mic_manager.start_after_playback()
            if not opened:
                logger.error(
                    "Failed to reopen mic for follow-up window. Ending session.",
                    "❌",
                )
                self.state.follow_up_retry_count = 0
                self._set_idle_state()
                stop_all_motors()
                await self._close_ws()
                return
            self.state.full_response_text = ""
            self.last_activity[0] = time.time()
            return

        # In interactive sessions, provide one final listen window only when the
        # user hasn't meaningfully engaged yet (e.g. first response without a question).
        # If the user already spoke and Billy finished without asking a question,
        # the conversation is naturally over — close the session.
        if (
            self.session_intent == "interactive"
            and self.autofollowup != "never"
            and not last_user_turn_meaningful
        ):
            logger.info(
                "No follow-up predicted and no prior meaningful user turn; opening one final listen window.",
                "🔁",
            )
            self.state._saw_follow_up_call = False
            self.state._last_user_turn_meaningful = False
            opened = await self.mic_manager.start_after_playback()
            if not opened:
                logger.error(
                    "Failed to reopen mic for final listen window. Ending session.",
                    "❌",
                )
                self.state.follow_up_retry_count = 0
                self._set_idle_state()
                stop_all_motors()
                await self._close_ws()
                return
            self.state.full_response_text = ""
            self.last_activity[0] = time.time()
            return

        logger.info("No follow-up. Ending session.", "🛑")
        # Reset the flag after using it
        self.state._saw_follow_up_call = False
        self.state.follow_up_retry_count = 0
        self.state._last_user_turn_meaningful = False
        self._set_idle_state()
        stop_all_motors()
        await self._close_ws()

    # ---- Mic helpers -------------------------------------------------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        logger.info("Session starting...", "⏱️")

        await self.persona_handler.reload_persona_from_profile()

        vad_params = SERVER_VAD_PARAMS[TURN_EAGERNESS]
        logger.info(f"🔧 VAD Parameters (eagerness={TURN_EAGERNESS}): {vad_params}")
        logger.info(
            f"🔧 Audio Config: SILENCE_THRESHOLD={SILENCE_THRESHOLD}, MIC_TIMEOUT_SECONDS={MIC_TIMEOUT_SECONDS}"
        )

        # Reset state
        self.audio_handler.clear_buffer()
        self.state.reset_for_new_session()
        self._logged_user_transcript_item_ids.clear()
        self.last_activity[0] = time.time()
        self.session_active.set()
        self._stopping = False
        self._interaction_count_recorded = False
        self._local_vad_active = False
        self._local_vad_hold_until = 0.0

        logger.info(
            f"🔧 Mic state check: allow_mic_input={self.state.allow_mic_input}, "
            f"session_active={self.session_active.is_set()}, "
            f"playback_done_event={'SET' if audio.playback_done_event.is_set() else 'CLEAR (waiting for wake-up)'}, "
            f"TEXT_ONLY_MODE={TEXT_ONLY_MODE}",
            "🔧",
        )

        async with self.ws_lock:
            if self.ws is None:
                try:
                    persona_voice = persona_manager.get_current_persona_voice()
                    provider_name = self.realtime_ai_provider.get_provider_name()
                    logger.info(
                        f"Using persona '{persona_manager.current_persona}' voice '{persona_voice}' for session startup",
                        "🎭",
                    )
                    self.ws = await self.realtime_ai_provider.connect(
                        instructions=get_instructions_with_user_context(provider_name),
                        tools=get_tools_for_current_mode(),
                        server_vad_params=SERVER_VAD_PARAMS[TURN_EAGERNESS],
                        interrupt_response=False,
                        text_only_mode=TEXT_ONLY_MODE,
                        voice=persona_voice,
                    )

                    # Kickoff message (from MQTT say)
                    if self.kickoff_text:
                        if self.kickoff_kind == "prompt":
                            kickoff_payload = self.kickoff_text
                        elif self.kickoff_kind == "literal":
                            follow_up_clause = (
                                "After you finish speaking, call `conversation_state` once. "
                                "If the line is not a question and needs no reply, set expects_follow_up=false."
                                if is_conversation_state_enabled()
                                else "After you finish speaking, end naturally. Do not include internal tool-call text."
                            )
                            kickoff_payload = (
                                "Speak EXACTLY the literal MQTT text below, verbatim, and nothing else.\n"
                                "Rules:\n"
                                "- Do not add commentary, style, jokes, or follow-up questions.\n"
                                "- Do not paraphrase or expand.\n"
                                "- Do not prepend or append any words.\n"
                                "- Only include quote characters if they are part of the literal message itself.\n\n"
                                f"Literal message: {self.kickoff_text}"
                                "\n\n"
                                f"{follow_up_clause}"
                            )
                        else:
                            kickoff_payload = self.kickoff_text

                        await self.realtime_ai_provider.send_message(
                            self.ws,
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": kickoff_payload}
                                    ],
                                },
                            },
                        )
                        await self.realtime_ai_provider.send_message(
                            self.ws, {"type": "response.create"}
                        )

                except websockets.exceptions.ConnectionClosedError as e:
                    reason = getattr(e, "reason", str(e))
                    if "invalid_api_key" in reason:
                        await self.error_handler.play_error_sound("noapikey", reason)
                    else:
                        await self.error_handler.play_error_sound("error", reason)
                    return

                except socket.gaierror:
                    await self.error_handler.play_error_sound(
                        "nowifi", "Network unreachable or DNS failed"
                    )
                    return

                except Exception as e:
                    await self.error_handler.play_error_sound("error", str(e))
                    return

        if not TEXT_ONLY_MODE:
            self.audio_handler.ensure_playback_worker()

        await self.run_stream()

    async def run_stream(self):
        if not TEXT_ONLY_MODE and not audio.playback_done_event.is_set():
            await asyncio.to_thread(audio.playback_done_event.wait)

        logger.info(
            "Mic stream active. Say something..."
            if not self.kickoff_text
            else "Announcing kickoff...",
            "🎙️" if not self.kickoff_text else "📣",
        )
        if self.kickoff_text:
            self._set_speaking_state()

        try:
            asyncio.create_task(self.user_handler.auto_identify_default_user())

            # Start mic immediately for normal interactive sessions.
            # Keep the session.updated fallback below in case startup races.
            if not self.kickoff_text:
                self.mic_manager.start()

            assert self.ws is not None
            ws = self.ws
            while True:
                if not self.session_active.is_set():
                    logger.verbose(
                        "Session marked inactive, stopping stream loop.", "🚪"
                    )
                    print()  # Add newline to end the mic volume display line
                    break

                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Periodic wake-up so stop/session_active changes are respected
                    # even if provider websocket is quiet.
                    continue
                except websockets.exceptions.ConnectionClosed:
                    if self.session_active.is_set():
                        logger.info("Websocket stream closed.", "🔌")
                    break

                data = json.loads(message)
                if not (data.get("type") or "").endswith("delta"):
                    logger.verbose(f"Raw message: {data}", "🔁")

                if data.get("type") in ("session.updated", "session_updated"):
                    self.session_initialized = True
                    # Fallback: start mic if it wasn't already started.
                    if not self.kickoff_text and not self.mic_manager.mic_running:
                        logger.info(
                            "🎵 Session initialized with VAD settings, starting mic",
                            "✅",
                        )
                        self.mic_manager.start()

                await self.handle_message(data)

        except Exception as e:
            if (
                isinstance(e, websockets.exceptions.ConnectionClosed)
                and not self.session_active.is_set()
            ):
                logger.info(f"Websocket closed during shutdown: {e}", "🔌")
            else:
                logger.error(f"Error opening mic input: {e}")
            self.session_active.clear()

        finally:
            try:
                self.mic_manager.stop()
                logger.info("Mic stream closed.", "🎙️")
            except Exception as e:
                logger.warning(f"Error while stopping mic: {e}")

    async def handle_message(self, data):
        t = data.get("type") or ""

        if t == "response.created":
            if self.state.should_ignore_short_response():
                self.state._skip_post_response_once = True
                with contextlib.suppress(Exception):
                    await self._ws_send_json({"type": "response.cancel"})
                self.state.allow_mic_input = True
                self.state.assistant_speaking = False
                self.last_activity[0] = time.time()
                logger.info(
                    "Cancelled response triggered by short audio turn; staying in listening mode.",
                    "🔇",
                )
                return
            self._on_response_created()
            return
        if t == "input_audio_buffer.speech_started":
            self._on_input_speech_started()
            return
        if t == "input_audio_buffer.speech_stopped":
            self._on_input_speech_stopped()
            return
        if t in self.TRANSCRIPT_DONE_TYPES:
            self._on_transcript_done(data)
            return
        if t in self.AUDIO_OUT_TYPES:
            self._on_audio_out(data)
            return
        if t == "input_audio_buffer.committed":
            self.state.on_audio_committed(self.state._pending_input_audio_chunks)
            return
        if t == "conversation.item.done":
            self._on_conversation_item_done(data)
            return
        if t in self.USER_TRANSCRIPT_TYPES:
            self._on_user_transcript_done(data)
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
            normalized_message = str(message).strip().lower()
            if code == "response_cancel_not_active" or (
                code == "invalid_request_error"
                and "cancellation failed" in normalized_message
                and "no active response found" in normalized_message
            ):
                logger.verbose(
                    "Ignoring non-fatal cancel race: no active response to cancel.",
                    "ℹ️",
                )
                return
            if code == "conversation_already_has_active_response":
                logger.verbose(
                    "Ignoring non-fatal race: response already in progress.",
                    "ℹ️",
                )
                return
            mapped_code = "noapikey" if "invalid_api_key" in code else "error"
            logger.error(f"API Error ({mapped_code}): {message}")
            await self.error_handler.play_error_sound(mapped_code, message)
            return
        # else: ignore unrecognized messages silently

    async def stop_session(self, reason: str | None = None):
        if self._stopping:
            return
        self._stopping = True
        if not reason:
            with contextlib.suppress(Exception):
                caller = inspect.stack()[1]
                reason = f"caller={caller.function}"
        logger.info(f"Stopping session... ({reason or 'unspecified'})", "🛑")

        # Increment interaction count for current user at end of session
        if not self._interaction_count_recorded:
            user_manager.increment_current_user_interaction_count()
            self._interaction_count_recorded = True

        self.session_active.clear()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.mic_manager.stop), timeout=1.0
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Mic stop timed out during session shutdown; continuing teardown.",
                "⚠️",
            )
            self.mic_manager.mic_running = False
        await self._close_ws()

        # Give the message loop a moment to exit
        await asyncio.sleep(0.1)

    async def request_stop(self):
        logger.info("Stop requested via external signal.", "🛑")
        self.session_active.clear()
        # Ensure run_stream is not left waiting on recv() keepalive timeouts.
        await self._close_ws(timeout=0.5)

    async def interrupt_to_user_turn(self):
        """Interrupt the assistant response and hand control back to the user mic."""
        if not self.session_active.is_set():
            return

        logger.info("Interrupting assistant turn and reopening mic...", "🛑")
        self.interrupt_event.clear()
        audio.stop_playback()

        if self.state.response_active:
            # Ignore the trailing response.done from this cancelled assistant turn.
            self.state._skip_post_response_once = True
            with contextlib.suppress(Exception):
                await self._ws_send_json({"type": "response.cancel"})

        # Force user-turn gating open even if provider state is racing.
        self.state.response_active = False
        self.state.allow_mic_input = True
        self.state.assistant_speaking = False
        self.state._saw_follow_up_call = False
        self.last_activity[0] = time.time()
        opened = await self.mic_manager.start_after_playback(delay=0.2, retries=2)
        if not opened:
            logger.warning(
                "Mic reopen failed after startup race fallback; session may need restart.",
                "⚠️",
            )
