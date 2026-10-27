"""Bounded backlog, PCM parity, timing, cancellation and repeated restarts."""

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest


def test_one_sender_preserves_pcm_and_order(audio_module, isolated):
    module = isolated.load('mic_uplink')
    a = audio_module
    a.MIC_RATE = 48000
    blocks = [np.arange(480, dtype=np.int16) + i for i in range(50)]
    expected = [a.mic_audio_payload(block) for block in blocks]
    sent = []

    async def run():
        async def send(samples):
            sent.append(a.mic_audio_payload(samples))

        failed = AsyncMock()
        sender = module.MicUplink(asyncio.get_running_loop(), send, failed, Mock())
        for block in blocks:
            assert sender.submit(block)
        for block in blocks:
            block.fill(0)  # callback buffer reuse must be harmless
        while sender.snapshot()['sent'] < 50:
            await asyncio.sleep(0)
        assert sender.snapshot()['peak_queued'] == 50
        sender.close()
        await asyncio.gather(sender._task, return_exceptions=True)
        failed.assert_not_awaited()

    asyncio.run(run())
    assert sent == expected


@pytest.mark.parametrize('failure', ['full', 'age', 'send_timeout', 'send_error'])
def test_backlog_fails_once_instead_of_growing_or_sending_stale_audio(
    isolated, failure
):
    module = isolated.load('mic_uplink')

    async def run():
        failed = AsyncMock()
        sent = []

        async def send(samples):
            sent.append(samples.copy())
            if failure == 'send_timeout':
                await asyncio.Event().wait()
            if failure == 'send_error':
                raise OSError('connection gone')

        sender = module.MicUplink(
            asyncio.get_running_loop(),
            send,
            failed,
            Mock(),
            capacity=3,
            max_age=0.001 if failure == 'age' else 1,
            send_timeout=0.01,
        )
        for _ in range(10 if failure == 'full' else 1):
            sender.submit(np.ones(960, dtype=np.int16))
        assert sender.snapshot()['queued'] <= 3
        if failure == 'age':
            time.sleep(0.005)  # simulate a stalled event loop
        task = sender._task
        await asyncio.wait_for(task, 0.2)
        failed.assert_awaited_once()
        assert sender.closed and sender.snapshot()['queued'] == 0
        assert not sender.submit(np.ones(960, dtype=np.int16))
        if failure in ('full', 'age'):
            assert not sent

    asyncio.run(run())


def test_stop_cancels_inflight_and_discards_pending(isolated):
    module = isolated.load('mic_uplink')

    async def run():
        started = asyncio.Event()
        finished = []
        failed = AsyncMock()

        async def send(samples):
            started.set()
            await asyncio.Event().wait()
            finished.append(True)

        sender = module.MicUplink(asyncio.get_running_loop(), send, failed, Mock())
        for _ in range(20):
            sender.submit(np.ones(960, dtype=np.int16))
        await started.wait()
        sender.close()
        await asyncio.gather(sender._task, return_exceptions=True)
        assert sender.snapshot()['queued'] == 0 and not finished
        failed.assert_not_awaited()

    asyncio.run(run())


def test_blocked_event_loop_has_one_wakeup_and_bounded_memory(isolated):
    module = isolated.load('mic_uplink')

    async def run():
        loop = asyncio.get_running_loop()
        real = loop.call_soon_threadsafe
        calls = []

        def notify(*args):
            calls.append(args)
            return real(*args)

        loop.call_soon_threadsafe = notify
        sender = module.MicUplink(loop, AsyncMock(), AsyncMock(), Mock(), capacity=100)
        block = np.zeros(960, dtype=np.int16)

        def capture():
            for _ in range(2000):
                sender.submit(block)

        thread = threading.Thread(target=capture)
        thread.start()
        thread.join()
        assert sender.snapshot()['queued'] == 100 and len(calls) == 1
        loop.call_soon_threadsafe = real
        await sender._task
        sender.failed.assert_awaited_once()

    asyncio.run(run())


def test_repeated_sessions_leave_no_senders_and_report_timing(isolated):
    module = isolated.load('mic_uplink')

    async def run():
        for _ in range(30):
            sender = module.MicUplink(
                asyncio.get_running_loop(), AsyncMock(), AsyncMock(), Mock()
            )
            sender.record_callback(time.monotonic() - 0.002, overflow=True)
            sender.record_callback(time.monotonic(), overflow=False)
            assert sender.snapshot()['callback_max_ms'] >= 2
            assert sender.snapshot()['overflow'] == 1
            sender.submit(np.zeros(960, dtype=np.int16))
            sender.close()
            await asyncio.gather(sender._task, return_exceptions=True)
        assert not [
            t for t in asyncio.all_tasks() if t.get_name() == 'billy-mic-uplink'
        ]

    asyncio.run(run())


