"""Physical-button controls for the phone; never touch the PCM audio path.

Media uses BlueZ AVRCP. Calls use the native PipeWire Telephony service
(PipeWire 1.4+), on the same user's session bus as WirePlumber. Missing or
unsupported controls leave the audio connection intact and report a warning.
"""

import asyncio
import re
import threading
import time

from ..logger import logger
from . import mode
from .transport import pipewire_env


BLUEZ = "org.bluez"
PLAYER = "org.bluez.MediaPlayer1"
TELEPHONY = "org.pipewire.Telephony"
TELEPHONY_ROOT = "/org/pipewire/Telephony"
GATEWAY = TELEPHONY + ".AudioGateway1"
CALL = TELEPHONY + ".Call1"
OBJECTS = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
CONTROL_TIMEOUT = 3.0
DOUBLE_TAP_SECONDS = 0.35

_lock = threading.Lock()
_busy = False
_epoch = 0
_paused = None  # (address, player path, takeover generation)
_tap = None


def clear_resume():
    """An AI session or shutdown cancels any remembered media-button intent."""
    global _paused, _epoch, _tap, _busy
    with _lock:
        _paused = None
        _epoch += 1
        if _tap:
            _tap[0].cancel()
            _tap = None
            _busy = False


def _address(route):
    match = re.fullmatch(
        r"bluez_input\.([0-9a-fA-F]{2}(?:_[0-9a-fA-F]{2}){5})\..+", route[0]
    )
    if not match:
        raise RuntimeError("Cannot identify the device for this Bluetooth route")
    return match[1].replace("_", ":").upper()


def handle_button(*, ai_active=False):
    """Consume a Bluetooth press without blocking GPIO or an audio thread."""
    global _busy, _paused, _tap
    state, route, generation = snapshot = mode.snapshot()
    exit_media = False
    with _lock:
        if _tap:
            timer, first, paused, epoch, deadline = _tap
            same_media = snapshot[1:] == first[1:] or (
                state == mode.IDLE and generation == first[2] + 1
            )
            if (
                same_media
                and (not ai_active or state != mode.IDLE)
                and time.monotonic() <= deadline
            ):
                timer.cancel()
                _tap = None
                _paused = None
                snapshot = first
                exit_media = True
            elif not same_media or (ai_active and state == mode.IDLE):
                timer.cancel()
                _tap = None
                _busy = False
            else:
                return True  # The already due single press owns the worker.
        # A new takeover invalidates an old pause, even for the same phone.
        if _paused and generation not in (_paused[2], _paused[2] + 1):
            _paused = None
        if state == mode.IDLE and ai_active:
            _paused = None
            return False
        if route and not mode.media_allowed(route):
            return True  # The watcher is still releasing the audio hardware.
        if _busy and not exit_media:
            return True  # Do not queue presses that could toggle twice later.
        if state == mode.IDLE and _paused is None and not exit_media:
            return False
        _busy = True
        if not exit_media:
            paused, epoch = _paused, _epoch
        if not exit_media and (
            (route and not route[1]) or (state == mode.IDLE and paused)
        ):
            timer = threading.Timer(DOUBLE_TAP_SECONDS, _single_press)
            timer.daemon = True
            _tap = (
                timer,
                snapshot,
                paused,
                epoch,
                time.monotonic() + DOUBLE_TAP_SECONDS,
            )
            # Start under the lock so cancellation cannot race a new tap.
            try:
                timer.start()
            except Exception:
                _tap = None
                _busy = False
                raise
            return True
    try:
        threading.Thread(
            target=_exit_worker if exit_media else _worker,
            args=(snapshot, paused, epoch),
            name="bluetooth-button",
            daemon=True,
        ).start()
    except Exception as exc:
        with _lock:
            _busy = False
        logger.warning(f"Bluetooth button worker could not start: {exc}")
    return True


def _single_press():
    global _tap
    with _lock:
        if _tap is None or _tap[0] is not threading.current_thread():
            return
        _, snapshot, paused, epoch, _ = _tap
        _tap = None
    _worker(snapshot, paused, epoch)


def _exit_worker(snapshot, paused, epoch):
    """Pause remotely, then leave media even if the phone ignores the command."""
    global _busy, _paused
    from . import watcher

    try:
        address = _address(snapshot[1]) if snapshot[1] else paused[0]
        try:
            asyncio.run(
                asyncio.wait_for(
                    _pause_for_exit(snapshot, paused, epoch), timeout=CONTROL_TIMEOUT
                )
            )
        except Exception as exc:
            logger.warning(
                f"Could not pause device while leaving Bluetooth media: {exc}"
            )
        with _lock:
            if epoch != _epoch:
                return
            _paused = None
            if watcher.exit_media(snapshot, address):
                logger.info("Leaving Bluetooth media. Returning to AI standby.", "⏹️")
    except Exception as exc:
        logger.warning(f"Could not leave Bluetooth media: {exc}")
    finally:
        with _lock:
            _busy = False


async def _pause_for_exit(snapshot, paused, epoch):
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    guard = lambda: _current(snapshot, epoch)
    _check(guard)
    address = _address(snapshot[1]) if snapshot[1] else paused[0]
    bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()
        await _toggle_media(bus, address, None, guard, pause_only=True)
    finally:
        bus.disconnect()


def _current(snapshot, epoch):
    with _lock:
        if epoch != _epoch:
            return False
    # STARTING -> RUNNING is the same takeover; ending or replacing it is not.
    return mode.snapshot()[1:] == snapshot[1:]


