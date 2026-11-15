"""A conversation answered by Home Assistant's Assist pipeline.

``HaAssistSession`` stands in for ``BillySession`` when
``REALTIME_AI_PROVIDER=home-assistant``. It offers the attributes and
coroutines core.button and core.mqtt use to start, interrupt and stop a
session, so wake word, button, MQTT and device triggers work unchanged.

One turn: Billy asks Home Assistant to start a pipeline run, streams the
microphone (16 kHz) until Home Assistant's VAD ends the utterance, then plays
the streamed TTS answer through the normal playback queue (with mouth
movement). When Home Assistant asks to continue the conversation, Billy
listens again in the same conversation. The microphone is closed while Billy
speaks, so there is no echo path and no barge-in except the button.
"""

import asyncio
import contextlib
import threading
import time

from ..logger import logger
from .audio_bridge import ASSIST_RATE, StreamResampler, mic_to_assist_pcm
from .messages import VoiceAssistantEvent as Event
from .server import RunEndedEarly, SatelliteUnavailable


MAX_LISTEN_SECONDS = 20.0
EVENT_TIMEOUT = 30.0
STREAM_START_GRACE = 2.0
WAKE_UP_CLIP_TIMEOUT = 6.0
MIC_QUEUE_BLOCKS = 50  # about 2 s of capture at 40 ms blocks


class _MicState:
    """Exposes ``mic_running`` the way core.button inspects sessions."""

    def __init__(self) -> None:
        self.mic_running = False


class _Hardware:
    """Billy's audio, motors and state publishing; replaced in tests."""

    def __init__(self) -> None:
        from .. import audio, mic, movements, mqtt

        self.audio = audio
        self.mic_manager = mic.MicManager()
        self.move_head = movements.move_head
        self.mqtt_publish = mqtt.mqtt_publish

    @property
    def mic_rate(self) -> int:
        return int(self.audio.MIC_RATE or ASSIST_RATE)

    def start_mic(self, callback) -> None:
        self.mic_manager.start(callback)

    def stop_mic(self) -> None:
        self.mic_manager.stop()

    def play(self, pcm_24k: bytes) -> None:
        self.audio.playback_queue.put(pcm_24k)

    def speaking_started(self) -> None:
        self.audio.playback_done_event.clear()

    def stop_playback(self) -> None:
        self.audio.stop_playback()

    async def wait_playback(self, cancelled) -> None:
        try:
            await self.audio.playback_queue.wait(cancelled=cancelled)
        finally:
            self.audio.playback_done_event.set()

    def wake_up_clip_done(self) -> bool:
        return self.audio.playback_done_event.is_set()

    def set_state(self, state: str) -> None:
        if state == 'idle':
            self.move_head('off')
        else:
            self.move_head('on')
        self.mqtt_publish('billy/state', state)

    async def error_sound(self, message: str) -> None:
        from ..session.error_handler import play_standalone_error_sound

        await play_standalone_error_sound('error', message)


