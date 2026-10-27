"""One bounded sender per AI capture session, with timing diagnostics.

Bluetooth has its own transport and diagnostics. DSP remains in the existing
capture callback until measurements on the Pi justify changing that boundary.
"""

import asyncio
import contextlib
import threading
import time
from collections import deque


class MicUplink:
    def __init__(
        self,
        loop,
        send,
        failed,
        report,
        *,
        capacity=100,
        max_age=1.0,
        send_timeout=1.0,
    ):
        self.loop = loop
        self.send = send
        self.failed = failed
        self.report = report
        self.capacity = capacity  # also accommodates the 3s startup prebuffer
        self.max_age = max_age
        self.send_timeout = send_timeout
        self._lock = threading.Lock()
        self._queue = deque()
        self._wake = asyncio.Event()
        self._notified = False
        self._error = None
        self.closed = False
        self._last_capture = None
        self._last_report = time.monotonic()
        self._metrics = dict(
            captured=0,
            sent=0,
            overflow=0,
            overflow_during_playback=0,
            overflow_without_playback=0,
            peak_queued=0,
            callback_max_ms=0.0,
            capture_gap_max_ms=0.0,
            queue_max_ms=0.0,
            send_max_ms=0.0,
        )
        self._task = loop.create_task(self._run(), name='billy-mic-uplink')

    def record_callback(self, started, overflow=False, *, playing=False):
        with self._lock:
            stats = self._metrics
            stats['captured'] += 1
            stats['overflow'] += bool(overflow)
            if overflow:
                key = (
                    'overflow_during_playback'
                    if playing
                    else 'overflow_without_playback'
                )
                stats[key] += 1
            stats['callback_max_ms'] = max(
                stats['callback_max_ms'], (time.monotonic() - started) * 1000
            )
            if self._last_capture is not None:
                stats['capture_gap_max_ms'] = max(
                    stats['capture_gap_max_ms'], (started - self._last_capture) * 1000
                )
            self._last_capture = started

    def submit(self, samples):
        """Copy callback-owned PCM; schedule at most one outstanding wake-up."""
        with self._lock:
            if self.closed or self._error is not None:
                return False
            if len(self._queue) >= self.capacity:
                self._error = RuntimeError('Microphone send queue is full')
            else:
                self._queue.append((time.monotonic(), samples.copy()))
                self._metrics['peak_queued'] = max(
                    self._metrics['peak_queued'], len(self._queue)
                )
            accepted = self._error is None
            if not self._notified:
                self._notified = True
                try:
                    self.loop.call_soon_threadsafe(self._wake.set)
                except RuntimeError:
                    self.closed = True
                    self._queue.clear()
                    return False
            return accepted

    def discard_pending(self):
        with self._lock:
            self._queue.clear()

    def snapshot(self):
        with self._lock:
            return {**self._metrics, 'queued': len(self._queue)}

    def close(self):
        with self._lock:
            self.closed = True
            self._queue.clear()
        task = self._task
        if task is not None and not task.done():
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(task.cancel)

    async def _run(self):
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                while True:
                    with self._lock:
                        if self.closed:
                            return
                        if self._error is not None:
                            raise self._error
                        if not self._queue:
                            self._wake.clear()
                            self._notified = False
                            break
                        captured, samples = self._queue.popleft()
                    started = time.monotonic()
                    age = started - captured
                    with self._lock:
                        self._metrics['queue_max_ms'] = max(
                            self._metrics['queue_max_ms'], age * 1000
                        )
                    if age > self.max_age:
                        raise TimeoutError('Microphone audio is too old to send')
                    async with asyncio.timeout(self.send_timeout):
                        await self.send(samples)
                    with self._lock:
                        self._metrics['sent'] += 1
                        self._metrics['send_max_ms'] = max(
                            self._metrics['send_max_ms'],
                            (time.monotonic() - started) * 1000,
                        )
                    # Let controls/cancellation run even when ws.send completes
                    # immediately while draining the startup prebuffer.
                    await asyncio.sleep(0)
                if time.monotonic() - self._last_report >= 5:
                    self.report(self.snapshot())
                    self._last_report = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._lock:
                self.closed = True
                self._queue.clear()
            # Cleanup may call close() from another thread. Do not let it cancel
            # the very task now closing the microphone and websocket.
            self._task = None
            self.report(self.snapshot())
            await self.failed(exc)
