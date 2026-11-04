"""Rings Billy while a paired phone has an incoming call.

The route watcher reports the phone's call state (see watcher.py); a call that
rings is not taken over until it is answered, so without this Billy would show
nothing at all while the phone rings. A press of Billy's button answers it
(controls.handle_button).

BLUETOOTH_RINGTONE chooses what plays: a random tone from Billy's ringtone
list (ringtones.py), the phone's own ringtone (the watcher takes the call
audio over while it rings), or nothing but the blinking status LED. Only
Billy the fish rings: other devices handle calls their own way.
"""

from __future__ import annotations

import threading
import time
import wave

from ..logger import logger


# Silence between two rings, like a real phone.
RING_PAUSE_SECONDS = 2.0
# A ringing state older than this is not trusted for a button press: the
# watcher refreshes it about every second while a phone is connected.
STATE_MAX_AGE_SECONDS = 3.0

_lock = threading.Lock()
_call_ringing = False
_updated = 0.0
_stop: threading.Event | None = None
_thread: threading.Thread | None = None


def call_ringing() -> bool:
    """True while a paired phone's call rings (recently confirmed)."""
    with _lock:
        return _call_ringing and time.monotonic() - _updated <= STATE_MAX_AGE_SECONDS


def update(ringing: bool) -> None:
    """The watcher's latest view of the paired phone: does a call ring?"""
    global _call_ringing, _updated
    with _lock:
        was_ringing = _call_ringing
        _call_ringing = bool(ringing)
        _updated = time.monotonic()
    if ringing and not was_ringing:
        logger.info("Bluetooth: incoming call. Press the button to answer.", "📲")
        _start()
    elif not ringing and was_ringing:
        stop()


def stop() -> None:
    """Stop ringing (answered, rejected, missed or taken over)."""
    global _call_ringing, _stop, _thread
    with _lock:
        _call_ringing = False
        stop_event, thread = _stop, _thread
        _stop = _thread = None
    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=2.0)


def ringtone_mode() -> str | None:
    """ "billy", "phone" or "off" (BLUETOOTH_RINGTONE); None on other devices.

    Only Billy the fish rings: other devices handle calls their own way.
    """
    from .. import config

    if getattr(config, "BILLY_DEVICE", "fish") != "fish":
        return None
    mode = str(getattr(config, "BLUETOOTH_RINGTONE", "billy") or "billy").lower()
    return mode if mode in {"billy", "phone", "off"} else "billy"


def _should_sound() -> bool:
    """Billy plays his own ringtones (and blinks) for "billy" and "off".

    With "phone" the phone's ringtone plays through the call audio, which the
    watcher takes over while the call rings.
    """
    return ringtone_mode() in {"billy", "off"}


def _start() -> None:
    global _stop, _thread
    if not _should_sound():
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(
            target=_ring, args=(_stop,), name="bluetooth-ringer", daemon=True
        )
        _thread.start()


def _busy() -> bool:
    """Something else owns the speaker: a conversation or Bluetooth audio."""
    from .. import button
    from . import mode

    return bool(button.is_active) or mode.active()


def _duration(path) -> float:
    """Length of a WAV in seconds (30 when it cannot be read)."""
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or 1)
    except Exception:
        return 30.0


def _ring(stop_event: threading.Event) -> None:
    from .. import audio
    from ..config import CHUNK_MS
    from ..mqtt import mqtt_publish
    from . import ringtones

    # "off": blink the status LED only.
    path = ringtones.pick() if ringtone_mode() == "billy" else None
    if path is None and ringtone_mode() == "billy":
        logger.warning("No ringtone found to play.")
    announced = False
    playing = False
    try:
        while not stop_event.is_set():
            if _busy():
                # A conversation started (wake word) or the call was answered:
                # never ring into audio that belongs to someone else.
                playing = False
                return
            if not announced:
                # Publishing billy/state also sets the status LED.
                mqtt_publish("billy/state", "ringing")
                announced = True
            if path is None:
                stop_event.wait(RING_PAUSE_SECONDS)
                continue
            audio.ensure_playback_worker_started(CHUNK_MS)
            # Wait for this ring's own chunks, as the wake-up clip does:
            # playback_done_event is not reset by plain WAV playback, so
            # is_billy_speaking() can stay True between rings.
            already_pending = audio.playback_queue.unfinished_tasks
            audio.enqueue_wav_to_playback(str(path))
            playing = True
            deadline = time.monotonic() + _duration(path) + 5.0
            while (
                not stop_event.is_set()
                and audio.playback_queue.unfinished_tasks > already_pending
                and time.monotonic() < deadline
            ):
                stop_event.wait(0.05)
            playing = False
            # Then the pause between rings, like a real phone.
            stop_event.wait(RING_PAUSE_SECONDS)
    except Exception as exc:
        logger.warning(f"Could not play the ringtone: {exc}")
    finally:
        if playing and not _busy():
            audio.stop_playback()
        if announced and not _busy():
            mqtt_publish("billy/state", "idle")