class HaAssistSession:
    def __init__(
        self,
        interrupt_event=None,
        *,
        service=None,
        hardware=None,
        announcement=None,
        wake_word_phrase='',
        **_,
    ):
        # Opening-line options (kickoff_text, ...) need Billy's own voice
        # provider; Home Assistant has no equivalent, so they are ignored.
        # ``announcement`` (service.RingAnnouncement) is a Home Assistant
        # announcement a device rang for: played first, then this session
        # listens only when Home Assistant asked to start a conversation.
        self._announcement = announcement
        # Billy's own wake word started this session; Home Assistant hears it
        # with the first run only.
        self._wake_word_phrase = wake_word_phrase or ''
        self.interrupt_event = interrupt_event or threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.session_active = threading.Event()
        self.last_activity = [time.time()]
        self.run_mode = None
        self.mic_manager = _MicState()
        self._service = service
        self._hardware = hardware
        self._stopping = False
        self._interrupted = False
        self._speaking = False
        self._accept_tts = False
        self._resampler: StreamResampler | None = None
        self._events: asyncio.Queue | None = None
        self._turn_task: asyncio.Task | None = None
        self._finished: asyncio.Event | None = None
        self._run_open = False
        # Set on the service thread: events before this turn's RUN_START, and
        # audio outside TTS_STREAM_START..END, belong to an earlier run.
        self._run_started = False
        self._tts_open = False
        self.conversation_id = ''

    # ----- interface used by core.button / core.mqtt ------------------------

    def is_assistant_turn(self) -> bool:
        return self._speaking

    async def interrupt_to_user_turn(self, source: str = 'button') -> None:
        """Cut Billy's answer short and listen again in the same conversation."""
        logger.info(f'Home Assistant answer interrupted by {source}.', '🎙️')
        self._interrupted = True
        self._accept_tts = False
        self._run_started = self._tts_open = False
        self._speaking = False
        self.hardware.stop_playback()
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()

    async def request_stop(self) -> None:
        self._stopping = True
        self.interrupt_event.set()
        self._accept_tts = False
        self._speaking = False
        self.hardware.stop_playback()
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()

    async def _close_ws(self, timeout: float = 0.5) -> None:
        await self.request_stop()

    async def stop_session(self) -> None:
        await self.request_stop()
        finished = self._finished
        if finished is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(finished.wait(), 3.0)

    # ----- delivery from the satellite service thread -----------------------

    def deliver_event(self, event) -> None:
        loop, queue = self.loop, self._events
        if loop is None or queue is None or loop.is_closed():
            return
        kind = event.event_type
        if kind == Event.RUN_START:
            self._run_started = True
        if not self._run_started:
            return  # late event of an interrupted or stopped run
        if kind == Event.TTS_STREAM_START:
            self._tts_open = True
        elif kind == Event.TTS_STREAM_END:
            self._tts_open = False
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def deliver_tts_audio(self, pcm_16k: bytes) -> None:
        resampler = self._resampler
        if not (self._accept_tts and self._tts_open) or resampler is None:
            return
        if self._stopping:
            return
        pcm = resampler.process(pcm_16k)
        if not pcm:
            return
        if not self._speaking:
            self._speaking = True
            self.hardware.speaking_started()
            self.hardware.set_state('speaking')
        self.last_activity[0] = time.time()
        try:
            self.hardware.play(pcm)
        except Exception as exc:
            logger.warning(f'Could not queue Home Assistant speech: {exc}')
            self._accept_tts = False

    # ----- lifecycle --------------------------------------------------------

    @property
    def hardware(self) -> _Hardware:
        if self._hardware is None:
            self._hardware = _Hardware()
        return self._hardware

    @property
    def service(self):
        if self._service is None:
            from .service import get_service

            self._service = get_service()
        return self._service

    def _should_stop(self) -> bool:
        return self._stopping or self.interrupt_event.is_set()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._finished = asyncio.Event()
        service = self.service
        if service is None or not service.voice_connected:
            self._finished.set()
            if self._announcement is not None:
                self._announcement.finish(False)
            logger.warning('Home Assistant is not connected to Billy.', '🏠')
            await self.hardware.error_sound('Home Assistant is not connected')
            return
        self.session_active.set()
        service.attach(self)
        try:
            await self._wait_for_wake_up_clip()
            announcement = self._announcement
            if announcement is not None:
                played = await self._play_announcement(announcement)
                if not (played and announcement.start_conversation):
                    return
            while not self._should_stop():
                self._interrupted = False
                self._turn_task = asyncio.create_task(self._turn())
                try:
                    keep_going = await self._turn_task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    if self._interrupted and not self._should_stop():
                        await self._end_run()
                        continue
                    break
                if not keep_going or self.run_mode == 'dory':
                    break
        finally:
            await self._cleanup(service)

    async def _play_announcement(self, announcement) -> bool:
        if not announcement.claim():
            return False
        from .service import decode_to_playback

        if announcement.text:
            logger.info(f'Home Assistant announcement: {announcement.text}', '📣')
        played = 0
        self._speaking = True
        self.hardware.speaking_started()
        self.hardware.set_state('speaking')

        def cancelled() -> bool:
            return self._should_stop() or self._interrupted

        try:
            for url in announcement.urls:
                played += await decode_to_playback(
                    url, self.hardware.play, cancelled=cancelled
                )
            await self.hardware.wait_playback(cancelled)
        except Exception as exc:
            logger.warning(f'Home Assistant announcement failed: {exc}')
            played = 0
        finally:
            self._speaking = False
        success = played > 0 and not cancelled()
        announcement.finish(success)
        return success

    async def _wait_for_wake_up_clip(self) -> None:
        deadline = time.monotonic() + WAKE_UP_CLIP_TIMEOUT
        while not self.hardware.wake_up_clip_done() and time.monotonic() < deadline:
            if self._should_stop():
                return
            await asyncio.sleep(0.02)

    async def _cleanup(self, service) -> None:
        if self._announcement is not None:
            self._announcement.finish(False)  # no-op once it was played
        self._accept_tts = False
        self._speaking = False
        self.hardware.stop_mic()
        self.mic_manager.mic_running = False
        await self._end_run()
        service.detach(self)
        if self._stopping:
            self.hardware.stop_playback()
        self.hardware.set_state('idle')
        self.session_active.clear()
        self._events = None
        if self._finished is not None:
            self._finished.set()

    async def _end_run(self) -> None:
        if self._run_open:
            self._run_open = False
            await self.service.stop_pipeline()

    # ----- one question and answer ------------------------------------------

    async def _turn(self) -> bool:
        """Listen, then play the answer; True to listen again afterwards."""
        self._run_started = self._tts_open = False
        self._events = asyncio.Queue()
        self._resampler = StreamResampler(ASSIST_RATE, 24000)
        self._accept_tts = True
        try:
            phrase, self._wake_word_phrase = self._wake_word_phrase, ''
            run = await self.service.start_pipeline(
                conversation_id=self.conversation_id, wake_word_phrase=phrase
            )
        except RunEndedEarly:
            if phrase:
                # The voice setup wizard takes wake-word runs as its test.
                logger.info(
                    f'Home Assistant took "{phrase}" for its voice assistant setup.',
                    '🏠',
                )
                return False
            logger.info(
                'Home Assistant ended the conversation before it started. If its '
                'voice assistant setup is still open, finish or close it first.',
                '🏠',
            )
            return False
        except (SatelliteUnavailable, TimeoutError) as exc:
            logger.warning(f'Home Assistant could not start listening: {exc}')
            await self.hardware.error_sound(str(exc))
            return False
        self._run_open = True
        self.hardware.set_state('listening')
        await self._stream_microphone(run)
        return await self._await_answer()

    async def _stream_microphone(self, run) -> None:
        queue: asyncio.Queue[bytes] = asyncio.Queue(MIC_QUEUE_BLOCKS)
        loop = asyncio.get_running_loop()
        dropped = 0
        mic_rate = self.hardware.mic_rate

        def offer(pcm: bytes) -> None:
            nonlocal dropped
            try:
                queue.put_nowait(pcm)
            except asyncio.QueueFull:
                dropped += 1

        def callback(indata, frames, time_info, status) -> None:
            # PortAudio thread: convert and hand over, never block.
            try:
                pcm = mic_to_assist_pcm(indata, mic_rate)
                loop.call_soon_threadsafe(offer, pcm)
            except RuntimeError:
                pass  # loop already closed during teardown

        self.hardware.start_mic(callback)
        self.mic_manager.mic_running = True
        deadline = time.monotonic() + MAX_LISTEN_SECONDS
        try:
            while run.mic_open and not self._should_stop():
                if time.monotonic() >= deadline:
                    logger.info('Stopped listening: utterance too long.', '⏱️')
                    await self._end_run()
                    break
                try:
                    pcm = await asyncio.wait_for(queue.get(), 0.5)
                except TimeoutError:
                    continue
                if not await self.service.send_audio(pcm):
                    break
                self.last_activity[0] = time.time()
        finally:
            self.hardware.stop_mic()
            self.mic_manager.mic_running = False
            if dropped:
                logger.warning(f'Dropped {dropped} microphone blocks (network slow).')

    async def _await_answer(self) -> bool:
        keep_going = False
        run_ended = stream_started = stream_ended = has_tts = False
        queue = self._events
        while not self._should_stop():
            timeout = EVENT_TIMEOUT
            if run_ended and has_tts and not stream_started:
                timeout = STREAM_START_GRACE
            try:
                event = await asyncio.wait_for(queue.get(), timeout)
            except TimeoutError:
                if not run_ended:
                    logger.warning('Home Assistant stopped responding.')
                    await self._end_run()
                break
            kind, data = event.event_type, event.data
            if kind == Event.STT_END and data.get('text'):
                logger.info(f'Home Assistant heard: {data["text"].strip()}', '👂')
            elif kind == Event.INTENT_END:
                self.conversation_id = data.get('conversation_id', '')
                keep_going = data.get('continue_conversation') == '1'
            elif kind == Event.TTS_START and data.get('text'):
                logger.info(f'Home Assistant answers: {data["text"]}', '💬')
            elif kind == Event.TTS_END:
                has_tts = bool(data.get('url'))
            elif kind == Event.TTS_STREAM_START:
                stream_started = True
            elif kind == Event.TTS_STREAM_END:
                stream_ended = True
            elif kind == Event.ERROR:
                code = data.get('code', '')
                logger.info(f'Home Assistant pipeline ended: {code} {data}', 'ℹ️')
                self._run_open = False
                if code not in ('stt-no-text-recognized', 'no_wake_word'):
                    await self.hardware.error_sound(data.get('message') or code)
                return False
            elif kind == Event.RUN_END:
                run_ended = True
                self._run_open = False
            if run_ended and (stream_ended or not (has_tts or stream_started)):
                break
        if self._speaking:
            await self.hardware.wait_playback(self._should_stop)
        self._speaking = False
        return keep_going and not self._should_stop()
