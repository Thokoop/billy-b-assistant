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

What counts as a route, and why the cadence is what it is. A phone that is
merely connected exposes no bluez node at all, so presence alone cannot
pre-empt a conversation. When the phone does open a transport the node appears
in state "suspended" - not "running" - and WirePlumber releases an idle
Bluetooth source after about five seconds, which closes the transport and makes
the phone revert to its own speaker. Measured on hardware: the node appeared for
5 and 6 seconds across two attempts.

So the node reads "running" only once something is already consuming it, which
makes state useless as a trigger: it is a consequence of attaching, not a
signal to attach. An earlier version keyed on it after measuring with the
prototype already attached, and never fired at all.

That five second window is also why POLL_SECONDS is what the prototype used.
Detection has to be quick enough to attach pw-cat before WirePlumber gives the
transport back.
"""

import threading
import time

from ..logger import logger
from . import mode, ringer, runtime
from .transport import discover, select_route


# The prototype's cadence, and for the same reason: a transport nothing has
# attached to is released after about five seconds, so a slower poll watches the
# node appear and vanish without ever taking it. One pw-dump costs ~14 ms of
# subprocess time and ~2 ms of JSON parse on the Pi, and only the parse holds the
# GIL, so this is well under one percent of the interpreter.
POLL_SECONDS = 0.5
# A route that does not survive this was never a usable transport. It also
# covers the half-open case, where a freshly configured A2DP transport exposes
# its node before the AVDTP Open/Start exchange completes.
SETTLE_SECONDS = 0.3
# A transport that keeps dropping is a broken link, not a session to restart
# forever. Shares the prototype's budget.
RESTART_BACKOFF_SECONDS = 0.5
# How often the paired phone is asked whether a call rings. A ringing call is
# not taken over until it is answered (see _run), so this is what lets Billy
# ring and a button press answer it.
RING_CHECK_SECONDS = 1.0

_stop = threading.Event()
_thread = None
_connect_thread = None
_session_stop = None
_session_lock = threading.Lock()
_next_ring_check = 0.0


def start():
    """Begin watching for Bluetooth routes. Safe to call twice."""
    global _thread, _connect_thread
    from ..config import BLUETOOTH_AUTO_SWITCH, TEXT_ONLY_MODE
    from . import autoconnect

    if not BLUETOOTH_AUTO_SWITCH or TEXT_ONLY_MODE:
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    # Settings polls this file to say what the audio path is doing. Writing it
    # at startup also clears a state left behind by a process that went away
    # mid-session - a crash, or a restart during a call.
    runtime.status(mode.IDLE)
    _thread = threading.Thread(target=_run, name="bluetooth-routes", daemon=True)
    _thread.start()
    _connect_thread = autoconnect.start(_stop)
    logger.info("Bluetooth route watcher started.", "👀")


def stop():
    """Stop watching, and end a session in progress."""
    global _thread, _connect_thread
    _stop.set()
    with _session_lock:
        if _session_stop is not None:
            _session_stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=3.0)
    _thread = None
    ringer.stop()
    if _connect_thread is not None:
        _connect_thread.join(timeout=2)
    _connect_thread = None


def active_route():
    """The route to hand a session, or None.

    Fails closed exactly as the prototype does: select_route() raises on more
    than one phone rather than sending the microphone to the wrong one, and
    that is a reason to stay out of the way, not to crash the watcher.
    """
    try:
        route = select_route(discover())
        return route if mode.observe_route(route) else None
    except Exception as exc:
        logger.verbose(f"Bluetooth route discovery skipped: {exc}", "ℹ️")
        return None


def exit_media(snapshot, address):
    """End only the requested media session; let normal cleanup release ALSA."""
    with _session_lock:
        if not mode.dismiss_media(snapshot, address):
            return False
        if _session_stop is not None:
            _session_stop.set()
    return True


def _run():
    dismissed = None
    restarts = 0
    ring_takeover_failed = False
    while not _stop.is_set():
        _check_ringing()
        route = active_route()
        if route is None:
            # The phone stopped streaming or hung up: whatever went wrong with
            # the last session is history once the transport is gone.
            dismissed = None
            restarts = 0
            ring_takeover_failed = False
            _stop.wait(POLL_SECONDS)
            continue
        if route == dismissed:
            _stop.wait(POLL_SECONDS)
            continue
        ringing = bool(route[1]) and _only_ringing()
        if ringing and (ringer.ringtone_mode() != "phone" or ring_takeover_failed):
            # A ringing call opens its audio early (in-band ringtone), but
            # the phone may not take audio back until the call is answered:
            # taking over then fails at once, and five failures would dismiss
            # the route for the whole call. Wait until it is answered - unless
            # the user wants the phone's own ringtone played through Billy.
            _stop.wait(POLL_SECONDS)
            continue
        _stop.wait(SETTLE_SECONDS)
        if _stop.is_set() or active_route() != route:
            continue
        try:
            _run_session(route)
            restarts = 0
        except mode.MediaDismissed:
            continue
        except Exception as exc:
            if not ringing:
                restarts, dismissed = _session_failed(exc, route, restarts, dismissed)
                continue
            # The phone would not send its ringtone: never let that count
            # against the call itself. Take the call once it is answered.
            ring_takeover_failed = True
            logger.info(
                f"The phone's ringtone is not available over Bluetooth ({exc}); "
                "Billy takes the call once it is answered.",
                "📞",
            )
            _stop.wait(RESTART_BACKOFF_SECONDS)


def _session_failed(exc, route, restarts, dismissed):
    """A session that ended in an error: retry, or give the route up.

    Returns the new (restarts, dismissed).
    """
    if isinstance(exc, runtime.StreamRestart):
        restarts += 1
        if restarts < runtime.MAX_CONSECUTIVE_RESTARTS:
            # The first attach may race AVDTP Open/Start. Repeated drops
            # deserve a warning even when a later retry succeeds.
            report = logger.verbose if restarts == 1 else logger.warning
            report(
                f"Bluetooth transport interrupted; retry {restarts}/{runtime.MAX_CONSECUTIVE_RESTARTS}: {exc}"
            )
        if restarts >= runtime.MAX_CONSECUTIVE_RESTARTS:
            logger.error(f"Bluetooth audio kept dropping, giving up: {exc}")
            dismissed = route
            restarts = 0
        _stop.wait(RESTART_BACKOFF_SECONDS)
        return restarts, dismissed
    # Latch the route so a session that cannot run does not restart on every
    # poll for as long as the phone stays connected. Unplugging or
    # reconnecting clears it.
    logger.error(f"Bluetooth session failed: {exc}")
    return restarts, route


def _check_ringing():
    """Tell the ringer, at most every RING_CHECK_SECONDS, whether a call rings."""
    global _next_ring_check
    now = time.monotonic()
    if now < _next_ring_check:
        return
    _next_ring_check = now + RING_CHECK_SECONDS
    from .controls import ACTIVE_CALL_STATES, RINGING_CALL_STATES, call_states

    try:
        states = set(call_states(timeout=0.5) or ())
        ringer.update(
            bool(states & RINGING_CALL_STATES) and not states & ACTIVE_CALL_STATES
        )
    except Exception as exc:
        logger.verbose(f"Bluetooth ring check skipped: {exc}", "ℹ️")


def _only_ringing():
    """True while the phone's only call is still ringing (not answered)."""
    from .controls import ACTIVE_CALL_STATES, call_states

    states = call_states()
    return bool(states) and not set(states) & ACTIVE_CALL_STATES


