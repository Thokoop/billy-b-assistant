"""Failures must finish accounting, wake waiters and allow the next session."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


@pytest.fixture
def playback(isolated):
    return isolated.load('playback_queue')


def test_discard_keeps_the_inflight_item_accounted(playback):
    queue = playback.PlaybackQueue()
    queue.put('writing')
    queue.get()
    queue.put('pending')
    queue.discard_pending()
    assert queue.empty() and queue.unfinished_tasks == 1
    queue.task_done()
    assert queue.unfinished_tasks == 0


def test_failure_releases_joiners_rejects_late_producers_and_can_recover(playback):
    queue = playback.PlaybackQueue()
    queue.put('pending')
    joined = threading.Event()
    thread = threading.Thread(target=lambda: (queue.join(), joined.set()), daemon=True)
    thread.start()
    queue.fail(OSError('speaker disconnected'))
    assert joined.wait(1)
    thread.join(1)
    with pytest.raises(playback.PlaybackError, match='disconnected'):
        queue.put('late')
    with pytest.raises(playback.PlaybackError):
        asyncio.run(queue.wait())
    queue.recover()
    queue.put('next')
    assert queue.get() == 'next'
    queue.task_done()
    asyncio.run(queue.wait())


def test_waiter_does_not_mistake_fast_recovery_for_success(playback):
    async def scenario():
        queue = playback.PlaybackQueue()
        queue.put('pending')
        waiter = asyncio.create_task(queue.wait())
        await asyncio.sleep(0)
        queue.fail(OSError('failed'))
        queue.recover()
        with pytest.raises(playback.PlaybackError):
            await waiter

    asyncio.run(scenario())


def test_wait_is_cancellable_and_bounded_without_executor_threads(
    playback, monkeypatch
):
    monkeypatch.setattr(
        asyncio, 'to_thread', Mock(side_effect=AssertionError('No executor waits'))
    )

    async def scenario():
        queue = playback.PlaybackQueue()
        queue.put('pending')
        with pytest.raises(asyncio.TimeoutError):
            await queue.wait(timeout=0.02)
        waiter = asyncio.create_task(queue.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        queue.discard_pending()

    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['raw', 'tts', 'bluetooth', 'song'])
def test_output_write_failure_finishes_current_and_pending_items(audio_module, mode):
    a = audio_module
    pcm = np.zeros(960, dtype=np.int16).tobytes()
    item = {
        'raw': pcm,
        'tts': ('tts', pcm, 1),
        'bluetooth': ('bluetooth', pcm, Mock()),
        'song': ('song', pcm, pcm, 0),
    }[mode]
    a.playback_queue.put(item)
    a.playback_queue.put(item)
    a.test_stream.write.side_effect = OSError('disconnected speaker')
    a.playback_worker(40)
    a.test_stream.write.assert_called_once()
    assert a.playback_queue.empty() and a.playback_queue.unfinished_tasks == 0
    assert a.playback_done_event.is_set()
    a.movements.stop_all_motors.assert_called_once()
    with pytest.raises(a.PlaybackError, match='disconnected'):
        asyncio.run(a.playback_queue.wait())


def test_output_open_failure_discards_waiting_audio(audio_module):
    a = audio_module
    a.playback_queue.put(b'pending')
    a.sd.OutputStream.side_effect = OSError('device absent')
    a.playback_worker(40)
    assert a.playback_queue.unfinished_tasks == 0
    with pytest.raises(a.PlaybackError, match='device absent'):
        asyncio.run(a.playback_queue.wait())


def test_song_reset_and_stop_do_not_corrupt_accounting(audio_module):
    a = audio_module
    a.playback_queue.put('in flight')
    a.playback_queue.get()
    a.playback_queue.put('old response')
    a.reset_for_new_song()
    assert a.playback_queue.unfinished_tasks == 1
    a.playback_queue.put('song chunk')
    a.stop_playback()
    assert a.playback_queue.unfinished_tasks == 1
    a.playback_queue.task_done()
    asyncio.run(a.playback_queue.wait())


def test_failure_wakes_an_idle_output_worker(audio_module):
    a = audio_module
    a.ensure_playback_worker_started(40)
    a.playback_queue.fail(TimeoutError('output stalled'))
    a._playback_thread.join(1)
    assert not a._playback_thread.is_alive()
    assert a.playback_queue.unfinished_tasks == 0


def test_replacement_worker_can_play_next_session(audio_module):
    a = audio_module
    a.sd.OutputStream.side_effect = OSError('unplugged')
    a.ensure_playback_worker_started(40)
    a._playback_thread.join(1)
    assert not a._playback_thread.is_alive()
    a.sd.OutputStream.side_effect = None
    a.ensure_playback_worker_started(40)
    a.playback_queue.put(np.zeros(960, dtype=np.int16).tobytes())
    a.playback_queue.put(None)
    a._playback_thread.join(1)
    assert not a._playback_thread.is_alive()
    a.test_stream.write.assert_called_once()
    asyncio.run(a.playback_queue.wait())


def test_concurrent_starts_create_one_worker(audio_module, monkeypatch):
    a = audio_module
    fake = Mock()
    fake.is_alive.return_value = True
    factory = Mock(return_value=fake)
    monkeypatch.setattr(a, 'threading', SimpleNamespace(Thread=factory))
    threads = [
        threading.Thread(target=a.ensure_playback_worker_started, args=(40,))
        for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
    factory.assert_called_once()
    fake.start.assert_called_once()


def test_healthy_bluetooth_uses_own_aec_and_keeps_48k_pcm(audio_module):
    a = audio_module
    aec = Mock()
    mono = np.full(480, 2000, dtype=np.int16)
    a.playback_queue.put(('bluetooth', mono.tobytes(), aec))
    a.playback_queue.put(None)
    a.playback_worker(40)
    written = a.test_stream.write.call_args.args[0]
    assert written.shape == (480, 2)
    np.testing.assert_array_equal(written[:, 0], mono // 2)
    np.testing.assert_array_equal(written[:, 1], written[:, 0])
    reference, rate = aec.feed_render.call_args.args
    assert rate == 48000
    np.testing.assert_array_equal(reference, written[:, 0])
    a.echo_canceller.feed_render.assert_not_called()
    assert a.PROVIDER_MIC_RATE == a.PROVIDER_OUTPUT_RATE == 24000
    asyncio.run(a.playback_queue.wait())


def test_healthy_long_playback_extends_stall_deadline(playback):
    async def scenario():
        queue = playback.PlaybackQueue()
        queue.put('long response')
        waiter = asyncio.create_task(queue.wait(timeout=0.1))
        for _ in range(20):
            await asyncio.sleep(0.015)
            queue.mark_progress()
        assert not waiter.done()  # > total timeout, but output kept advancing.
        queue.get()
        queue.task_done()
        await waiter

    asyncio.run(scenario())


def test_more_input_cannot_hide_stalled_output(playback):
    async def scenario():
        queue = playback.PlaybackQueue()
        queue.put('first')
        waiter = asyncio.create_task(queue.wait(timeout=0.03))
        for _ in range(5):
            await asyncio.sleep(0.01)
            queue.put('more')
        with pytest.raises(TimeoutError):
            await waiter
        queue.discard_pending()

    asyncio.run(scenario())


def test_failure_is_visible_before_finishing_the_last_item(audio_module, monkeypatch):
    a = audio_module
    a.playback_queue.put(b'\x00\x00' * 960)
    a.test_stream.write.side_effect = OSError('disconnected')
    original = a.playback_queue.task_done
    rejected = []

    def finish():
        try:
            a.playback_queue.put('late producer')
        except a.PlaybackError:
            rejected.append(True)
        original()

    monkeypatch.setattr(a.playback_queue, 'task_done', finish)
    a.playback_worker(40)
    assert rejected == [True]
    assert a.playback_queue.unfinished_tasks == 0


@pytest.mark.parametrize('mode', ['raw', 'tts'])
def test_process_shutdown_stops_inflight_audio_and_closes_stream_before_return(
    audio_module, mode
):
    a = audio_module
    entered, release = threading.Event(), threading.Event()

    def write(_):
        entered.set()
        assert release.wait(2)

    a.test_stream.write.side_effect = write
    pcm = np.zeros(24000, dtype=np.int16).tobytes()  # Many output chunks.
    item = pcm if mode == 'raw' else ('tts', pcm, None)
    a.ensure_playback_worker_started(40)
    a.playback_queue.put(item)
    a.playback_queue.put(item)
    assert entered.wait(1)
    stopped = threading.Event()
    shutdown = threading.Thread(
        target=lambda: (a.shutdown_playback_worker(), stopped.set())
    )
    shutdown.start()
    assert a._playback_shutdown.wait(1)
    assert not stopped.is_set()  # Must wait for the current native write.
    release.set()
    shutdown.join(2)
    assert stopped.is_set() and not a._playback_thread.is_alive()
    a.test_stream.write.assert_called_once()
    a.sd.OutputStream.return_value.__exit__.assert_called_once()
    assert a.playback_queue.unfinished_tasks == 0
    with pytest.raises(a.PlaybackError, match='shutting down'):
        a.playback_queue.put(item)
    a.ensure_playback_worker_started(40)
    assert not a._playback_thread.is_alive()  # No late producer may reopen output.


def test_idle_or_failed_output_shutdown_is_idempotent(audio_module):
    a = audio_module
    a.ensure_playback_worker_started(40)
    assert a.shutdown_playback_worker()
    assert a.shutdown_playback_worker()
    assert a.playback_queue.unfinished_tasks == 0
    a.sd.OutputStream.return_value.__exit__.assert_called_once()
    with pytest.raises(a.PlaybackError, match='shutting down'):
        a.playback_queue.recover()


def test_failed_output_can_still_be_shut_down(audio_module):
    a = audio_module
    a.sd.OutputStream.side_effect = OSError('absent')
    a.ensure_playback_worker_started(40)
    a._playback_thread.join(1)
    assert a.shutdown_playback_worker()
    assert a.playback_queue.unfinished_tasks == 0
