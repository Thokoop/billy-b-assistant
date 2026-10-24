"""Continuous Live sessions, independent of Realtime's response/turn machinery."""

import asyncio
import base64
import json
import time
from collections import deque
from contextlib import suppress
from contextvars import ContextVar

import numpy as np
from websockets.exceptions import ConnectionClosed

from .. import audio
from ..config import MIC_TIMEOUT_SECONDS, SILENCE_THRESHOLD, TEXT_ONLY_MODE
from ..logger import logger
from ..providers.live_provider import live_tools, split_instructions
from ..providers.live_transcript import LiveTranscript
from .mic_manager_wrapper import MicManagerWrapper


_tool_messages = ContextVar('live_tool_messages', default=None)


class LiveMicrophone(MicManagerWrapper):
    """Use the existing capture/uplink/AEC, with a continuous Live timeline."""

    async def _send_audio(self, samples):
        if not self.session.session_active.is_set() or self.session.ws is None:
            raise asyncio.CancelledError
        payload = audio.mic_audio_payload(samples)
        if payload is not None:
            event = json.loads(payload)
            event['type'] = 'session.input_audio.append'
            await self.session.ws.send(json.dumps(event))

    def _process_microphone(self, indata):
        if not self.session.session_active.is_set():
            return
        samples = audio.mic_input_samples_for_meter(indata)
        playing = not audio.playback_done_event.is_set()
        if audio.echo_cancellation_active():
            samples = audio.process_mic_with_aec(samples, render_active=playing)
        # Keep time moving during playback even without AEC. Sending silence
        # prevents speaker echo becoming input without starving Live's timeline.
        if playing and not audio.echo_cancellation_active():
            samples = samples * 0
        self.last_rms = audio.calculate_input_rms(samples)
        if self.last_rms > 0 and self.last_rms >= SILENCE_THRESHOLD:
            self._last_live_mic_activity = time.time()
            self.session.last_activity[0] = self._last_live_mic_activity
        if self.session.session_initialized:
            self._uplink.submit(samples)
        else:
            self._store_prebuffer(samples)

    def _report_uplink(self, metrics):
        # Include the idle gate in the existing, rate-limited verbose report.
        # A low RMS at log time can hide an earlier noise spike, so show when
        # the microphone last exceeded the threshold as well.
        now = time.time()
        last_mic_activity = getattr(self, '_last_live_mic_activity', None)
        engine = getattr(self.session, '_live', None)
        super()._report_uplink({
            **metrics,
            'mic_rms': round(self.last_rms, 1),
            'speech_threshold': SILENCE_THRESHOLD,
            'mic_activity_age_seconds': (
                round(max(0, now - last_mic_activity), 1)
                if last_mic_activity is not None
                else None
            ),
            'idle_seconds': round(max(0, now - self.session.last_activity[0]), 1),
            'idle_timeout_seconds': max(1, MIC_TIMEOUT_SECONDS),
            'assistant_speaking': self.session.state.assistant_speaking,
            'pending_responses': len(engine.responses) if engine else 0,
            'pending_tools': len(engine.tasks) if engine else 0,
        })

    async def timeout_checker(self):
        # LiveSession owns inactivity; Realtime's VAD/turn timeout is unsuitable.
        return


