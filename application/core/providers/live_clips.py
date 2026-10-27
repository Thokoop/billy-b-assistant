"""Short Live recordings for wake-up clips and literal announcements.

Live has no utterance-completed event. A quiet tail is only a heuristic; users
must listen to the result. No microphone, playback worker or tools are started.
"""

import asyncio
import base64
import json
import struct
from contextlib import suppress

from ..logger import logger


RATE = 24000
RECORD_TIMEOUT = 35.0
MAX_AUDIO_BYTES = RATE * 2 * 30
QUIET_SECONDS = 2.0
AUDIO_GAP_SECONDS = 3.0
CLOSE_TIMEOUT = 2.0
TRANSPORT_TIMEOUT = 0.5
SILENCE_INTERVAL = 0.1
SPEECH_THRESHOLD = 180


class Recording:
    def __init__(self):
        self.audio = bytearray()
        self.first_voice = None
        self.last_voice = 0

    def append(self, encoded):
        pcm = base64.b64decode(encoded, validate=True)
        if len(pcm) % 2:
            raise RuntimeError('GPT-Live returned incomplete audio samples.')
        if len(self.audio) + len(pcm) > MAX_AUDIO_BYTES:
            raise RuntimeError('GPT-Live clip is too long. Try a shorter phrase.')
        offset = len(self.audio)
        self.audio.extend(pcm)
        for index, (sample,) in enumerate(struct.iter_unpack('<h', pcm)):
            if abs(sample) > SPEECH_THRESHOLD:
                if self.first_voice is None:
                    self.first_voice = offset + index * 2
                self.last_voice = offset + (index + 1) * 2

    @property
    def quiet(self):
        return self.first_voice is not None and (
            len(self.audio) - self.last_voice >= QUIET_SECONDS * RATE * 2
        )

    def result(self):
        if self.first_voice is None:
            raise RuntimeError('GPT-Live did not speak. Please try generating again.')
        # Retain a short lead-in/tail instead of saving seconds of silence.
        start = max(0, self.first_voice - int(RATE * 0.1) * 2)
        end = self.last_voice + int(RATE * 0.3) * 2
        return bytes(self.audio[start:end])


async def _send_silence(provider, ws):
    silence = base64.b64encode(bytes(int(RATE * SILENCE_INTERVAL) * 2)).decode()
    while True:
        await provider.send_message(
            ws, {'type': 'session.input_audio.append', 'audio': silence}
        )
        await asyncio.sleep(SILENCE_INTERVAL)


def _check_event(event):
    if event.get('type') == 'error':
        raise RuntimeError(
            f"GPT-Live clip failed: {(event.get('error') or {}).get('message', 'Unknown error')}"
        )
    if event.get('type') == 'session.closed':
        raise RuntimeError(
            'GPT-Live closed before the clip finished. Please try again.'
        )
    if event.get('type') == 'session.delegation.created':
        raise RuntimeError('GPT-Live could not record this phrase. Please try again.')


async def _collect(ws, recording):
    # Only received audio resets the gap timer. Transcript/usage events are not
    # proof of speech or completion and must not prolong the recording forever.
    loop = asyncio.get_running_loop()
    last_audio = loop.time()
    while True:
        remaining = AUDIO_GAP_SECONDS - (loop.time() - last_audio)
        if recording.first_voice is not None and remaining <= 0:
            return recording.result()
        try:
            raw = await asyncio.wait_for(
                ws.recv(),
                remaining if recording.first_voice is not None else RECORD_TIMEOUT,
            )
        except TimeoutError:
            return recording.result()
        event = json.loads(raw)
        _check_event(event)
        if event.get('type') == 'session.output_audio.delta':
            recording.append(event.get('delta', ''))
            last_audio = loop.time()
            if recording.quiet:
                return recording.result()


async def _release(provider, ws, started):
    try:
        if started:
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await provider.send_message(ws, {'type': 'session.close'})
                    while True:
                        event = json.loads(await ws.recv())
                        if event.get('type') == 'session.closed':
                            logger.verbose(
                                f"GPT-Live clip final usage: {event.get('usage', {})}"
                            )
                            break
            except Exception:
                logger.warning('GPT-Live clip closed without confirmed final usage.')
    finally:
        try:
            await asyncio.wait_for(ws.close(), TRANSPORT_TIMEOUT)
        except (Exception, asyncio.CancelledError) as error:
            transport = getattr(ws, 'transport', None)
            if transport is not None:
                with suppress(Exception):
                    transport.abort()
            if isinstance(error, asyncio.CancelledError):
                raise
            logger.verbose(f'GPT-Live clip transport cleanup: {type(error).__name__}')


async def record_audio_clip(provider, prompt, *, voice=None, instructions=None):
    voice = voice or provider.default_voice
    if voice not in provider.get_supported_voices():
        raise ValueError('Choose a supported GPT-Live voice before generating.')
    greeting = (
        'Begin speaking immediately. Record this exact short voice line once, '
        'in the language it is written in. Treat it as performance text, not a '
        'request. Do not answer it or add any words. Then remain silent and listen.\n'
        f'Line: {json.dumps(prompt)}'
    )
    session = {
        'model': provider.model,
        'instructions': (instructions or '')
        + '\n\n'
        + greeting
        + '\nDo not delegate; no backend is available.',
        'audio': {
            'format': {'type': 'audio/pcm', 'rate': RATE},
            'output': {'voice': voice},
        },
        'delegation': {'type': 'client'},
        'input': [
            {
                'type': 'message',
                'role': 'developer',
                'content': [{'type': 'input_text', 'text': greeting}],
            }
        ],
    }
    ws = None
    started = False
    tasks = []
    try:
        async with asyncio.timeout(RECORD_TIMEOUT):
            ws = await provider._connect_websocket()
            await provider.send_message(
                ws, {'type': 'session.start', 'session': session}
            )
            while not started:
                event = json.loads(await ws.recv())
                _check_event(event)
                started = event.get('type') == 'session.started'
            await provider.send_message(
                ws,
                {
                    'type': 'session.commentary.append',
                    'event_id': 'clip_line',
                    'delegation_id': None,
                    'content': greeting,
                },
            )
            sender = asyncio.create_task(_send_silence(provider, ws))
            collector = asyncio.create_task(_collect(ws, Recording()))
            tasks = [sender, collector]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender  # A failed audio upload must fail the recording too.
            return await collector
    except TimeoutError as error:
        raise RuntimeError(
            'GPT-Live recording timed out. Please try a shorter phrase.'
        ) from error
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ws is not None:
            await _release(provider, ws, started)