@pytest.mark.parametrize('prebuffer', [False, True])
def test_real_mic_wrapper_keeps_first_words_and_resets_pending_audio(
    audio_module, isolated, prebuffer
):
    import contextlib
    import os
    from collections import deque
    from types import SimpleNamespace

    from conftest import definitions

    a = audio_module
    a.MIC_RATE = 24000
    a.playback_done_event.set()
    a.echo_cancellation_active = Mock(return_value=False)
    module = isolated.load('mic_uplink')
    cls = definitions(
        'application/core/session/mic_manager_wrapper.py',
        ['MicManagerWrapper'],
        dict(
            asyncio=asyncio,
            contextlib=contextlib,
            os=os,
            time=time,
            deque=deque,
            MicManager=Mock(),
            MicUplink=module.MicUplink,
            logger=Mock(),
            SILENCE_THRESHOLD=500,
            CHUNK_MS=40,
            math=__import__('math'),
            TEXT_ONLY_MODE=False,
            audio=a,
            calculate_input_rms=a.calculate_input_rms,
            mic_input_samples_for_meter=a.mic_input_samples_for_meter,
        ),
    ).MicManagerWrapper

    async def run():
        active = asyncio.Event()
        active.set()
        ws = SimpleNamespace(send=AsyncMock())
        s = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            session_active=active,
            session_initialized=not prebuffer,
            ws=ws,
            _set_listening_state=Mock(),
            _server_barge_in_enabled=False,
            state=Mock(
                allow_mic_input=True, assistant_speaking=False, response_active=False
            ),
            stop_session=AsyncMock(),
        )
        mic = cls(s)
        mic.timeout_checker = AsyncMock()
        mic.start()
        block = np.full((960, 1), 800, dtype=np.int16)
        mic.callback(block, 960, None, SimpleNamespace(input_overflow=False))
        if prebuffer:
            ws.send.assert_not_awaited()
            assert len(mic._prebuffered_audio) == 1
            s.session_initialized = True
            mic.flush_prebuffer()
        for _ in range(100):
            if ws.send.await_count:
                break
            await asyncio.sleep(0)
        assert ws.send.await_count == 1
        assert json.loads(ws.send.call_args.args[0]) == json.loads(
            a.mic_audio_payload(block[:, 0])
        )
        old = mic._uplink
        mic.callback(block, 960, None, SimpleNamespace(input_overflow=True))
        await mic.reset_uplink()
        assert old.closed and old._task.done()
        assert old.snapshot()['queued'] == 0
        assert old.snapshot()['overflow'] == 1
        assert old.snapshot()['overflow_without_playback'] == 1
        assert ws.send.await_count == 1  # the queued old block was discarded
        mic.callback(block, 960, None, SimpleNamespace(input_overflow=False))
        for _ in range(100):
            if ws.send.await_count == 2:
                break
            await asyncio.sleep(0)
        assert ws.send.await_count == 2
        a.playback_done_event.clear()
        mic.callback(block, 960, None, SimpleNamespace(input_overflow=True))
        assert mic._uplink.snapshot()['overflow_during_playback'] == 1
        assert mic._uplink.snapshot()['overflow_without_playback'] == 0
        a.playback_done_event.set()
        task = mic._uplink._task
        active.clear()
        mic.stop()
        await asyncio.gather(task, return_exceptions=True)
        s.stop_session.assert_not_awaited()

    asyncio.run(run())


def test_overflow_diagnostics_distinguish_playback_without_changing_capture(isolated):
    module = isolated.load('mic_uplink')

    async def run():
        sender = module.MicUplink(
            asyncio.get_running_loop(), AsyncMock(), AsyncMock(), Mock()
        )
        for overflow, playing in [
            (True, True),
            (False, True),
            (True, False),
            (True, True),
        ]:
            sender.record_callback(time.monotonic(), overflow, playing=playing)
        metrics = sender.snapshot()
        assert metrics['captured'] == 4
        assert metrics['overflow'] == 3
        assert metrics['overflow_during_playback'] == 2
        assert metrics['overflow_without_playback'] == 1
        sender.close()
        await asyncio.gather(sender._task, return_exceptions=True)

    asyncio.run(run())