class LiveSession:
    def __init__(self, session):
        self.session = session
        self.closed = asyncio.Event()
        self.closing = False
        self.close_complete = asyncio.Event()
        self.receiver = None
        self.monitor = None
        self.caption_task = None
        self.transcript = LiveTranscript()
        self.tasks = set()
        self.seen = deque(maxlen=256)
        self.responses = {}
        self.tool_lock = asyncio.Lock()
        self.last_output = 0.0
        self.playback_quiet_since = None
        self.drop_output = False
        self.tools = []
        self.voice = None
        self.started_at = time.monotonic()
        self.audible_intervals = deque()
        self.finishing_tool = None

    async def send(self, event):
        if self.closing or not self.session.session_active.is_set():
            raise asyncio.CancelledError
        if self.session.ws is not None:
            await self.session.realtime_ai_provider.send_message(self.session.ws, event)

    async def start(self):
        from ..bluetooth import mode
        from ..persona_manager import persona_manager
        from ..session_manager import (
            get_instructions_with_user_context,
            get_tools_for_current_mode,
        )

        s = self.session
        if mode.active():
            logger.info('GPT-Live waiting: Bluetooth owns the audio devices.')
            return
        s.loop = asyncio.get_running_loop()
        if TEXT_ONLY_MODE:
            raise ValueError(
                'GPT-Live needs audio; select OpenAI Realtime for text-only mode.'
            )
        await s.user_handler.auto_identify_default_user()
        await s.persona_handler.reload_persona_from_profile()
        await asyncio.to_thread(audio.recover_echo_cancellation)
        s.state.reset_for_new_session()
        s.audio_handler.clear_buffer()
        s.session_initialized = False
        s.session_active.set()
        s._stopping = False
        s.last_activity[0] = time.time()
        s.mic_manager = LiveMicrophone(s)
        self.tools = live_tools(get_tools_for_current_mode())
        self.voice = persona_manager.get_current_persona_voice()
        try:
            # Do not forward the wake-up clip or open Bluetooth-owned hardware.
            while not audio.playback_done_event.is_set():
                if not s.session_active.is_set():
                    return
                await asyncio.sleep(0.01)
            if s.kickoff_text and s.kickoff_kind == 'literal':
                clip = await s.realtime_ai_provider.generate_audio_clip(
                    s.kickoff_text, voice=self.voice, strict_literal=True
                )
                if not s.session_active.is_set() or mode.active():
                    return
                s.audio_handler.ensure_playback_worker()
                audio.playback_done_event.clear()
                audio.playback_queue.put(clip)
                await audio.playback_queue.wait(
                    cancelled=lambda: not s.session_active.is_set()
                )
                audio.playback_done_event.set()
                if not s.session_active.is_set():
                    return
            if not s.session_active.is_set() or mode.active():
                return
            s.mic_manager.start()
            s.ws = await s.realtime_ai_provider.connect(
                get_instructions_with_user_context('gpt-live'),
                self.tools,
                voice=self.voice,
                announcement=s.kickoff_text if s.kickoff_kind == 'literal' else None,
            )
            if not s.session_active.is_set() or self.closing:
                await s.ws.close()
                s.ws = None
                return
            s.last_activity[0] = time.time()
            s.session_initialized = True
            s.mic_manager.flush_prebuffer()
            s.audio_handler.ensure_playback_worker()
            self.receiver = asyncio.create_task(self.receive())
            self.monitor = asyncio.create_task(self.watch_playback())
            self.caption_task = asyncio.create_task(self.publish_transcript())
            if s.kickoff_text and s.kickoff_kind != 'literal':
                # Typed requests belong to Responses, not a fabricated voice turn.
                await self.send({
                    'type': 'response.item.create',
                    'item': {
                        'type': 'message',
                        'role': 'user',
                        'content': [{'type': 'input_text', 'text': s.kickoff_text}],
                    },
                })
                await self.send({'type': 'response.create'})
            done, _ = await asyncio.wait(
                (self.receiver, self.monitor), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f'GPT-Live session failed: {exc}')
            await s.error_handler.play_error_sound('error', str(exc))
        finally:
            try:
                await s.stop_session(reason='live_session_ended')
                # An external stop can clear session_active before it finishes.
                # Keep the receiver and event loop alive until that stop drains Live.
                stop_complete = getattr(s, '_stop_complete', None)
                if stop_complete is not None:
                    await stop_complete.wait()
            finally:
                for task in (
                    self.receiver,
                    self.monitor,
                    self.caption_task,
                    *tuple(self.tasks),
                ):
                    if (
                        task is not None
                        and task is not asyncio.current_task()
                        and task is not self.finishing_tool
                    ):
                        task.cancel()
                await asyncio.gather(
                    *(
                        t
                        for t in (
                            self.receiver,
                            self.monitor,
                            self.caption_task,
                            *tuple(self.tasks),
                        )
                        if t is not None
                        and t is not asyncio.current_task()
                        and t is not self.finishing_tool
                    ),
                    return_exceptions=True,
                )
                if (
                    self.finishing_tool is not None
                    and self.finishing_tool is not asyncio.current_task()
                ):
                    await asyncio.gather(self.finishing_tool, return_exceptions=True)
                self.transcript.close()
                self._log_transcript()
                if not mode.active():
                    s._set_idle_state()

    async def receive(self):
        ws = self.session.ws
        try:
            async for raw in ws:
                event = json.loads(raw)
                if event.get('type') == 'session.closed':
                    logger.info(f"GPT-Live final usage: {event.get('usage')}")
                    self.closed.set()
                    return
                await self.handle(event)
        except ConnectionClosed:
            if not self.closing:
                raise
        if not self.closed.is_set() and not self.closing:
            raise RuntimeError('GPT-Live connection closed before finalization')

    def _record_transcript(self, event):
        speaker = (
            'user' if event.get('type') == 'session.input_transcript.delta' else 'billy'
        )
        self.transcript.append(
            speaker, event.get('delta'), event.get('start_ms'), event.get('end_ms')
        )

    def _log_transcript(self):
        text = self.transcript.finish()
        if text:
            logger.info(f"GPT-Live session transcript:\n{text}")

    async def handle(self, event):
        kind = event.get('type')
        s = self.session
        if self.closing or not s.session_active.is_set():
            # stop_session clears session_active before waiting for microphone
            # cleanup. Keep final transcripts, but never queue audio or tools
            # during that gap before close() starts draining the socket.
            if kind in (
                'session.input_transcript.delta',
                'session.output_transcript.delta',
            ):
                self._record_transcript(event)
            return
        if kind == 'session.output_audio.delta':
            pcm = base64.b64decode(event.get('delta') or '', validate=True)
            if len(pcm) % 2:
                raise ValueError('GPT-Live returned an incomplete PCM sample')
            if not pcm:
                return
            audible = (
                np.max(np.abs(np.frombuffer(pcm, dtype='<i2').astype(np.int32))) > 32
            )
            if self.drop_output:
                if audible:
                    self.last_output = time.monotonic()
                return
            handler = s.audio_handler
            if handler._playback_generation is None:
                handler._playback_generation = audio.begin_aec_playback_generation()
            generation = handler._playback_generation
            heard_ms = audio.aec_heard_audio_ms(generation)
            # The physical button can reset playback from another thread before
            # the stop/interrupt coroutine runs. A reset position is not backlog.
            if not audio.is_current_aec_playback_generation(generation):
                return
            start_ms = handler._queued_output_bytes / 48
            end_ms = (handler._queued_output_bytes + len(pcm)) / 48
            if end_ms - heard_ms > 30_000:
                raise RuntimeError(
                    'GPT-Live audio playback fell more than 30 seconds behind'
                )
            handler._queued_output_bytes += len(pcm)
            # Preserve all audio, including pauses. Only audible output keeps
            # the conversation alive; Live may stream digital silence too.
            if audible:
                self.last_output = time.monotonic()
                self.playback_quiet_since = None
                self.audible_intervals.append((start_ms, end_ms))
                if not s.state.assistant_speaking:
                    s._set_speaking_state()
                s.state.assistant_speaking = True
                audio.playback_done_event.clear()
                s.last_activity[0] = time.time()
            audio.playback_queue.put(('tts', pcm, generation))
        elif kind in (
            'session.input_transcript.delta',
            'session.output_transcript.delta',
        ):
            # Transcript delivery is not audio activity. Empty fragments must
            # not prolong silence, and delayed assistant text must not restart
            # the countdown after the speaker has already finished playing.
            delta = event.get('delta')
            if (
                kind == 'session.input_transcript.delta'
                and isinstance(delta, str)
                and delta.strip()
            ):
                s.last_activity[0] = time.time()
            self._record_transcript(event)
        elif kind == 'response.event':
            await self.backend_event(event)
        elif kind == 'error':
            raise RuntimeError(
                (event.get('error') or {}).get('message') or 'GPT-Live error'
            )

    async def publish_transcript(self):
        while self.session.session_active.is_set() and not self.closing:
            await asyncio.to_thread(self.transcript.publish)
            await asyncio.sleep(0.2)

    async def watch_playback(self):
        s = self.session
        while s.session_active.is_set():
            await asyncio.sleep(0.1)
            if self.closing or not s.session_active.is_set():
                return
            quiet = time.monotonic() - self.last_output > 0.6
            if quiet and self.drop_output:
                self.drop_output = False
            heard_ms = audio.aec_heard_audio_ms(s.audio_handler._playback_generation)
            while self.audible_intervals and heard_ms >= self.audible_intervals[0][1]:
                self.audible_intervals.popleft()
            if not self.audible_intervals and s.state.assistant_speaking:
                # Live sends small continuous chunks. Draining one chunk is not
                # the end of speech: bridge brief gaps after actual playback.
                # Only debounce the published state. Capture may resume as
                # soon as the speaker is quiet, even without echo cancellation.
                s.audio_handler.signal_playback_done()
                now = time.monotonic()
                if self.playback_quiet_since is None:
                    self.playback_quiet_since = now
                elif now - self.playback_quiet_since >= 0.6:
                    s.state.assistant_speaking = False
                    s._set_listening_state()
                    s.last_activity[0] = time.time()
            if time.monotonic() - self.started_at > 1800 or any(
                time.monotonic() - r['started'] > 120 for r in self.responses.values()
            ):
                raise TimeoutError(
                    'GPT-Live session or backend exceeded its time limit'
                )
            busy = (
                s.state.assistant_speaking or bool(self.responses) or bool(self.tasks)
            )
            if not busy and time.time() - s.last_activity[0] > max(
                1, MIC_TIMEOUT_SECONDS
            ):
                await s.stop_session(reason='live_inactivity')
                return

    async def backend_event(self, envelope):
        event = envelope.get('event') or {}
        delegation = envelope.get('delegation_id')
        kind = event.get('type')
        response_id = event.get('response_id') or (event.get('response') or {}).get(
            'id'
        )
        if kind == 'response.created':
            if not response_id or len(self.responses) >= 16:
                raise RuntimeError('Invalid or excessive GPT-Live backend responses')
            self.responses[response_id] = {
                'calls': {},
                'done': False,
                'delegation': delegation,
                'started': time.monotonic(),
            }
            return
        if kind == 'response.output_item.done':
            item = event.get('item') or {}
            if item.get('type') != 'function_call':
                return
            call_id = item.get('call_id')
            if not call_id or call_id in self.seen:
                return
            # Responses output-item events do not carry response_id. Correlate
            # them with response.created through the outer Live delegation_id.
            # Never guess across delegations or when two responses are active.
            if not response_id:
                candidates = [
                    key
                    for key, record in self.responses.items()
                    if record['delegation'] == delegation and not record['done']
                ]
                if len(candidates) == 1:
                    response_id = candidates[0]
            record = self.responses.get(response_id)
            if not record or record['done'] or record['delegation'] != delegation:
                raise RuntimeError(
                    'GPT-Live tool call has no unique active backend response '
                    f'(delegation={delegation}, response={response_id})'
                )
            if len(self.tasks) >= 8:
                raise RuntimeError('Too many pending GPT-Live tool calls')
            self.seen.append(call_id)
            self.responses[response_id]['calls'][call_id] = False
            task = asyncio.create_task(self.run_tool(item, response_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif kind in ('response.completed', 'response.failed', 'response.incomplete'):
            if kind != 'response.completed':
                raise RuntimeError(f'GPT-Live backend failed: {event.get("response")}')
            if response_id in self.responses:
                self.responses[response_id]['done'] = True
                await self.continue_backend(response_id)

    async def continue_backend(self, response_id):
        record = self.responses.get(response_id)
        if not record or not record['done'] or not all(record['calls'].values()):
            return
        self.responses.pop(response_id)
        if record['calls']:
            await self.send({'type': 'response.create'})
        self.session.last_activity[0] = time.time()

    async def run_tool(self, item, response_id):
        messages = []
        token = _tool_messages.set(messages)
        call_id = item['call_id']
        try:
            async with asyncio.timeout(45), self.tool_lock:
                args = json.loads(item.get('arguments') or '{}')
                allowed = {t['name'] for t in self.tools}
                if item.get('name') not in allowed or not isinstance(args, dict):
                    raise ValueError('Unavailable function or invalid arguments')
                if item['name'] == 'play_song':
                    self.finishing_tool = asyncio.current_task()
                await self.session.function_handler.handle(
                    item['name'], json.dumps(args), call_id
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            messages.append({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': json.dumps({
                        'ok': False,
                        'error': str(exc),
                        'note': 'Check the outcome before retrying an action.',
                    }),
                },
            })
        finally:
            _tool_messages.reset(token)
        if self.closing or not self.session.session_active.is_set():
            return
        try:
            results = [
                m['item']
                for m in messages
                if m.get('item', {}).get('type') == 'function_call_output'
                and m['item'].get('call_id') == call_id
            ]
            if not results and item['name'] in {'switch_persona', 'manage_profile'}:
                from ..persona_manager import persona_manager

                target = args.get('persona') or args.get('preferred_persona')
                results = [
                    {
                        'type': 'function_call_output',
                        'call_id': call_id,
                        'output': json.dumps({
                            'ok': bool(
                                target and target == persona_manager.current_persona
                            ),
                            'persona': persona_manager.current_persona,
                        }),
                    }
                ]
            result = (
                results[-1]
                if results
                else {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': json.dumps({
                        'ok': False,
                        'error': 'Function returned no confirmed result. Do not claim success.',
                    }),
                }
            )
            await self.send({'type': 'response.item.create', 'item': result})
            for message in messages:
                if (
                    message.get('type') != 'response.create'
                    and message.get('item', {}).get('type') != 'function_call_output'
                ):
                    await self.application_event(message)
            self.responses[response_id]['calls'][call_id] = True
            await self.continue_backend(response_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f'GPT-Live tool result failed: {exc}')
            await self.session.stop_session(reason='live_tool_result_failed')

    async def application_event(self, event):
        """Bridge existing business handlers, never Realtime audio/turn events."""
        captured = _tool_messages.get()
        if captured is not None:
            captured.append(event)
            return
        kind = event.get('type')
        if kind == 'session.update':
            from ..session_manager import get_tools_for_current_mode

            config = event['session']
            self.tools = live_tools(get_tools_for_current_mode())
            instructions = config.get('instructions')
            backend = {'tools': self.tools}
            if instructions:
                voice, backend['instructions'] = split_instructions(
                    instructions, self.tools
                )
                # UTF-8 byte bounds are conservative for the 500-token append limit.
                # Split by complete characters, keeping every instruction in order.
                chunk = ''
                for character in 'Updated persona and context:\n' + voice:
                    if len((chunk + character).encode('utf-8')) > 400:
                        await self.send({
                            'type': 'session.instructions.append',
                            'delegation_id': None,
                            'content': chunk,
                        })
                        chunk = ''
                    chunk += character
                if chunk:
                    await self.send({
                        'type': 'session.instructions.append',
                        'delegation_id': None,
                        'content': chunk,
                    })
            await self.send({
                'type': 'session.update',
                'session': {'delegation': {'responses': backend}},
            })
            requested_voice = config.get('audio', {}).get('output', {}).get('voice')
            if requested_voice and requested_voice != self.voice:
                logger.info(
                    'GPT-Live voice changes take effect with the next conversation.'
                )
        elif kind == 'conversation.item.create':
            item = event['item']
            if item.get('type') == 'function_call_output':
                await self.send({'type': 'response.item.create', 'item': item})
            elif item.get('type') == 'message':
                # Backend context accepts images and text; never inject synthetic
                # user text into Live's microphone transcript.
                await self.send({'type': 'response.item.create', 'item': item})
        elif kind == 'response.create':
            await self.send({'type': 'response.create'})
        elif kind == 'persona_changed':
            return  # Local notification, not an API command.
        elif kind == 'session.close':
            await self.session.stop_session(reason='live_close_requested')
        else:
            raise ValueError(f'Unsupported GPT-Live application event: {kind}')

    async def interrupt(self):
        self.drop_output = True
        self.last_output = time.monotonic()
        audio.stop_playback()
        self.session.audio_handler.clear_buffer()
        self.audible_intervals.clear()
        self.session.state.assistant_speaking = False
        self.session.interrupt_event.clear()
        self.session._set_listening_state()
        await self.send({
            'type': 'session.instructions.append',
            'delegation_id': None,
            'content': 'The user pressed the interrupt button. Stop speaking and listen. '
            'Do not resume the previous answer unless asked.',
        })

    @staticmethod
    def _abort_transport(ws):
        # A timed-out WebSocket handshake must not keep the socket alive.
        transport = getattr(ws, 'transport', None)
        if transport is not None:
            with suppress(Exception):
                transport.abort()

    async def close(self, *, timeout=5.0):
        if self.closing:
            # Concurrent stop requests must not report completion while the
            # original close still owns the socket and audio cleanup.
            try:
                await asyncio.wait_for(self.close_complete.wait(), timeout=timeout)
            except TimeoutError:
                self._abort_transport(self.session.ws)
            return
        self.closing = True
        self.transcript.close()
        for task in tuple(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        ws = self.session.ws
        try:
            if ws is None:
                return
            try:
                if not self.closed.is_set():
                    async with asyncio.timeout(timeout):
                        await ws.send(json.dumps({'type': 'session.close'}))
                        if self.receiver is not None and not self.receiver.done():
                            await self.closed.wait()
                    if not self.closed.is_set():
                        logger.warning('GPT-Live closed without confirmed final usage.')
            except Exception as exc:
                logger.warning(
                    f'GPT-Live finalization incomplete ({type(exc).__name__}): {exc}'
                )
            finally:
                try:
                    await asyncio.wait_for(ws.close(), timeout=min(0.5, timeout))
                except TimeoutError:
                    logger.info(
                        'GPT-Live transport close timed out; releasing connection.'
                    )
                    self._abort_transport(ws)
                except asyncio.CancelledError:
                    self._abort_transport(ws)
                    raise
                except ConnectionClosed:
                    pass
                except Exception as exc:
                    logger.warning(
                        f'GPT-Live transport close failed ({type(exc).__name__}): {exc}'
                    )
                    self._abort_transport(ws)
        finally:
            try:
                if self.session.ws is ws:
                    self.session.ws = None
                from ..bluetooth import mode

                if ws is not None and not mode.active():
                    audio.stop_playback()
                    self.session.audio_handler.clear_buffer()
                    self.session.state.assistant_speaking = False
            finally:
                try:
                    self._log_transcript()
                finally:
                    self.close_complete.set()
