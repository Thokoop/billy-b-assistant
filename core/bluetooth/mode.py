"""Whether a Bluetooth audio session owns Billy's audio right now.

A Bluetooth session takes the speaker, the microphone and the motors away
from the AI path, so every entry point that could start a session, open the
microphone or push audio at the speaker has to be able to ask. That is why
the flag lives here and not in core.button: this module imports nothing from
the rest of Billy, which is what lets core.button, core.mqtt and core.say all
consult it without an import cycle.

`active()` is deliberately true from the moment a takeover begins, not from
the moment audio flows. The window it exists to cover is the handover itself
- stopping the AI session, then waiting for ALSA to release the capture
handle - because that is precisely when a wake-word resume or a queued MQTT
session would collide on the microphone (-9985).

Nothing claims the mode yet. The route watcher (phase 4c) is what will.
"""

import contextlib
import threading


IDLE = "idle"
STARTING = "starting"
RUNNING = "running"

_lock = threading.Lock()
_state = IDLE


def state():
    """The current mode, one of IDLE / STARTING / RUNNING."""
    with _lock:
        return _state


def active():
    """True while a Bluetooth session is starting or running."""
    return state() != IDLE


@contextlib.contextmanager
def claim():
    """Own the audio path for one Bluetooth session.

    Yields a callable that promotes STARTING to RUNNING once audio is
    actually flowing; the distinction is for status reporting, since both
    already count as active(). The mode returns to IDLE however the session
    ends, including by exception - a Bluetooth session that died must not
    leave the AI path locked out for the rest of the process's life.

    Claiming twice is a bug rather than a race to tolerate: one watcher runs
    one session at a time, so a second claim means two watchers.
    """
    global _state
    with _lock:
        if _state != IDLE:
            raise RuntimeError(f"Bluetooth audio session already {_state}")
        _state = STARTING
    try:
        yield _promote
    finally:
        with _lock:
            _state = IDLE


def _promote():
    global _state
    with _lock:
        if _state == STARTING:
            _state = RUNNING
