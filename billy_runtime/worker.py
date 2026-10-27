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


def _process_identity(pid):
    """Include Linux process start time so a reused PID cannot revive old state."""
    import sys
    from pathlib import Path

    os.kill(pid, 0)
    if sys.platform == 'linux':
        # comm may contain spaces or parentheses; fields after its final ')' start at 3.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]  # starttime, field 22
    return str(pid)  # Local development on systems without /proc.


def set_activity(state):
    """Publish the core's state in RAM, including on older supervised installs.

    Release code owns this snapshot. The root supervisor only needs to provide
    its existing health report; it need not understand a new progress message.
    """
    import json

    from .paths import INSTALL_ROOT
    from .storage import atomic_write, runtime_directory

    if os.environ.get('BILLY_WORKER_MODE') != 'core':
        return  # Web-interface LED tests must not overwrite the real core state.
    try:
        pid = os.getpid()
        snapshot = {'state': state, 'pid': pid, 'process_start': _process_identity(pid)}
        atomic_write(
            runtime_directory(INSTALL_ROOT) / 'activity.json', json.dumps(snapshot)
        )
    except (OSError, ValueError, IndexError):
        pass  # Status reporting must never interrupt audio or startup.


def read_activity(root=None):
    """Read live activity without importing hardware code or requiring MQTT."""
    import json

    from .paths import INSTALL_ROOT
    from .storage import runtime_directory

    try:
        directory = runtime_directory(root or INSTALL_ROOT)
        report = json.loads((directory / 'core.json').read_text())
        if not report.get('healthy') or not 0 <= time.time() - report['seen'] < 8:
            return None
        activity = json.loads((directory / 'activity.json').read_text())
        pid = activity['pid']
        if not isinstance(pid, int) or pid <= 0:
            return None
        if activity['process_start'] != _process_identity(pid):
            return None
        state = activity.get('state')
        return state if isinstance(state, str) else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
