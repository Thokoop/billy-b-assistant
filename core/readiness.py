"""A file Billy touches once he is actually up.

billy.service is Type=simple, so systemd calls the unit "active" the moment
the process is exec'd - before Python has finished importing, let alone before
the microphone, motors and status LED exist. Anything waiting for Billy to come
back (the web interface after a restart) therefore sees "active" seconds too
early, which is why the page could reload while the status LED was still dark.

The marker is removed on shutdown and rewritten when the main loop is about to
start, so its modification time answers "since when has Billy been ready".
"""

import contextlib
import os
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
READY_MARKER_PATH = PROJECT_ROOT / "state" / "ready"


def mark_ready() -> None:
    """Record that Billy has finished starting up."""
    # Readiness reporting must never be the reason Billy fails to start, or
    # fails to shut down cleanly.
    with contextlib.suppress(OSError):
        READY_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        READY_MARKER_PATH.write_text(f"{time.time():.3f}\n")


def clear_ready() -> None:
    """Record that Billy is no longer running."""
    with contextlib.suppress(OSError):
        os.unlink(READY_MARKER_PATH)


def ready_at() -> float | None:
    """When Billy last reported himself ready, or None if he has not.

    A marker left behind by a process that was killed outright still carries
    its old timestamp, so callers comparing it against a moment of their own
    are not fooled by it.
    """
    try:
        return READY_MARKER_PATH.stat().st_mtime
    except OSError:
        return None
