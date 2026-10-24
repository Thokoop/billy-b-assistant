"""Contracts for Realtime PCM, playback completion and interruption."""

import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from conftest import definitions


@pytest.fixture
def handler(audio_module):
    a = audio_module
    cls = definitions(
        'application/core/session/audio_handler.py',
        ['AudioHandler'],
        dict(
            asyncio=asyncio,
            base64=base64,
            time=time,
            audio=a,
            TEXT_ONLY_MODE=False,
            CHUNK_MS=40,
            logger=Mock(),
            _PROVIDER_OUTPUT_BYTES_PER_MS=48,
        ),
    ).AudioHandler
    owner = SimpleNamespace(
        state=SimpleNamespace(assistant_speaking=False, _turn_had_speech=False),
        last_activity=[0],
        interrupt_event=threading.Event(),
        session_active=threading.Event(),
    )
    owner.session_active.set()
    return cls(owner)


def test_realtime_delta_keeps_24k_input_and_48k_stereo_playback(audio_module, handler):
    a = audio_module
    pcm = np.full(960, 2000, dtype=np.int16)  # 40ms at 24k.
    handler.on_audio_delta(
        dict(
            type='response.output_audio.delta',
            delta=base64.b64encode(pcm).decode(),
            item_id='item1',
            content_index=0,
        )
    )
    assert bytes(handler.audio_buffer) == pcm.tobytes()
    assert handler.session.state.assistant_speaking
    a.playback_queue.put(None)
    a.playback_worker(40)
    output = a.test_stream.write.call_args.args[0]
    assert output.shape == (1920, 2) and output.dtype == np.int16
    np.testing.assert_array_equal(output[:, 0], output[:, 1])
    assert abs(int(output[960, 0]) - 1000) <= 2
    reference, rate, generation = a.echo_canceller.feed_render.call_args.args
    assert (rate, generation) == (48000, 1)
    np.testing.assert_array_equal(reference, output[:, 0])
    asyncio.run(handler.wait_for_playback_complete())
    assert a.playback_queue.unfinished_tasks == 0


def test_cancelled_tts_generation_is_not_played(audio_module, handler):
    a = audio_module
    handler.on_audio_delta(
        dict(delta=base64.b64encode(b'\x01\x00' * 960).decode(), item_id='item1')
    )
    a.echo_canceller.is_current_render_generation.return_value = False
    a.playback_queue.put(None)
    a.playback_worker(40)
    a.test_stream.write.assert_not_called()
    assert a.playback_queue.unfinished_tasks == 0


def test_interruption_position_still_tracks_each_provider_item(audio_module, handler):
    pcm = base64.b64encode(b'\x01\x00' * 24000).decode()
    handler.on_audio_delta(dict(delta=pcm, item_id='first', content_index=0))
    handler.on_audio_delta(dict(delta=pcm, item_id='second', content_index=1))
    audio_module.echo_canceller.heard_audio_ms.return_value = 1250
    assert handler.interruption_point() == dict(
        item_id='second', content_index=1, audio_end_ms=250
    )
    audio_module.stop_playback()
    handler.clear_buffer()
    assert handler.interruption_point() is None


def finish_method(audio):
    return definitions(
        'application/core/session_manager.py',
        ['_wait_for_response_playback'],
        dict(
            asyncio=asyncio,
            audio=audio,
            logger=Mock(),
            PLAYBACK_DRAIN_TIMEOUT_SECONDS=0.05,
            POST_PLAYBACK_ECHO_SETTLE_SECONDS=0.5,
            time=time,
        ),
        cls='BillySession',
    )._wait_for_response_playback


def finishing_session():
    s = SimpleNamespace(
        audio_handler=Mock(),
        state=Mock(_skip_post_response_once=False),
        last_activity=[0],
        _complete_response=AsyncMock(),
        stop_session=AsyncMock(),
        _cancelled_response_ids={'response1'},
    )
    s.session_active = threading.Event()
    s.session_active.set()
    s.audio_handler.wait_for_playback_complete = AsyncMock(return_value=True)
    return s


def test_healthy_response_drains_before_followup(audio_module):
    s = finishing_session()
    asyncio.run(finish_method(audio_module)(s, 'response1'))
    s.audio_handler.wait_for_playback_complete.assert_awaited_once_with(timeout=0.05)
    s.audio_handler.save_response_audio.assert_called_once()
    s.audio_handler.signal_playback_done.assert_called_once()
    s._complete_response.assert_awaited_once()
    s.stop_session.assert_not_called()


def test_late_interruption_still_skips_followup(audio_module):
    s = finishing_session()
    s.state._skip_post_response_once = True
    asyncio.run(finish_method(audio_module)(s, 'response1'))
    s._complete_response.assert_not_called()
    s.audio_handler.save_response_audio.assert_not_called()
    assert 'response1' not in s._cancelled_response_ids
    assert not s.state._skip_post_response_once


@pytest.mark.parametrize('failure', ['device', 'timeout'])
def test_failed_playback_ends_session_instead_of_opening_followup(
    audio_module, failure
):
    a = audio_module
    s = finishing_session()
    s.audio_handler.wait_for_playback_complete.side_effect = (
        a.PlaybackError('lost speaker') if failure == 'device' else TimeoutError()
    )
    asyncio.run(finish_method(a)(s, 'response1'))
    s.stop_session.assert_awaited_once_with(
        reason='playback_failed' if failure == 'device' else 'playback_timeout'
    )
    s._complete_response.assert_not_called()
    s.audio_handler.save_response_audio.assert_not_called()


