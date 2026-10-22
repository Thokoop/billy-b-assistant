"""Regression reproductions for the core review findings fixed in phases 4–6."""

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from conftest import definitions


def test_ai_does_not_advertise_failed_aec(isolated):
    echo = isolated.load('echo_canceller')
    processor = Mock()
    processor.process_capture.side_effect = RuntimeError('DSP failed')
    aec = echo.EchoCanceller(True, lambda **kwargs: processor)
    aec.process_capture(np.zeros(480, dtype=np.int16), 48000)
    active = definitions(
        'core/audio.py', ['echo_cancellation_active'], {'echo_canceller': aec}
    ).echo_cancellation_active
    assert not active()


def test_manual_interruption_truncates_unheard_audio():
    ns = dict(
        asyncio=asyncio,
        contextlib=contextlib,
        time=time,
        logger=Mock(),
        audio=Mock(),
        show_status_led_interruption=Mock(),
        MANUAL_HANDOFF_ECHO_SETTLE_SECONDS=1.5,
    )
    interrupt = definitions(
        'core/session_manager.py', ['interrupt_to_user_turn'], ns, cls='BillySession'
    ).interrupt_to_user_turn

    async def scenario():
        s = Mock()
        s.session_active.is_set.return_value = True
        s.state = SimpleNamespace(response_active=True)
        s.realtime_ai_provider.get_provider_name.return_value = 'openai'
        s._current_response_id = 'response1'
        s._active_server_speech_item_id = None
        s._manual_handoff_discard_item_ids = set()
        s._assistant_overlap_speech_item_ids = set()
        s._locally_confirmed_barge_in_item_ids = set()
        s._cancelled_response_ids = set()
        s._interrupted_response_ids = []
        s.mic_manager.reset_uplink = AsyncMock()
        s._client_managed_vad = True
        s.last_activity = [0]
        s._ws_send_json = AsyncMock()
        s._verify_interruption_handoff = AsyncMock()
        s.mic_manager.start_after_playback = AsyncMock(return_value=True)
        s.audio_handler.interruption_points.return_value = [
            dict(item_id='item1', content_index=0, audio_end_ms=500)
        ]
        await interrupt(s)
        await asyncio.sleep(0)
        assert any(
            c.args[0]['type'] == 'conversation.item.truncate'
            for c in s._ws_send_json.call_args_list
        )

    asyncio.run(scenario())


def test_clip_generation_reports_provider_error():
    collect = definitions(
        'core/realtime_ai_provider.py',
        ['_collect_audio_response'],
        {
            'json': json,
            'asyncio': asyncio,
            'base64': __import__('base64'),
            'CLIP_TIMEOUT_SECONDS': 1,
            'MAX_CLIP_BYTES': 1024,
        },
        cls='RealtimeAIProvider',
    )._collect_audio_response

    async def websocket():
        yield json.dumps({'type': 'error', 'error': {'message': 'invalid session'}})
        await asyncio.Event().wait()

    async def scenario():
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(collect(None, websocket()), timeout=0.02)

    asyncio.run(scenario())


def test_unusable_unselected_mic_does_not_hide_selected_device():
    def pick(index, channels):
        if index == 0:
            raise RuntimeError('Unrelated device cannot capture')
        return 24000

    ns = dict(
        sd=SimpleNamespace(
            query_devices=lambda: [
                dict(name='other', max_input_channels=1, max_output_channels=0),
                dict(name='selected', max_input_channels=1, max_output_channels=0),
                dict(name='speaker', max_input_channels=0, max_output_channels=2),
            ],
            check_output_settings=Mock(),
        ),
        logger=Mock(),
        MIC_DEVICE_INDEX=None,
        OUTPUT_DEVICE_INDEX=2,
        MIC_PREFERENCE='selected',
        SPEAKER_PREFERENCE='speaker',
        _preference_matches_device=lambda preference, name: preference == name,
        _pick_mic_rate=pick,
        PROVIDER_MIC_RATE=24000,
        CHUNK_MS=40,
        TEXT_ONLY_MODE=False,
    )
    definitions('core/audio.py', ['detect_devices'], ns).detect_devices()
    assert ns['MIC_DEVICE_INDEX'] == 1 and ns['MIC_RATE'] == 24000
