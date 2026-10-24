"""BlueZ pairing agent. Runs on one persistent asyncio loop, without GPIO imports."""

import asyncio
import logging
import os
import secrets
import threading
import time

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method

from billy_runtime import bluetooth_preferences


A2DP_SOURCE = "0000110a-0000-1000-8000-00805f9b34fb"
A2DP_SINK = "0000110b-0000-1000-8000-00805f9b34fb"
CONNECT_TIMEOUT_SECONDS = 12
MEDIA_CONNECT_TIMEOUT_SECONDS = 10
MEDIA_CONNECT_ATTEMPTS = 4
MEDIA_RETRY_ERRORS = {
    "org.bluez.Error.InProgress",
    "org.bluez.Error.NotReady",
    "org.bluez.Error.NotAvailable",
    "org.bluez.Error.Failed",
}


def device_name():
    """Match the header's hostname label without changing the remaining letters."""
    hostname = os.uname().nodename or "billy"
    return hostname[:1].upper() + hostname[1:]


def media_connected(objects, device_path):
    """An idle A2DP transport counts too; playback need not have started."""
    for interfaces in objects.values():
        props = interfaces.get("org.bluez.MediaTransport1", {})
        if props.get("Device", Variant("o", "/")).value == device_path and props.get(
            "UUID", Variant("s", "")
        ).value.lower() in {A2DP_SOURCE, A2DP_SINK}:
            return True
    return False


AUDIO_SERVICES = {
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP source
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP sink
    "0000110c-0000-1000-8000-00805f9b34fb",  # AVRCP target
    "0000110e-0000-1000-8000-00805f9b34fb",  # AVRCP
    "0000111e-0000-1000-8000-00805f9b34fb",  # Hands-free
    "0000111f-0000-1000-8000-00805f9b34fb",  # Audio gateway
}


class PairingAgent(ServiceInterface):
    def __init__(self, owner):
        super().__init__("org.bluez.Agent1")
        self.owner = owner

    @method()
    async def RequestConfirmation(self, device: 'o', passkey: 'u'):
        await self.owner.confirm(device, f"{passkey:06d}")

    @method()
    async def RequestAuthorization(self, device: 'o'):
        await self.owner.confirm(device, None)

    @method()
    async def AuthorizeService(self, device: 'o', uuid: 's'):
        objects = await self.owner.objects()
        props = objects.get(device, {}).get("org.bluez.Device1", {})
        trusted = props.get("Trusted")
        paired = props.get("Paired")
        requested = (
            self.owner.pending
            and self.owner.pending.get("device") == device
            and time.monotonic() < self.owner.deadline
            and paired
            and paired.value
        )
        if uuid.lower() not in AUDIO_SERVICES or not (
            (trusted and trusted.value) or requested
        ):
            raise DBusError(
                "org.bluez.Error.Rejected", "Device is not trusted for audio"
            )

    @method()
    def Cancel(self):
        self.owner.reject_pending()

    @method()
    def Release(self):
        self.owner.reject_pending()
        self.owner.registered = False