def _run_session(route):
    """Take the audio path, run one session, and give it back."""
    from .. import button
    from ..config import BLUETOOTH_CALL_AEC
    from ..mqtt import mqtt_publish
    from .endpoints import SharedEndpoints

    global _session_stop

    source, sink = route
    # An answered call (or media) takes over now: stop ringing first.
    ringer.stop()
    with mode.claim(route) as running:
        session_stop = threading.Event()
        with _session_lock:
            _session_stop = session_stop
        try:
            button.stop_for_takeover("bluetooth")
            # A double press may have arrived between claiming the mode and
            # publishing _session_stop. Do not open a dismissed audio route.
            if not mode.media_allowed(route) or session_stop.is_set():
                return
            logger.info(
                f"Bluetooth {'call' if sink else 'media'} taking over audio.", "📞"
            )
            # Publishing the state is also what colours the status LED, the
            # same way song mode announces "playing_song": mqtt_publish() calls
            # set_status_led_state() for billy/state. Home Assistant gets the
            # mode for free, which is the other half of what phase 4 wanted.
            mqtt_publish("billy/state", "in_call" if sink else "bt_media")
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
            # runtime.session() reports media_audio/call_audio while it runs;
            # nothing else says when it stops.
            runtime.status(mode.IDLE)
            # Inside the claim, so the wake-word listener this re-arms cannot
            # open the microphone until mode() has gone back to idle.
            button.resume_after_takeover()
            logger.info("Bluetooth audio finished.", "🔇")
