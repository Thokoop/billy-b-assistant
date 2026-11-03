"""Session lifecycle notifications for code that sits beside the assistant.

The assistant itself does not depend on anything here. Extension devices
(see core.devices) subscribe to learn when a conversation starts or ends, for
example to react when a session ends on its own because of a timeout or
because the assistant closed it.

Events carry keyword data:

    session_started   session_id, source
    session_ended     session_id, source, stopped_by

``session_id`` increases with every session, so a late ``session_ended`` of a
session that was superseded can be told apart from the current one.
``stopped_by`` is the source passed to ``request_session_stop()`` when the
session was stopped from outside, and ``None`` when it ended by itself.

Callbacks run synchronously on the thread that emits the event. They must
return quickly; an exception in a callback is logged and never reaches the
session code.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .logger import logger


SESSION_STARTED = "session_started"
SESSION_ENDED = "session_ended"

_listeners: dict[str, list[Callable[..., object]]] = {}
_lock = threading.Lock()


def subscribe(event: str, callback: Callable[..., object]) -> Callable[[], None]:
    """Call ``callback(**data)`` on every ``event``; returns an unsubscriber."""
    with _lock:
        _listeners.setdefault(event, []).append(callback)

    def unsubscribe():
        with _lock:
            callbacks = _listeners.get(event, [])
            if callback in callbacks:
                callbacks.remove(callback)

    return unsubscribe


def emit(event: str, **data) -> None:
    with _lock:
        callbacks = list(_listeners.get(event, ()))
    for callback in callbacks:
        try:
            callback(**data)
        except Exception as exc:
            logger.warning(f"Session event listener for {event} failed: {exc}")


def clear() -> None:
    """Remove every listener (shutdown and tests)."""
    with _lock:
        _listeners.clear()
