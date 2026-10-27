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

The route watcher claims the mode and records the route for button controls.
"""

import contextlib
import threading


IDLE = "idle"
STARTING = "starting"
RUNNING = "running"

_lock = threading.Lock()
_state = IDLE
_route = None
_generation = 0
_dismissed_media = None


class MediaDismissed(RuntimeError):
    """A physical double press returned control to the assistant."""


def dismiss_media(snapshot, address):
    """Suppress this phone's lingering media route without releasing hardware."""
    global _dismissed_media
    with _lock:
        if (_route, _generation) != snapshot[1:] and not (
            _state == IDLE and _generation == snapshot[2] + 1
        ):
            return False
        if _route and _route[1]:
            return False
        _dismissed_media = f"bluez_input.{address.replace(':', '_')}.".lower()
        return True


def media_allowed(route):
    with _lock:
        return _media_allowed(route)


def _media_allowed(route):
    return not (
        route
        and not route[1]
        and _dismissed_media
        and route[0].lower().startswith(_dismissed_media)
    )


def observe_route(route):
    """Re-arm automatic media takeover once the old transport has disappeared."""
    global _dismissed_media
    with _lock:
        if route is None:
            _dismissed_media = None
        return _media_allowed(route)


def state():
    """The current mode, one of IDLE / STARTING / RUNNING."""
    with _lock:
        return _state


def active():
    """True while a Bluetooth session is starting or running."""
    return state() != IDLE


def snapshot():
    """Atomically identify the current takeover, including its audio route."""
    with _lock:
        return _state, _route, _generation


@contextlib.contextmanager
def claim(route=None):
    """Own the audio path for one Bluetooth session.

    Yields a callable that promotes STARTING to RUNNING once audio is
    actually flowing; the distinction is for status reporting, since both
    already count as active(). The mode returns to IDLE however the session
    ends, including by exception - a Bluetooth session that died must not
    leave the AI path locked out for the rest of the process's life.

    Claiming twice is a bug rather than a race to tolerate: one watcher runs
    one session at a time, so a second claim means two watchers.
    """
    global _state, _route, _generation
    with _lock:
        if _state != IDLE:
            raise RuntimeError(f"Bluetooth audio session already {_state}")
        if not _media_allowed(route):
            raise MediaDismissed("Bluetooth media was dismissed by the button")
        _state = STARTING
        _route = route
        _generation += 1
    try:
        yield _promote
    finally:
        with _lock:
            _state = IDLE
            _route = None
            _generation += 1


def _promote():
    global _state
    with _lock:
        if _state == STARTING:
            _state = RUNNING