class BluezController:
    def __init__(self, *, threaded=True):
        if threaded:
            self.loop = asyncio.new_event_loop()
            threading.Thread(
                target=self.loop.run_forever, daemon=True, name="bluetooth-pairing"
            ).start()
        else:
            self.loop = asyncio.get_running_loop()
        self.bus = None
        self.registered = False
        self.pending = None
        self.deadline = 0
        self.timer = None
        self.pairing_lock = asyncio.Lock()
        self.device_lock = asyncio.Lock()

    def run(self, coroutine, *, timeout=15):
        task = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return task.result(timeout=timeout)
        except TimeoutError:
            task.cancel()
            raise RuntimeError("Bluetooth operation timed out") from None

    async def interface(self, path, name):
        if self.bus is None or not self.bus.connected:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self.registered = False
        intro = await self.bus.introspect("org.bluez", path)
        return self.bus.get_proxy_object("org.bluez", path, intro).get_interface(name)

    async def objects(self):
        manager = await self.interface("/", "org.freedesktop.DBus.ObjectManager")
        return await manager.call_get_managed_objects()

    async def adapter(self):
        for path, interfaces in (await self.objects()).items():
            if "org.bluez.Adapter1" in interfaces:
                return path
        raise RuntimeError("No Bluetooth adapter found")

    async def set_property(self, path, interface, name, signature, value):
        props = await self.interface(path, "org.freedesktop.DBus.Properties")
        await props.call_set(interface, name, Variant(signature, value))

    async def status(self):
        sampled_at = time.monotonic()
        objects = await self.objects()
        devices = []
        selected = bluetooth_preferences.selected()
        adapter = None
        for path, interfaces in objects.items():
            if "org.bluez.Adapter1" in interfaces and adapter is None:
                props = interfaces["org.bluez.Adapter1"]
                adapter = {
                    key: value.value
                    for key, value in props.items()
                    if key in {"Alias", "Powered", "Discoverable"}
                }
                # Migrate an existing alias and follow hostname edits without
                # opening pairing or interrupting any connected devices.
                name = device_name()
                if adapter.get("Alias") != name:
                    try:
                        await asyncio.wait_for(
                            self.set_property(
                                path, "org.bluez.Adapter1", "Alias", "s", name
                            ),
                            timeout=1,
                        )
                    except Exception:
                        # Naming is optional; a failed write must not hide devices.
                        # The next status sample retries and reports the actual alias.
                        logging.getLogger(__name__).debug(
                            "Could not update Bluetooth name", exc_info=True
                        )
                    else:
                        adapter["Alias"] = name
            if "org.bluez.Device1" in interfaces:
                props = interfaces["org.bluez.Device1"]
                device = {
                    key: value.value
                    for key, value in props.items()
                    if key in {"Address", "Alias", "Paired", "Connected"}
                }
                device["AutoConnect"] = (
                    bool(device.get("Paired")) and device.get("Address") in selected
                )
                device["MediaSupported"] = A2DP_SOURCE in {
                    uuid.lower() for uuid in props.get("UUIDs", Variant("as", [])).value
                }
                device["MediaConnected"] = bool(
                    device.get("Connected")
                ) and media_connected(objects, path)
                devices.append(device)
        pending = None
        if self.pending:
            pending = {k: v for k, v in self.pending.items() if k != "future"}
        return {
            "sampled_at": sampled_at,
            "adapter": adapter,
            "devices": devices,
            "pending": pending,
            "pairing_seconds": max(0, int(self.deadline - time.monotonic())),
        }

    async def start_pairing(self):
        async with self.pairing_lock:
            return await self._start_pairing()

    async def _start_pairing(self):
        path = await self.adapter()
        if not self.registered:
            self.bus.unexport("/billy/agent")
            self.bus.export("/billy/agent", PairingAgent(self))
            manager = await self.interface("/org/bluez", "org.bluez.AgentManager1")
            await manager.call_register_agent("/billy/agent", "DisplayYesNo")
            try:
                await manager.call_request_default_agent("/billy/agent")
            except Exception:
                await manager.call_unregister_agent("/billy/agent")
                self.bus.unexport("/billy/agent")
                raise
            self.registered = True
        self.reject_pending()
        for name, sig, value in [
            ("Powered", "b", True),
            ("Alias", "s", device_name()),
            ("DiscoverableTimeout", "u", 120),
            ("PairableTimeout", "u", 120),
            ("Pairable", "b", True),
            ("Discoverable", "b", True),
        ]:
            await self.set_property(path, "org.bluez.Adapter1", name, sig, value)
        self.deadline = time.monotonic() + 120
        if self.timer:
            self.timer.cancel()
        self.timer = asyncio.create_task(self.expire())
        return {"ok": True}

    async def expire(self):
        # Independent of UI polling: closing the browser must not leave the
        # adapter discoverable after the phone completes pairing.
        while self.deadline:
            await asyncio.sleep(min(1, max(0.25, self.deadline - time.monotonic())))
            async with self.pairing_lock:
                try:
                    if time.monotonic() >= self.deadline:
                        await self._stop_pairing()
                        return
                    await asyncio.wait_for(self.finish_connected_pairing(), timeout=3)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "Could not finish Bluetooth pairing; retrying", exc_info=True
                    )

    async def finish_connected_pairing(self):
        target = self.pending.get("device") if self.pending else None
        for path, interfaces in (await self.objects()).items():
            props = interfaces.get("org.bluez.Device1", {})
            if target and path != target:
                continue
            if all(
                props.get(key, Variant("b", False)).value
                for key in ("Paired", "Connected")
            ):
                await self.set_property(path, "org.bluez.Device1", "Trusted", "b", True)
                await self._stop_pairing(cancel_timer=False)
                return

    async def stop_pairing(self):
        async with self.pairing_lock:
            return await self._stop_pairing()

    async def _stop_pairing(self, *, cancel_timer=True):
        if self.pending:
            device_path = self.pending["device"]
            props = (
                (await self.objects()).get(device_path, {}).get("org.bluez.Device1", {})
            )
            if props and not props.get("Paired", Variant("b", False)).value:
                device = await self.interface(device_path, "org.bluez.Device1")
                try:
                    await device.call_cancel_pairing()
                except DBusError as exc:
                    if exc.type not in {
                        "org.bluez.Error.DoesNotExist",
                        "org.bluez.Error.NotReady",
                    }:
                        raise
        path = await self.adapter()
        for name in ("Discoverable", "Pairable"):
            await self.set_property(path, "org.bluez.Adapter1", name, "b", False)
        self.deadline = 0
        self.reject_pending()
        if cancel_timer and self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
        self.timer = None
        return {"ok": True}

    async def confirm(self, device, passkey):
        async with self.pairing_lock:
            if time.monotonic() >= self.deadline or self.pending:
                raise DBusError(
                    "org.bluez.Error.Rejected", "Pairing window closed or busy"
                )
            props = (await self.objects()).get(device, {}).get("org.bluez.Device1", {})
            # Discovery may have taken us past the end of the pairing window.
            if time.monotonic() >= self.deadline:
                raise DBusError("org.bluez.Error.Rejected", "Pairing window closed")
            self.pending = {
                "id": secrets.token_hex(16),
                "device": device,
                "passkey": passkey,
                "name": props.get("Alias", Variant("s", "Phone")).value,
            }
            # Opening the short pairing window is Billy's local consent.
            # Return immediately; the phone still asks its user to pair.
            # Trust is saved only once BlueZ reports Paired AND Connected.

    def reject_pending(self):
        self.pending = None

    async def connect_audio(self, device, path, initial_props):
        media_fallback = False
        try:
            await device.call_connect()
        except DBusError as exc:
            if (
                exc.type == "org.bluez.Error.Failed"
                and exc.text == "le-connection-abort-by-local"
            ):
                # Generic Connect may choose LE on a dual-mode phone/laptop.
                # A paired device with a known A2DP source can instead connect
                # that classic audio profile, within the same action timeout.
                media_fallback = A2DP_SOURCE in {
                    uuid.lower()
                    for uuid in initial_props.get("UUIDs", Variant("as", [])).value
                }
                logging.getLogger(__name__).log(
                    logging.INFO if media_fallback else logging.WARNING,
                    "Bluetooth generic connection failed: %s (%s); audio fallback: %s",
                    exc.type,
                    exc.text,
                    media_fallback,
                )
                if not media_fallback:
                    raise RuntimeError(
                        "Could not connect. Make sure Bluetooth is on and select "
                        f"{device_name()} on your device, then try again."
                    ) from exc
            elif exc.type not in {
                "org.bluez.Error.AlreadyConnected",
                "org.bluez.Error.InProgress",
            }:
                raise
        # Connect can complete with HFP while discovery/A2DP is still starting.
        # Keep the existing call/link intact and retry only the media profile.
        deadline = time.monotonic() + MEDIA_CONNECT_TIMEOUT_SECONDS
        next_attempt = 0
        attempts = 0
        accepted = False
        saw_connected = bool(initial_props.get("Connected", Variant("b", False)).value)
        while time.monotonic() < deadline:
            current = await self.objects()
            props = current.get(path, {}).get("org.bluez.Device1", {})
            if not props or not props.get("Paired", Variant("b", False)).value:
                raise RuntimeError("Bluetooth device is no longer paired or available")
            connected = props.get("Connected", Variant("b", False)).value
            if saw_connected and not connected:
                raise RuntimeError("Phone disconnected while connecting media audio")
            saw_connected = saw_connected or connected
            if connected and media_connected(current, path):
                return
            resolved = props.get("ServicesResolved")
            uuids = props.get("UUIDs", initial_props.get("UUIDs", Variant("as", [])))
            supports_media = media_fallback or A2DP_SOURCE in {
                uuid.lower() for uuid in uuids.value
            }
            link_ready = connected and (resolved is None or resolved.value)
            if link_ready and not supports_media:
                return  # A device without A2DP needs no media connection.
            if (
                supports_media
                and (link_ready or media_fallback)
                and not accepted
                and attempts < MEDIA_CONNECT_ATTEMPTS
                and time.monotonic() >= next_attempt
            ):
                attempts += 1
                try:
                    await device.call_connect_profile(A2DP_SOURCE)
                    accepted = True
                except DBusError as exc:
                    if exc.type == "org.bluez.Error.AlreadyConnected":
                        accepted = True
                    elif exc.type in MEDIA_RETRY_ERRORS:
                        logging.getLogger(__name__).info(
                            "Bluetooth media attempt %s/%s deferred: %s (%s)",
                            attempts,
                            MEDIA_CONNECT_ATTEMPTS,
                            exc.type,
                            exc.text,
                        )
                        next_attempt = time.monotonic() + min(2 ** (attempts - 1), 4)
                    else:
                        logging.getLogger(__name__).warning(
                            "Bluetooth media connection failed: %s (%s)",
                            exc.type,
                            exc.text,
                        )
                        raise RuntimeError(
                            "Media audio could not connect. Check Bluetooth on your phone."
                        ) from exc
            # A successful D-Bus return alone does not mean audio is ready yet.
            await asyncio.sleep(0.25)
        logging.getLogger(__name__).warning(
            "Bluetooth media transport did not become ready after %s profile attempts",
            attempts,
        )
        raise TimeoutError("Bluetooth media transport not ready")

    async def disconnect_audio(self, device, path):
        """One UI action tears down the whole link, including racing profiles."""
        deadline = time.monotonic() + 6
        next_attempt = 0
        attempts = 0
        disconnected_since = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if disconnected_since is None and now >= next_attempt and attempts < 3:
                attempts += 1
                try:
                    await device.call_disconnect()
                except DBusError as exc:
                    if exc.type not in {
                        "org.bluez.Error.NotConnected",
                        "org.bluez.Error.InProgress",
                    }:
                        raise
                next_attempt = time.monotonic() + 1
            props = (await self.objects()).get(path, {}).get("org.bluez.Device1", {})
            if not props.get("Connected", Variant("b", False)).value:
                # A just-completing profile connection may bring the link back.
                # Do not report success on that brief disconnected transition.
                if disconnected_since is None:
                    disconnected_since = time.monotonic()
                if time.monotonic() - disconnected_since >= 0.5:
                    return
            else:
                disconnected_since = None
            await asyncio.sleep(0.1)
        raise TimeoutError("Bluetooth device is still connected")

    async def set_auto_connect(self, address, enabled):
        for interfaces in (await self.objects()).values():
            props = interfaces.get("org.bluez.Device1", {})
            if props.get("Address", Variant("s", "")).value == address:
                if not props.get("Paired", Variant("b", False)).value:
                    raise ValueError("Pair this device from your phone first")
                await asyncio.to_thread(
                    bluetooth_preferences.set_enabled, address, enabled
                )
                return {"ok": True}
        raise ValueError("Device not found")

    async def device_action(self, address, action):
        # Reject overlapping requests from other tabs instead of queuing a
        # stale selection that could undo the user's most recent action.
        if self.device_lock.locked():
            raise RuntimeError(
                "Bluetooth is busy. Wait for the current action to finish."
            )
        async with self.device_lock:
            if action == "select":
                return await self.select_device(address)
            return await self._device_action(address, action)

    async def select_device(self, address):
        """Explicit UI selection may replace a connection; startup auto-connect may not."""
        objects = await self.objects()
        target = next(
            (
                path
                for path, interfaces in objects.items()
                if interfaces.get("org.bluez.Device1", {})
                .get("Address", Variant("s", ""))
                .value
                == address
            ),
            None,
        )
        if target is None:
            raise ValueError("Device not found")
        if (
            not objects[target]["org.bluez.Device1"]
            .get("Paired", Variant("b", False))
            .value
        ):
            raise ValueError("Pair this device from your phone first")

        async def disconnect_others():
            for path, interfaces in objects.items():
                props = interfaces.get("org.bluez.Device1", {})
                if path != target and props.get("Connected", Variant("b", False)).value:
                    device = await self.interface(path, "org.bluez.Device1")
                    await self.disconnect_audio(device, path)
            # A phone may reconnect on its own while another is disconnecting.
            # Never start the selected connection without confirming exclusivity.
            current = await self.objects()
            if any(
                path != target
                and interfaces.get("org.bluez.Device1", {})
                .get("Connected", Variant("b", False))
                .value
                for path, interfaces in current.items()
            ):
                raise RuntimeError(
                    "Another phone is still connected. Try selecting your device again."
                )

        try:
            await asyncio.wait_for(disconnect_others(), timeout=10)
        except TimeoutError as exc:
            raise RuntimeError(
                "The current phone could not disconnect. The new connection was not started."
            ) from exc
        # Re-read pairing and availability after teardown, before connecting.
        return await self._device_action(address, "connect")

    async def _device_action(self, address, action):
        objects = await self.objects()
        for path, interfaces in objects.items():
            props = interfaces.get("org.bluez.Device1", {})
            if props.get("Address", Variant("s", "")).value != address:
                continue
            if action == "forget":
                adapter = await self.interface(
                    path.rsplit("/", 1)[0], "org.bluez.Adapter1"
                )
                await adapter.call_remove_device(path)
                await asyncio.to_thread(
                    bluetooth_preferences.set_enabled, address, False
                )
            else:
                if (
                    action == "connect"
                    and not props.get("Paired", Variant("b", False)).value
                ):
                    raise ValueError("Pair this device from your phone first")
                device = await self.interface(path, "org.bluez.Device1")
                if action == "connect":
                    try:
                        await asyncio.wait_for(
                            self.connect_audio(device, path, props),
                            timeout=CONNECT_TIMEOUT_SECONDS,
                        )
                    except TimeoutError as exc:
                        raise RuntimeError(
                            "Media audio could not connect in time. "
                            f"Select {device_name()} on your phone or try again."
                        ) from exc
                elif action == "disconnect":
                    try:
                        await asyncio.wait_for(
                            self.disconnect_audio(device, path), timeout=8
                        )
                    except TimeoutError as exc:
                        raise RuntimeError(
                            f"Phone is still connected. Disconnect {device_name()} in your phone's Bluetooth settings."
                        ) from exc
                else:
                    raise ValueError("Unknown device action")
            return {"ok": True}
        raise ValueError("Device not found")
