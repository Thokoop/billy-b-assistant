"""Turns a Bluetooth transport appearing into a Bluetooth session.

This is what makes Bluetooth a mode of the main Billy process rather than a
separate service. There is no IPC to start a session with: webconfig runs in
its own process and cannot start a thread inside billy.service, so the route
itself is the trigger.

Why polling is fine. Measured on the Pi (PipeWire 1.2.7, 124 KiB of dump):
`pw-dump` costs ~14 ms of subprocess time and ~2 ms of JSON parse. Only the
parse holds the GIL, so at POLL_SECONDS the watcher occupies it for about one
part in a thousand - far below the level that starves the microphone callback
`ROUTE_CHECK_TICKS` exists to protect. A BlueZ D-Bus subscription would be
cheaper still and is not worth the dependency at these numbers.

What counts as a route. Measured on the same hardware: a phone that is merely
connected exposes no bluez node at all - nodes appear when a transport starts,
and both a music stream and a call report state "running". So idly walking in
with a paired phone cannot pre-empt a conversation. RUNNING_STATE is checked
anyway because a freshly configured A2DP transport exposes its node before the
AVDTP Open/Start exchange completes, and that half-open node is exactly the one
this must not hand the speaker to.
"""

import threading

from ..logger import logger
from . import mode, runtime
from .transport import discover, select_route


# Idle cost is one pw-dump per interval; see the module docstring.
POLL_SECONDS = 2.0
# A route that does not survive this was never a usable transport.
SETTLE_SECONDS = 0.3
# PipeWire's node state once the transport is actually carrying audio.
RUNNING_STATE = "running"
# A transport that keeps dropping is a broken link, not a session to restart
# forever. Shares the prototype's budget.
RESTART_BACKOFF_SECONDS = 0.5

_stop = threading.Event()
_thread = None
_session_stop = None
_session_lock = threading.Lock()


def start():
    """Begin watching for Bluetooth routes. Safe to call twice."""
    global _thread
    from ..config import BLUETOOTH_AUTO_SWITCH, TEXT_ONLY_MODE

    if not BLUETOOTH_AUTO_SWITCH or TEXT_ONLY_MODE:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="bluetooth-routes", daemon=True)
    _thread.start()
    logger.info("Bluetooth route watcher started.", "👀")


def stop():
    """Stop watching, and end a session in progress."""
    global _thread
    _stop.set()
    with _session_lock:
        if _session_stop is not None:
            _session_stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=3.0)
    _thread = None


def active_route():
    """The route to hand a session, or None.

    Fails closed exactly as the prototype does: select_route() raises on more
    than one phone rather than sending the microphone to the wrong one, and
    that is a reason to stay out of the way, not to crash the watcher.
    """
    try:
        nodes = [node for node in discover() if node.get("state") == RUNNING_STATE]
        return select_route(nodes)
    except Exception as exc:
        logger.verbose(f"Bluetooth route discovery skipped: {exc}", "ℹ️")
        return None


def _run():
    dismissed = None
    restarts = 0
    while not _stop.is_set():
        route = active_route()
        if route is None:
            # The phone stopped streaming or hung up: whatever went wrong with
            # the last session is history once the transport is gone.
            dismissed = None
            restarts = 0
            _stop.wait(POLL_SECONDS)
            continue
        if route == dismissed:
            _stop.wait(POLL_SECONDS)
            continue
        _stop.wait(SETTLE_SECONDS)
        if _stop.is_set() or active_route() != route:
            continue
        try:
            _run_session(route)
            restarts = 0
        except runtime.StreamRestart as exc:
            restarts += 1
            if restarts >= runtime.MAX_CONSECUTIVE_RESTARTS:
                logger.error(f"Bluetooth audio kept dropping, giving up: {exc}")
                dismissed = route
                restarts = 0
            _stop.wait(RESTART_BACKOFF_SECONDS)
        except Exception as exc:
            # Latch the route so a session that cannot run does not restart on
            # every poll for as long as the phone stays connected. Unplugging
            # or reconnecting clears it.
            logger.error(f"Bluetooth session failed: {exc}")
            dismissed = route


def _run_session(route):
    """Take the audio path, run one session, and give it back."""
    from .. import button
    from ..config import BLUETOOTH_CALL_AEC
    from .endpoints import SharedEndpoints

    global _session_stop

    source, sink = route
    with mode.claim() as running:
        session_stop = threading.Event()
        with _session_lock:
            _session_stop = session_stop
        try:
            button.stop_for_takeover("bluetooth")
            logger.info(
                f"Bluetooth {'call' if sink else 'media'} taking over audio.", "📞"
            )
            running()
            runtime.session(
                route,
                SharedEndpoints(
                    want_aec=bool(sink) and BLUETOOTH_CALL_AEC,
                    want_microphone=bool(sink),
                ),
                stop=session_stop,
            )
        finally:
            with _session_lock:
                _session_stop = None
            # Inside the claim, so the wake-word listener this re-arms cannot
            # open the microphone until mode() has gone back to idle.
            button.resume_after_takeover()
            logger.info("Bluetooth audio finished.", "🔇")
