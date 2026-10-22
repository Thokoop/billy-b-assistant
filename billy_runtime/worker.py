"""Nonblocking progress notifications from the actual core control loop."""

import contextlib
import os
import time


_last = 0.0
_ready = False


def notify(message):
    fd = os.environ.get('BILLY_PROGRESS_FD')
    if fd:
        with contextlib.suppress(OSError, ValueError):
            os.write(int(fd), (message + '\n').encode())


def ready():
    global _ready
    _ready = True
    notify('ready')


def progress():
    global _last
    now = time.monotonic()
    if _ready and now - _last >= 2:
        _last = now
        notify('progress')


def monitored_session(method):
    """Watch real asyncio progress while a conversation is active."""
    import asyncio
    import functools
    import uuid

    @functools.wraps(method)
    async def wrapped(*args, **kwargs):
        identity = uuid.uuid4().hex
        notify('loop-start:' + identity)

        async def pulse():
            while True:
                await asyncio.sleep(2)
                notify('loop-progress:' + identity)

        task = asyncio.create_task(pulse())
        try:
            return await method(*args, **kwargs)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            notify('loop-end:' + identity)

    return wrapped
