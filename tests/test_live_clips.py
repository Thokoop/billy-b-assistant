"""Offline Live voice recordings, saved wake-up clips and MQTT announcements."""

import asyncio
import base64
import importlib
import json
import struct
import sys
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def pcm(value, seconds):
    return struct.pack('<h', value) * int(24000 * seconds)


def audio(data):
    return {
        'type': 'session.output_audio.delta',
        'delta': base64.b64encode(data).decode(),
    }


class Socket:
    def __init__(self, events=(), *, start=True, finalize=True):
        self.events = events
        self.start = start
        self.finalize = finalize
        self.inbox = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.transport = Mock()
        self.streaming = asyncio.Event()

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event['type'] == 'session.start' and self.start:
            self.inbox.put_nowait({'type': 'session.started'})
        if event['type'] == 'session.commentary.append':
            for item in self.events:
                self.inbox.put_nowait(item)
        if event['type'] == 'session.input_audio.append':
            self.streaming.set()
        if event['type'] == 'session.close' and self.finalize:
            self.inbox.put_nowait({'type': 'session.closed', 'usage': {'seconds': 4}})

    async def recv(self):
        event = await self.inbox.get()
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)

    async def close(self):
        self.closed = True


@pytest.fixture
def clips(isolated, monkeypatch):
    module = importlib.import_module('isolated_core.providers.live_clips')
    provider_module = importlib.import_module('isolated_core.providers.live_provider')
    realtime = importlib.import_module('isolated_core.providers.realtime')
    monkeypatch.setattr(
        realtime.RealtimeAIProvider,
        '_connect_websocket',
        AsyncMock(side_effect=AssertionError('Unexpected provider connection')),
    )
    monkeypatch.setattr(module, 'logger', isolated.logger)
    p = provider_module.LiveProvider('test-key')
    monkeypatch.setattr(module, 'RECORD_TIMEOUT', 0.2)
    monkeypatch.setattr(module, 'AUDIO_GAP_SECONDS', 0.03)
    monkeypatch.setattr(module, 'CLOSE_TIMEOUT', 0.02)
    monkeypatch.setattr(module, 'TRANSPORT_TIMEOUT', 0.01)
    return SimpleNamespace(module=module, provider=p, isolated=isolated)


def record(clips, ws, voice='vesper'):
    clips.provider._connect_websocket = AsyncMock(return_value=ws)
    return clips.provider.generate_wakeup_clip(
        'You rang?', voice, 'Cheeky London accent.'
    )


@pytest.mark.parametrize('voice', ['ballad', 'vesper', 'noeul'])
def test_native_live_voice_with_paced_silence_no_backend_and_finalization(clips, voice):
    async def run():
        ws = Socket([audio(pcm(0, 0.5) + pcm(900, 0.2) + pcm(0, 2.1))])
        result = await record(clips, ws, voice)
        assert result == pcm(0, 0.1) + pcm(900, 0.2) + pcm(0, 0.3)
        session = ws.sent[0]['session']
        assert session['audio']['output']['voice'] == voice
        assert session['model'] == 'gpt-live-1'
        assert session['delegation'] == {'type': 'client'}
        assert 'Cheeky London accent.' in session['instructions']
        assert 'You rang?' in session['input'][0]['content'][0]['text']
        assert session['input'][0]['role'] == 'developer'
        greeting = next(e for e in ws.sent if e['type'] == 'session.commentary.append')
        assert greeting['delegation_id'] is None
        assert greeting['content'] == session['input'][0]['content'][0]['text']
        silence = [e for e in ws.sent if e['type'] == 'session.input_audio.append']
        assert len(silence) == 1  # paced, never uploading a long file at once
        assert base64.b64decode(silence[0]['audio']) == pcm(0, 0.1)
        assert ws.sent[-1]['type'] == 'session.close'
        assert ws.closed
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(run())


