"""Hardware-free acceptance for interruption, AEC fallback and startup order."""

import asyncio
import contextlib
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import numpy as np
import pytest
from conftest import definitions
from test_realtime_audio import handler  # noqa: F401


@pytest.mark.parametrize('provider', ['openai', 'xai'])
@pytest.mark.parametrize('generating', [True, False])
def test_button_snapshots_before_stop_and_rejects_late_audio(provider, generating):
    audio = Mock()
    events = []
    audio.stop_playback.side_effect = lambda: events.append('stop')
    ns = dict(
        asyncio=asyncio,
        contextlib=contextlib,
        time=time,
        logger=Mock(),
        audio=audio,
        show_status_led_interruption=Mock(),
        MANUAL_HANDOFF_ECHO_SETTLE_SECONDS=1.5,
    )
    methods = definitions(
        'core/session_manager.py',
        ['interrupt_to_user_turn', '_on_audio_out'],
        ns,
        cls='BillySession',
    )

    async def run():
        s = Mock()
        s.session_active.is_set.return_value = True
        s.state = SimpleNamespace(response_active=generating)
        s.realtime_ai_provider.get_provider_name.return_value = provider
        s._current_response_id = 'r1'
        s._active_server_speech_item_id = None
        for name in [
            '_manual_handoff_discard_item_ids',
            '_assistant_overlap_speech_item_ids',
            '_locally_confirmed_barge_in_item_ids',
            '_cancelled_response_ids',
        ]:
            setattr(s, name, set())
        s._interrupted_response_ids = deque(maxlen=64)
        s._client_managed_vad = True
        s.last_activity = [0]
        s._ws_send_json = AsyncMock()
        s._verify_interruption_handoff = AsyncMock()
        s.mic_manager.start_after_playback = AsyncMock(return_value=True)
        s.mic_manager.reset_uplink = AsyncMock()
        point = dict(item_id='heard-item', content_index=0, audio_end_ms=520)
        s.audio_handler.interruption_points.side_effect = lambda: (
            events.append('snapshot') or [point]
        )
        s.audio_handler.clear_buffer.side_effect = lambda: events.append('clear')
        await methods.interrupt_to_user_turn(s)
        await asyncio.sleep(0)
        assert events[:3] == ['snapshot', 'stop', 'clear']
        payloads = [call.args[0] for call in s._ws_send_json.call_args_list]
        assert any(p['type'] == 'response.cancel' for p in payloads) == generating
        truncates = [p for p in payloads if p['type'] == 'conversation.item.truncate']
        assert truncates == (
            [dict(type='conversation.item.truncate', **point)]
            if provider == 'openai'
            else []
        )
        s._cancelled_response_ids.clear()  # response.done can release its short-lived state
        methods._on_audio_out(s, {'response_id': 'r1'})
        s.audio_handler.on_audio_delta.assert_not_called()
        methods._on_audio_out(s, {'response_id': 'new'})
        s.audio_handler.on_audio_delta.assert_called_once()

    asyncio.run(run())


def test_aec_failure_chunk_is_never_forwarded_during_playback():
    a = Mock()
    a.echo_cancellation_active.side_effect = [True, False]
    a.playback_done_event.is_set.return_value = False
    a.process_mic_with_aec.side_effect = lambda samples, **kw: samples
    method = definitions(
        'core/session/mic_manager_wrapper.py',
        ['_process_microphone'],
        dict(audio=a, mic_input_samples_for_meter=lambda x: x),
        cls='MicManagerWrapper',
    )._process_microphone
    s = Mock()
    s.session.session_active.is_set.return_value = True
    s.session.state.allow_mic_input = False
    s.session.state.assistant_speaking = True
    method(s, np.arange(960, dtype=np.int16))
    s._uplink.submit.assert_not_called()
    s._store_prebuffer.assert_not_called()
    s.reset_barge_in_evidence.assert_called_once()
    s._clear_prebuffer.assert_called_once()


@pytest.mark.parametrize('failure', ['capture', 'render', 'initialize'])
def test_aec_failure_latches_until_explicit_session_retry(isolated, failure):
    echo = isolated.load('echo_canceller')
    bad, good = Mock(), Mock()
    bad.process_capture.side_effect = RuntimeError('DSP failed')
    bad.process_render.side_effect = RuntimeError('render failed')
    good.process_capture.side_effect = lambda x: x
    good.voice_detected.return_value = True
    factory = Mock(
        side_effect=[
            RuntimeError('init failed') if failure == 'initialize' else bad,
            good,
        ]
    )
    aec = echo.EchoCanceller(True, factory)
    samples = np.zeros(480, dtype=np.int16)
    if failure == 'render':
        aec.feed_render(samples, 48000)
    else:
        aec.process_capture(samples, 48000)
    assert not aec.healthy
    for _ in range(20):
        aec.process_capture(samples, 48000)
    assert factory.call_count == 1
    assert aec.recover()
    aec.process_capture(samples, 48000)
    assert aec.healthy and aec.voice_detected
    assert factory.call_count == 2
    assert aec.recover() and factory.call_count == 2


def device(name, inputs=0, outputs=0):
    return dict(name=name, max_input_channels=inputs, max_output_channels=outputs)


@pytest.mark.parametrize('reverse', [False, True])
def test_device_order_does_not_probe_unrelated_inputs(audio_module, reverse):
    a = audio_module
    devices = [device('broken', 8), device('mic', 1), device('speaker', outputs=2)]
    if reverse:
        devices.reverse()
    a.sd.query_devices = lambda: devices
    a.sd.check_output_settings = Mock()
    a._pick_mic_rate = Mock(return_value=24000)
    a.detect_devices()
    selected = next(i for i, d in enumerate(devices) if d['name'] == 'mic')
    a._pick_mic_rate.assert_called_once_with(selected, 1)
    assert selected == a.MIC_DEVICE_INDEX
    assert a.MIC_CHANNELS == 1 and a.CHUNK_SIZE == 960
    a.sd.check_output_settings.assert_called_once()


