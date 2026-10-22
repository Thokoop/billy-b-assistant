"""Real session methods with fake devices and persistence; no API connection."""

import asyncio
import contextlib
import inspect
import os
import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import definitions


def stop_method(users):
    return definitions(
        'core/session_manager.py',
        ['stop_session'],
        dict(
            asyncio=asyncio,
            contextlib=contextlib,
            inspect=inspect,
            logger=Mock(),
            user_manager=users,
        ),
        cls='BillySession',
    ).stop_session


def session():
    active = threading.Event()
    active.set()
    return SimpleNamespace(
        _stopping=False,
        tool_calls=Mock(),
        _tool_args_buffer={},
        _interaction_count_recorded=False,
        session_active=active,
        mic_manager=Mock(),
        _close_ws=AsyncMock(),
    )


@pytest.mark.parametrize('failure', ['profile', 'mic', 'none'])
def test_stop_closes_hardware_even_if_profile_or_mic_fails(failure):
    users = Mock()
    s = session()
    events = []

    def stop_mic():
        assert not s.session_active.is_set()
        events.append('mic')
        if failure == 'mic':
            raise OSError('device gone')

    def save():
        events.append('save')
        if failure == 'profile':
            raise OSError('disk full')

    async def close():
        events.append('websocket')

    users.increment_current_user_interaction_count.side_effect = save
    s.mic_manager.stop.side_effect = stop_mic
    s._close_ws.side_effect = close
    stop = stop_method(users)

    async def scenario():
        await stop(s, reason='test')
        await stop(s, reason='again')

    asyncio.run(scenario())
    assert events == ['mic', 'websocket', 'save']
    assert not s.session_active.is_set()
    assert s._interaction_count_recorded


def test_cancelled_stop_still_closes_websocket():
    s = session()
    started = threading.Event()
    release = threading.Event()

    def stop_mic():
        started.set()
        release.wait(2)

    s.mic_manager.stop.side_effect = stop_mic
    stop = stop_method(Mock())

    async def scenario():
        task = asyncio.create_task(stop(s, reason='test'))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            s._close_ws.assert_awaited_once()
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'failure', [OSError('missing wav'), RuntimeError('speaker gone'), TimeoutError()]
)
def test_error_sound_failure_cannot_skip_session_cleanup(failure):
    play = AsyncMock(side_effect=failure)
    method = definitions(
        'core/session/error_handler.py',
        ['play_error_sound'],
        dict(play_standalone_error_sound=play, logger=Mock()),
        cls='ErrorHandler',
    ).play_error_sound
    s = SimpleNamespace(stop_session=AsyncMock())
    asyncio.run(method(SimpleNamespace(session=s), 'error', 'network lost'))
    s.stop_session.assert_awaited_once()


def test_error_sound_cancellation_still_cleans_up():
    play = AsyncMock(side_effect=asyncio.CancelledError())
    method = definitions(
        'core/session/error_handler.py',
        ['play_error_sound'],
        dict(play_standalone_error_sound=play, logger=Mock()),
        cls='ErrorHandler',
    ).play_error_sound
    s = SimpleNamespace(stop_session=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(method(SimpleNamespace(session=s)))
    s.stop_session.assert_awaited_once()


def make_mic_wrapper():
    provider = Mock()
    provider.start.side_effect = OSError('busy')
    ns = dict(
        asyncio=asyncio,
        contextlib=contextlib,
        os=os,
        time=time,
        deque=deque,
        MicManager=Mock(return_value=provider),
        MicUplink=Mock(),
        logger=Mock(),
        SILENCE_THRESHOLD=500,
    )
    cls = definitions(
        'core/session/mic_manager_wrapper.py', ['MicManagerWrapper'], ns
    ).MicManagerWrapper
    active = threading.Event()
    active.set()
    owner = SimpleNamespace(
        session_active=active, _set_listening_state=Mock(), stop_session=AsyncMock()
    )
    wrapper = cls(owner)
    wrapper._ensure_uplink = Mock()
    return wrapper, provider, ns


def test_stopping_cancels_pending_mic_retry():
    mic, device, ns = make_mic_wrapper()

    async def scenario():
        mic.start()
        assert mic._retry_task is not None
        await asyncio.sleep(0)
        mic.session.session_active.clear()
        mic.stop()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert mic._retry_task.cancelled()
        assert device.start.call_count == 1
        assert not mic.mic_running

    asyncio.run(scenario())


def test_session_stop_during_retry_backoff_does_not_reopen_mic():
    mic, device, ns = make_mic_wrapper()

    async def pause(_):
        mic.session.session_active.clear()

    ns['asyncio'] = SimpleNamespace(sleep=pause)
    asyncio.run(mic._retry_loop())
    device.start.assert_not_called()
    assert not mic.mic_running


def test_retry_that_finishes_after_stop_releases_the_stream():
    mic, device, ns = make_mic_wrapper()

    async def pause(_):
        pass

    ns['asyncio'] = SimpleNamespace(sleep=pause)
    device.start.side_effect = lambda _: mic.session.session_active.clear()
    asyncio.run(mic._retry_loop())
    device.stop.assert_called_once()
    assert not mic.mic_running
    mic.session._set_listening_state.assert_not_called()


def test_error_mood_write_failure_does_not_skip_motor_stop():
    mood = Mock()
    mood.apply_event.side_effect = OSError('disk full')
    motors = Mock()
    play = definitions(
        'core/session/error_handler.py',
        ['play_standalone_error_sound'],
        dict(
            asyncio=asyncio,
            os=SimpleNamespace(
                path=SimpleNamespace(join=os.path.join, exists=lambda _: False)
            ),
            mood_manager=mood,
            stop_all_motors=motors,
            set_status_led_state=Mock(),
            logger=Mock(),
        ),
    ).play_standalone_error_sound
    asyncio.run(play('error'))
    motors.assert_called_once()