def test_short_pause_does_not_cut_speech_and_no_more_audio_ends_recording(clips):
    async def run():
        ws = Socket([audio(pcm(700, 0.1) + pcm(0, 0.5))])
        task = asyncio.create_task(record(clips, ws))
        await ws.streaming.wait()
        await asyncio.sleep(0.01)
        assert not task.done()
        ws.inbox.put_nowait(audio(pcm(800, 0.1)))
        # Non-audio events do not falsely confirm speech completion.
        ws.inbox.put_nowait({
            'type': 'session.output_transcript.delta',
            'delta': 'You rang?',
        })
        assert await task == pcm(700, 0.1) + pcm(0, 0.5) + pcm(800, 0.1)
        assert ws.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    'events,match',
    [
        ([{'type': 'error', 'error': {'message': 'No access'}}], 'No access'),
        ([{'type': 'session.closed'}], 'closed before'),
        ([{'type': 'session.delegation.created'}], 'could not record'),
        ([audio(b'\x01')], 'incomplete audio'),
        ([audio(pcm(0, 2))], 'timed out|did not speak'),
        ([OSError('connection lost')], 'connection lost'),
    ],
)
def test_failed_recording_returns_no_clip_and_closes(clips, events, match):
    ws = Socket(events)
    with pytest.raises((RuntimeError, OSError), match=match):
        asyncio.run(record(clips, ws))
    assert ws.closed


def test_malformed_base64_is_rejected(clips):
    ws = Socket([{'type': 'session.output_audio.delta', 'delta': '!bad!'}])
    with pytest.raises(ValueError):
        asyncio.run(record(clips, ws))
    assert ws.closed


def test_size_limit_never_returns_truncated_speech(clips, monkeypatch):
    monkeypatch.setattr(clips.module, 'MAX_AUDIO_BYTES', 100)
    ws = Socket([audio(pcm(700, 0.1))])
    with pytest.raises(RuntimeError, match='too long'):
        asyncio.run(record(clips, ws))
    assert ws.closed


def test_startup_is_bounded_and_does_not_send_commands_before_started(
    clips, monkeypatch
):
    monkeypatch.setattr(clips.module, 'RECORD_TIMEOUT', 0.02)
    ws = Socket(start=False)
    with pytest.raises(RuntimeError, match='timed out'):
        asyncio.run(record(clips, ws))
    assert [e['type'] for e in ws.sent] == ['session.start']
    assert ws.closed


def test_silence_sender_failure_is_not_hidden(clips):
    ws = Socket()
    send = ws.send

    async def fail(raw):
        if json.loads(raw)['type'] == 'session.input_audio.append':
            raise OSError('upload failed')
        await send(raw)

    ws.send = fail
    with pytest.raises(OSError, match='upload failed'):
        asyncio.run(record(clips, ws))
    assert ws.closed


def test_recording_cancellation_survives_transport_timeout(clips):
    async def run():
        ws = Socket(finalize=False)
        ws.close = AsyncMock(side_effect=lambda: None)

        async def stuck():
            await asyncio.Event().wait()

        ws.close.side_effect = stuck
        task = asyncio.create_task(record(clips, ws))
        await ws.streaming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        ws.transport.abort.assert_called_once()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    asyncio.run(run())


def test_close_timeout_does_not_discard_an_already_recorded_clip(clips):
    ws = Socket([audio(pcm(700, 0.1) + pcm(0, 2))], finalize=False)
    ws.close = AsyncMock(side_effect=TimeoutError)
    assert asyncio.run(record(clips, ws)) == pcm(700, 0.1) + pcm(0, 0.3)
    ws.transport.abort.assert_called_once()
    clips.isolated.logger.warning.assert_called_once()


def test_invalid_voice_does_not_silently_use_marin(clips):
    clips.provider._connect_websocket = AsyncMock()
    with pytest.raises(ValueError, match='supported GPT-Live voice'):
        asyncio.run(clips.provider.generate_wakeup_clip('Hello', voice='typo'))
    clips.provider._connect_websocket.assert_not_called()


@pytest.mark.parametrize('name', ['openai', 'xai'])
def test_other_providers_keep_literal_wakeup_rendering(isolated, name):
    module = importlib.import_module(f'isolated_core.providers.{name}_provider')
    cls = module.OpenAIProvider if name == 'openai' else module.XAIProvider
    provider = cls('test-key')
    provider.generate_audio_clip = AsyncMock(return_value=b'pcm')
    assert (
        asyncio.run(provider.generate_wakeup_clip('Hello', 'voice', 'Style')) == b'pcm'
    )
    provider.generate_audio_clip.assert_awaited_once_with(
        'Hello', voice='voice', instructions='Style', strict_literal=True
    )