def _worker(snapshot, paused, epoch):
    global _busy, _paused
    try:
        result = asyncio.run(_bounded_control(snapshot, paused, epoch))
        with _lock:
            current = mode.snapshot()
            if epoch == _epoch and (
                current[1:] == snapshot[1:]
                or (current[0] == mode.IDLE and current[2] == snapshot[2] + 1)
            ):
                _paused = result
    except Exception as exc:
        with _lock:
            if epoch == _epoch:
                _paused = None
        detail = str(exc) or "Bluetooth button operation timed out"
        logger.warning(f"Bluetooth button: {detail}")
    finally:
        with _lock:
            _busy = False


async def _bounded_control(snapshot, paused, epoch):
    return await asyncio.wait_for(
        _control(snapshot, paused, epoch), timeout=CONTROL_TIMEOUT
    )


async def _interface(bus, service, path, name):
    intro = await bus.introspect(service, path)
    return bus.get_proxy_object(service, path, intro).get_interface(name)


async def _objects(bus, service, path):
    manager = await _interface(bus, service, path, OBJECTS)
    return await manager.call_get_managed_objects()


def _value(props, key, default=None):
    value = props.get(key)
    return value.value if value is not None else default


def _check(guard):
    if not guard():
        raise RuntimeError("Audio mode changed; skipped outdated button command")


async def _control(snapshot, paused, epoch):
    # Lazy imports: starting Billy without Bluetooth does not need D-Bus.
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    state, route, generation = snapshot
    guard = lambda: _current(snapshot, epoch)
    _check(guard)
    if state != mode.IDLE and route is None:
        raise RuntimeError("Bluetooth route is not ready for button control")
    address = _address(route) if route else paused[0]
    is_call = bool(route and route[1])
    if is_call:
        bus = MessageBus(bus_address=pipewire_env()["DBUS_SESSION_BUS_ADDRESS"])
    else:
        bus = MessageBus(bus_type=BusType.SYSTEM)
    try:
        await bus.connect()
        if is_call:
            await _hangup(bus, address, guard)
            logger.info("Bluetooth button: hang-up requested.", "📞")
            return None
        remembered_player = paused[1] if state == mode.IDLE else None
        action, player = await _toggle_media(bus, address, remembered_player, guard)
        logger.info(f"Bluetooth button: {action} requested.", "⏯️")
        return (address, player, generation) if action == "pause" else None
    finally:
        bus.disconnect()


async def _toggle_media(bus, address, resume_player, guard, *, pause_only=False):
    objects = await _objects(bus, BLUEZ, "/")
    devices = [
        path
        for path, interfaces in objects.items()
        if _value(interfaces.get("org.bluez.Device1", {}), "Address", "").upper()
        == address
        and _value(interfaces["org.bluez.Device1"], "Connected", False)
    ]
    if len(devices) != 1:
        raise RuntimeError("Media device is disconnected or ambiguous")
    device = devices[0]
    control = objects[device].get("org.bluez.MediaControl1", {})
    if not _value(control, "Connected", False):
        raise RuntimeError("Device does not expose Bluetooth media controls")
    players = [
        path
        for path, interfaces in objects.items()
        if _value(interfaces.get(PLAYER, {}), "Device") == device
    ]
    player = _value(control, "Player")
    if player is None and len(players) == 1:
        player = players[0]
    if player not in players or (resume_player and player != resume_player):
        raise RuntimeError(
            "Media player changed or is ambiguous; press again to use Billy"
        )
    props = await _interface(bus, BLUEZ, player, PROPERTIES)
    status = (await props.call_get(PLAYER, "Status")).value
    if resume_player and status != "paused":
        raise RuntimeError("Media is no longer paused; press again to use Billy")
    if pause_only and status in {"paused", "stopped"}:
        _check(guard)
        return "pause", player
    if pause_only or status in {"playing", "forward-seek", "reverse-seek"}:
        action = "pause"
    elif status in {"paused", "stopped"}:
        action = "play"
    else:
        raise RuntimeError("Device did not report a usable media playback state")
    interface = await _interface(bus, BLUEZ, player, PLAYER)
    _check(guard)
    if action == "pause":
        await interface.call_pause()
    else:
        await interface.call_play()
    return action, player


async def _hangup(bus, address, guard):
    try:
        objects = await _objects(bus, TELEPHONY, TELEPHONY_ROOT)
    except Exception as exc:
        raise RuntimeError(
            "Hang-up unavailable: requires PipeWire 1.4+ with its native "
            "Telephony service on the user bus; the call remains connected"
        ) from exc
    gateways = [
        path
        for path, interfaces in objects.items()
        if _value(interfaces.get(GATEWAY, {}), "Address", "").upper() == address
    ]
    if len(gateways) != 1:
        raise RuntimeError(
            "Cannot identify the device for this call in the Telephony service"
        )
    gateway = gateways[0]
    calls = await _objects(bus, TELEPHONY, gateway)
    candidates = [
        path
        for path, interfaces in calls.items()
        if path.startswith(gateway + "/")
        and _value(interfaces.get(CALL, {}), "State")
        in {"active", "dialing", "alerting"}
    ]
    # Never answer/reject a waiting call or end unrelated held calls.
    if len(candidates) != 1:
        raise RuntimeError("No single active phone call to hang up")
    path = candidates[0]
    props = await _interface(bus, TELEPHONY, path, PROPERTIES)
    if (await props.call_get(CALL, "State")).value not in {
        "active",
        "dialing",
        "alerting",
    }:
        raise RuntimeError("Call state changed; skipped hang-up")
    interface = await _interface(bus, TELEPHONY, path, CALL)
    _check(guard)
    await interface.call_hangup()
