"""Playback accounting and cancellable waits, independent of audio hardware."""

import asyncio
from queue import Empty, Queue
from time import monotonic


class PlaybackError(RuntimeError):
    """The output worker failed; the pending audio was discarded."""


class PlaybackQueue(Queue):
    """Unbounded PCM handoff with explicit failure and recovery.

    Only queued items can be discarded. An item already taken by a worker
    remains its responsibility, including when its output write raises.
    """

    def __init__(self):
        super().__init__()
        self._failure = None
        self._closed = False
        self._failure_epoch = 0
        self._progress = 0

    def put(self, item, block=True, timeout=None):
        # This queue is unbounded, as the previous Queue() was. Check failure
        # and enqueue under the same mutex so a failed worker cannot leave a
        # producer's late block stranded behind the failure flush.
        with self.not_full:
            if self._closed:
                raise PlaybackError('Playback is shutting down')
            if self._failure is not None:
                raise PlaybackError(self._failure)
            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def get(self, block=True, timeout=None):
        with self.not_empty:
            if timeout is not None and timeout < 0:
                raise ValueError("timeout must be non-negative")
            deadline = None if timeout is None else monotonic() + timeout
            while not self._qsize():
                if self._closed:
                    # Account for the stop item like a normal queued sentinel.
                    self.unfinished_tasks += 1
                    return None
                if self._failure is not None:
                    raise PlaybackError(self._failure)
                if not block:
                    raise Empty
                remaining = None if deadline is None else deadline - monotonic()
                if remaining is not None and remaining <= 0:
                    raise Empty
                self.not_empty.wait(remaining)
            item = self._get()
            self.not_full.notify()
            return item

    def mark_progress(self):
        """A successful output write extends the stall deadline."""
        with self.mutex:
            self._progress += 1

    def task_done(self):
        super().task_done()
        self.mark_progress()

    def _discard_locked(self):
        while self._qsize():
            self._get()
            self.unfinished_tasks -= 1
        if self.unfinished_tasks == 0:
            self.all_tasks_done.notify_all()

    def discard_pending(self):
        """Discard queued work without pretending an in-flight write finished."""
        with self.mutex:
            self._discard_locked()

    def fail(self, error):
        with self.mutex:
            self._failure = str(error) or type(error).__name__
            self._failure_epoch += 1
            self._discard_locked()
            self.not_empty.notify_all()

    def close(self):
        """Permanently retire output on process shutdown; wake an idle worker."""
        with self.mutex:
            self._closed = True
            self._discard_locked()
            self.not_empty.notify_all()

    def recover(self):
        """Called only when starting a replacement output worker."""
        with self.mutex:
            if self._closed:
                raise PlaybackError('Playback is shutting down')
            self._failure = None

    async def wait(self, timeout=25.0, *, cancelled=None):
        """Wait until drained, timing out only if output makes no progress.

        A long healthy answer can take longer than timeout to play. Appending
        more input is not progress; only completed work/output writes count.
        Cancellation leaves no executor thread blocked on Queue.join().
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        with self.mutex:
            epoch = self._failure_epoch
            progress = self._progress
        while True:
            # A previous AI session must not keep waiting on audio now owned
            # by Bluetooth. Do not flush that new owner's queue on cancellation.
            if cancelled is not None and cancelled():
                return False
            with self.mutex:
                if self._failure is not None or epoch != self._failure_epoch:
                    raise PlaybackError(self._failure or "Playback worker failed")
                if self.unfinished_tasks == 0:
                    return True
                if progress != self._progress:
                    progress = self._progress
                    deadline = loop.time() + timeout
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Playback did not finish in time")
            await asyncio.sleep(min(0.01, remaining))