@pytest.fixture
def wakeup(isolated, monkeypatch, tmp_path):
    monkeypatch.setitem(
        sys.modules, 'billy_runtime.paths', SimpleNamespace(INSTALL_ROOT=tmp_path)
    )
    isolated.config.CUSTOM_INSTRUCTIONS = 'Fallback persona'
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_persona_instructions=lambda _: 'Cheeky Billy'
            )
        ),
    )
    module = isolated.load('wakeup')
    provider = SimpleNamespace(
        generate_wakeup_clip=AsyncMock(return_value=pcm(700, 0.1))
    )
    monkeypatch.setattr(
        module,
        'voice_provider_registry',
        SimpleNamespace(get_provider=lambda: provider),
    )
    return module, provider, tmp_path


def test_wakeup_generation_writes_valid_wav_with_chosen_voice_and_mood(wakeup):
    module, provider, root = wakeup
    result = asyncio.run(
        module.WakeupClipGenerator(voice='vesper', persona_name='test').generate(
            'Hello', 1, ['calm']
        )
    )
    assert result == str(root / 'personas/test/wakeup/1.wav')
    call = provider.generate_wakeup_clip.call_args.kwargs
    assert call['voice'] == 'vesper'
    assert 'calm' in call['instructions'] and 'Cheeky Billy' in call['instructions']
    with wave.open(result) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (
            1,
            2,
            24000,
        )
        assert wav.readframes(24000) == pcm(700, 0.1)


@pytest.mark.parametrize('failure', ['generation', 'empty', 'partial', 'write'])
def test_failed_wakeup_preserves_existing_clip(wakeup, monkeypatch, failure):
    module, provider, root = wakeup
    path = root / 'sounds/wake-up/custom/1.wav'
    path.write_bytes(b'previous clip')
    if failure == 'generation':
        provider.generate_wakeup_clip.side_effect = RuntimeError('failed')
    elif failure in {'empty', 'partial'}:
        provider.generate_wakeup_clip.return_value = b'' if failure == 'empty' else b'x'
    else:
        monkeypatch.setattr(
            module.os, 'replace', Mock(side_effect=OSError('disk full'))
        )
    with pytest.raises((RuntimeError, OSError)):
        asyncio.run(module.WakeupClipGenerator(voice='ballad').generate('Hello', 1))
    assert path.read_bytes() == b'previous clip'
    assert list(path.parent.iterdir()) == [path]


@pytest.fixture
def say_runtime(clips, monkeypatch):
    config = clips.isolated.config
    config.REALTIME_AI_PROVIDER = 'gpt-live'
    playback = SimpleNamespace(
        ensure_playback_worker_started=Mock(),
        playback_done_event=asyncio.Event(),
        playback_queue=Mock(),
    )
    monkeypatch.setitem(sys.modules, 'isolated_core.audio', playback)
    mode = SimpleNamespace(active=Mock(return_value=False))
    monkeypatch.setitem(
        sys.modules, 'isolated_core.bluetooth', SimpleNamespace(mode=mode)
    )
    persona = SimpleNamespace(get_current_persona_voice=Mock(return_value='vesper'))
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(persona_manager=persona),
    )
    session_factory = Mock()
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(BillySession=session_factory),
    )
    realtime = importlib.import_module('isolated_core.providers.realtime')
    provider_lookup = Mock(return_value=clips.provider)
    monkeypatch.setattr(
        realtime.voice_provider_registry, 'get_provider', provider_lookup
    )
    module = clips.isolated.load('say')
    return SimpleNamespace(
        module=module,
        playback=playback,
        mode=mode,
        persona=persona,
        session_factory=session_factory,
        provider_lookup=provider_lookup,
    )