@pytest.mark.parametrize('text_only', [False, True])
def test_openai_session_configuration_stays_24k_and_preserves_turn_controls(text_only):
    method = definitions(
        'application/core/providers/openai_provider.py',
        ['_get_initial_session_config'],
        {},
        cls='OpenAIProvider',
    )._get_initial_session_config
    tools = [{'name': 'conversation_state', 'type': 'function'}]
    result = method(
        SimpleNamespace(default_voice='cedar'),
        'Billy instructions',
        tools,
        text_only_mode=text_only,
        voice='marin',
        server_vad_params={'threshold': 0.6, 'silence_duration_ms': 500},
        create_response=False,
        interrupt_response=False,
    )
    assert result['type'] == 'session.update'
    s = result['session']
    assert s['type'] == 'realtime'
    assert s['instructions'] == 'Billy instructions' and s['tools'] == tools
    assert s['audio']['input'] == dict(
        format={'type': 'audio/pcm', 'rate': 24000},
        turn_detection=dict(
            type='server_vad',
            threshold=0.6,
            silence_duration_ms=500,
            create_response=False,
            interrupt_response=False,
        ),
    )
    if text_only:
        assert 'output' not in s['audio']
    else:
        assert s['audio']['output'] == dict(
            format={'type': 'audio/pcm', 'rate': 24000}, voice='marin', speed=1.0
        )


def test_mic_payload_keeps_provider_format_after_call(audio_module, monkeypatch):
    a = audio_module
    a.MIC_RATE = 48000
    received = []

    async def scenario():
        ws = SimpleNamespace(closed=False, send=AsyncMock())
        future = a.send_mic_audio(
            ws, np.ones(480, dtype=np.int16), asyncio.get_running_loop()
        )
        await asyncio.wrap_future(future)
        received.append(json.loads(ws.send.call_args.args[0]))

    asyncio.run(scenario())
    assert received[0]['type'] == 'input_audio_buffer.append'
    assert len(base64.b64decode(received[0]['audio'])) == 240 * 2
    assert a.MIC_RATE == 48000 and a.PROVIDER_MIC_RATE == 24000


def test_bluetooth_to_realtime_preserves_separate_aec_and_audio_rates(
    audio_module, handler
):
    a = audio_module
    call_aec = Mock()
    a.playback_queue.put((
        'bluetooth',
        np.zeros(480, dtype=np.int16).tobytes(),
        call_aec,
    ))
    handler.on_audio_delta(
        dict(delta=base64.b64encode(b'\x01\x00' * 960).decode(), item_id='ai-item')
    )
    a.playback_queue.put(None)
    a.playback_worker(40)
    assert [c.args[0].shape for c in a.test_stream.write.call_args_list] == [
        (480, 2),
        (1920, 2),
    ]
    call_aec.feed_render.assert_called_once()
    a.echo_canceller.feed_render.assert_called_once()
    assert (
        a.echo_canceller.feed_render.call_args.args[2] == handler._playback_generation
    )
    asyncio.run(handler.wait_for_playback_complete())


def test_stopped_ai_drain_releases_without_discarding_bluetooth_audio(
    audio_module, handler
):
    a = audio_module
    s = finishing_session()
    handler.session = s
    s.audio_handler = handler
    a.playback_queue.put(('bluetooth', b'\x00\x00' * 480, Mock()))

    async def scenario():
        waiter = asyncio.create_task(finish_method(a)(s, 'old-response'))
        await asyncio.sleep(0)
        s.session_active.clear()  # request_stop during Bluetooth takeover
        a.playback_queue.mark_progress()
        a.playback_queue.put(('bluetooth', b'\x00\x00' * 480, Mock()))
        await asyncio.wait_for(waiter, timeout=0.2)

    asyncio.run(scenario())
    s._complete_response.assert_not_called()
    s.stop_session.assert_not_called()
    assert a.playback_queue.qsize() == 2
    assert a.playback_queue.unfinished_tasks == 2
    a.playback_queue.discard_pending()


@pytest.mark.parametrize('mode', ['tts', 'clip'])
@pytest.mark.parametrize('level', [0, 32, -32])
def test_silent_live_playback_does_not_trigger_head_or_tail_interludes(
    audio_module, monkeypatch, mode, level
):
    a = audio_module
    monkeypatch.setattr(a.random, 'randint', lambda *_: 1920)
    # Continuous silence must still be written for Live timing and AEC, but
    # must not count as speaking time for head/tail gestures.
    pcm = np.full(960 * 50, level, dtype=np.int16).tobytes()
    a.playback_queue.put(('tts', pcm, 1) if mode == 'tts' else pcm)
    a.playback_queue.put(None)
    a.playback_worker(40)
    assert a.test_stream.write.call_count == 50
    assert a.echo_canceller.mark_render_written.call_count == 50
    a.interlude.assert_not_called()
    assert a.playback_queue.unfinished_tasks == 0


@pytest.mark.parametrize('mode', ['tts', 'clip'])
@pytest.mark.parametrize('level', [2000, -32768])
def test_spoken_audio_keeps_interludes_but_silent_gaps_do_not_advance_them(
    audio_module, monkeypatch, mode, level
):
    a = audio_module
    monkeypatch.setattr(a.random, 'randint', lambda *_: 1920)
    gestures_at = []
    a.interlude.side_effect = lambda: gestures_at.append(a.test_stream.write.call_count)
    for pcm in [
        np.full(960, level, dtype=np.int16),
        np.zeros(960 * 10, dtype=np.int16),
        np.full(960, level, dtype=np.int16),
    ]:
        a.playback_queue.put(
            ('tts', pcm.tobytes(), 1) if mode == 'tts' else pcm.tobytes()
        )
    a.playback_queue.put(None)
    a.playback_worker(40)
    assert a.test_stream.write.call_count == 12
    assert gestures_at == [12]  # The second audible frame, never during the pause.
    assert a.playback_queue.unfinished_tasks == 0