@pytest.mark.parametrize(
    'failure',
    ['missing_mic', 'missing_speaker', 'unsupported_mic', 'unsupported_output'],
)
def test_failed_detection_does_not_publish_partial_settings(audio_module, failure):
    a = audio_module
    devices = [device('mic', 1), device('speaker', outputs=2)]
    if failure == 'missing_mic':
        devices.pop(0)
    if failure == 'missing_speaker':
        devices.pop()
    a.sd.query_devices = lambda: devices
    a._pick_mic_rate = Mock(return_value=48000)
    a.sd.check_output_settings = Mock()
    if failure == 'unsupported_mic':
        a._pick_mic_rate.side_effect = RuntimeError('rate')
    if failure == 'unsupported_output':
        a.sd.check_output_settings.side_effect = RuntimeError('output')
    with pytest.raises(RuntimeError):
        a.detect_devices()
    assert a.MIC_DEVICE_INDEX is None and a.MIC_RATE is None
    assert a.OUTPUT_DEVICE_INDEX is None


def test_initialize_closes_probe_streams_before_returning(audio_module):
    a = audio_module
    events = []
    a.detect_devices = Mock()
    a.sd.InputStream = MagicMock()
    a.sd.InputStream.return_value.__exit__.side_effect = lambda *a: events.append(
        'mic closed'
    )
    a.sd.OutputStream.return_value.__exit__.side_effect = lambda *a: events.append(
        'speaker closed'
    )
    a.initialize_devices()
    assert events == ['speaker closed', 'mic closed']


@pytest.mark.parametrize('fail_probe', [False, True])
def test_startup_does_not_start_consumers_or_mark_ready_before_audio(
    monkeypatch, fail_probe
):
    import sys

    events = []

    def record(name):
        return lambda *a, **kw: events.append(name)

    user = Mock(load_default_user=record('profile'))
    monkeypatch.setitem(
        sys.modules, 'core.profile_manager', SimpleNamespace(user_manager=user)
    )

    def probe():
        events.append('audio')
        if fail_probe:
            raise RuntimeError('missing device')

    def loop(*, on_ready):
        events.append('button ready')
        on_ready()

    class Thread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    ns = dict(
        clear_ready=record('clear'),
        signal=Mock(),
        initialize_status_led=Mock(),
        set_status_led_state=Mock(),
        initialize_devices=probe,
        start_motor_watchdog=record('watchdog'),
        core=SimpleNamespace(button=SimpleNamespace(start_loop=loop)),
        bluetooth_watcher=SimpleNamespace(start=record('bluetooth')),
        threading=SimpleNamespace(Thread=Thread),
        start_mqtt=record('mqtt'),
        mark_ready=record('ready'),
        signal_handler=Mock(),
    )
    main = definitions('main.py', ['main'], ns).main
    if fail_probe:
        with pytest.raises(RuntimeError):
            main()
        assert events == ['clear', 'profile', 'audio']
    else:
        main()
        assert events == [
            'clear',
            'profile',
            'audio',
            'watchdog',
            'button ready',
            'bluetooth',
            'mqtt',
            'ready',
        ]


@pytest.mark.parametrize(
    'heard,expected',
    [
        (500, [('one', 500), ('two', 0)]),
        (1500, [('two', 500)]),
        (9999, [('two', 1000)]),
    ],
)
def test_multi_item_interruption_also_removes_fully_unheard_items(
    audio_module, handler, heard, expected
):
    import base64

    for item_id in ['one', 'two']:
        handler.on_audio_delta(
            dict(item_id=item_id, delta=base64.b64encode(b'\0\0' * 24000).decode())
        )
    audio_module.aec_heard_audio_ms = Mock(return_value=heard)
    assert [
        (p['item_id'], p['audio_end_ms']) for p in handler.interruption_points()
    ] == expected


@pytest.mark.parametrize('failure', ['open', 'start', 'latency'])
def test_mic_open_failure_never_uses_unselected_default(
    audio_module, isolated, failure
):
    a = audio_module
    a.MIC_RATE = 24000
    a.MIC_DEVICE_INDEX = 3
    a.MIC_CHANNELS = 1
    a.CHUNK_SIZE = 960
    a.sd.InputStream = Mock()
    stream = a.sd.InputStream.return_value
    stream.latency = 0.02
    a.set_aec_capture_latency = Mock()
    if failure == 'open':
        a.sd.InputStream.side_effect = RuntimeError('open failed')
    if failure == 'start':
        stream.start.side_effect = RuntimeError('start failed')
    if failure == 'latency':
        a.set_aec_capture_latency.side_effect = RuntimeError('DSP failed')
    mic = isolated.load('mic').MicManager()
    with pytest.raises(RuntimeError):
        mic.start(Mock())
    assert a.sd.InputStream.call_count == 1
    assert a.sd.InputStream.call_args.kwargs['device'] == 3
    assert mic.stream is None
    if failure != 'open':
        stream.close.assert_called_once()


def test_mic_close_attempted_even_when_abort_and_stop_fail(audio_module, isolated):
    mic = isolated.load('mic').MicManager()
    stream = Mock()
    stream.abort.side_effect = OSError('abort')
    stream.stop.side_effect = OSError('stop')
    mic.stream = stream
    mic.stop()
    stream.close.assert_called_once()
    assert mic.stream is None