@pytest.mark.parametrize('voice', ['ballad', 'vesper', 'noeul'])
@pytest.mark.parametrize('interactive', [None, False])
def test_mqtt_literal_say_uses_profile_voice_in_native_live_recording(
    clips, say_runtime, voice, interactive
):
    runtime = say_runtime
    runtime.persona.get_current_persona_voice.return_value = voice
    expected = pcm(900, 0.2) + pcm(0, 0.3)
    ws = Socket([audio(pcm(900, 0.2) + pcm(0, 2.1))])
    clips.provider._connect_websocket = AsyncMock(return_value=ws)
    asyncio.run(runtime.module.say('"Het eten is klaar."', interactive=interactive))
    session = ws.sent[0]['session']
    assert session['audio']['output']['voice'] == voice
    assert session['model'] == clips.provider.model
    assert session['delegation'] == {'type': 'client'}
    line = session['input'][0]['content'][0]['text']
    assert 'Het eten is klaar.' in line and 'wake-up' not in line
    assert 'no added words' in session['instructions']
    runtime.provider_lookup.assert_called_once_with('gpt-live')
    runtime.playback.playback_queue.put.assert_called_once_with(expected)
    runtime.session_factory.assert_not_called()
    assert ws.closed and ws.sent[-1]['type'] == 'session.close'


def test_say_picks_current_voice_again_for_each_announcement(clips, say_runtime):
    sockets = [Socket([audio(pcm(900, 0.2) + pcm(0, 2.1))]) for _ in range(2)]
    clips.provider._connect_websocket = AsyncMock(side_effect=sockets)
    say_runtime.persona.get_current_persona_voice.side_effect = ['vesper', 'quartz']

    async def run():
        await say_runtime.module.say('First announcement')
        await say_runtime.module.say('After changing persona')

    asyncio.run(run())
    assert [ws.sent[0]['session']['audio']['output']['voice'] for ws in sockets] == [
        'vesper',
        'quartz',
    ]


@pytest.mark.parametrize('stage', ['before', 'during'])
def test_say_respects_bluetooth_ownership_before_and_after_recording(
    clips, say_runtime, stage
):
    if stage == 'before':
        say_runtime.mode.active.return_value = True
    else:
        say_runtime.mode.active.side_effect = [False, True]
    clips.provider.generate_audio_clip = AsyncMock(return_value=b'pcm')
    asyncio.run(say_runtime.module.say('Hello'))
    if stage == 'before':
        clips.provider.generate_audio_clip.assert_not_awaited()
        say_runtime.playback.ensure_playback_worker_started.assert_not_called()
    else:
        clips.provider.generate_audio_clip.assert_awaited_once()
    say_runtime.playback.playback_queue.put.assert_not_called()
    say_runtime.session_factory.assert_not_called()


@pytest.mark.parametrize('failure', ['error', 'timeout', 'too_long'])
def test_failed_live_say_never_plays_partial_audio_or_changes_voice(
    clips, say_runtime, monkeypatch, failure
):
    events = [audio(pcm(900, 0.2))]
    if failure == 'error':
        events.append({'type': 'error', 'error': {'message': 'Unavailable'}})
    elif failure == 'timeout':
        monkeypatch.setattr(clips.module, 'RECORD_TIMEOUT', 0.01)
        monkeypatch.setattr(clips.module, 'AUDIO_GAP_SECONDS', 1)
    else:
        monkeypatch.setattr(clips.module, 'MAX_AUDIO_BYTES', 100)
    ws = Socket(events)
    clips.provider._connect_websocket = AsyncMock(return_value=ws)
    with pytest.raises(RuntimeError):
        asyncio.run(say_runtime.module.say('Hello'))
    say_runtime.playback.playback_queue.put.assert_not_called()
    clips.provider._connect_websocket.assert_awaited_once()
    assert ws.sent[0]['session']['audio']['output']['voice'] == 'vesper'
    assert ws.closed


@pytest.mark.parametrize('name,voice', [('openai', 'ballad'), ('xai', 'Ara')])
def test_say_keeps_configured_non_live_provider(clips, say_runtime, name, voice):
    clips.isolated.config.REALTIME_AI_PROVIDER = name
    provider = SimpleNamespace(generate_audio_clip=AsyncMock(return_value=b'pcm'))
    say_runtime.provider_lookup.return_value = provider
    say_runtime.persona.get_current_persona_voice.return_value = voice
    asyncio.run(say_runtime.module.say('Hello'))
    say_runtime.provider_lookup.assert_called_once_with(name)
    call = provider.generate_audio_clip.call_args.kwargs
    assert call['voice'] == voice and call['prompt'] == 'Hello'
    assert call['strict_literal'] is True
    say_runtime.playback.playback_queue.put.assert_called_once_with(b'pcm')
    say_runtime.session_factory.assert_not_called()
