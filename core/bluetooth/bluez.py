"""BlueZ pairing agent. Runs on one persistent asyncio loop, without GPIO imports."""

import asyncio
import secrets
import threading
import time

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, method


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
        if uuid.lower() not in AUDIO_SERVICES or not (trusted and trusted.value):
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
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(
            target=self.loop.run_forever, daemon=True, name="bluetooth-pairing"
        ).start()
        self.bus = None
        self.registered = False
        self.pending = None
        self.deadline = 0
        self.timer = None
        self.pairing_lock = asyncio.Lock()

    def run(self, coroutine):
        task = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return task.result(timeout=15)
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
        objects = await self.objects()
        devices = []
        adapter = None
        for path, interfaces in objects.items():
            if "org.bluez.Adapter1" in interfaces and adapter is None:
                props = interfaces["org.bluez.Adapter1"]
                adapter = {
                    key: value.value
                    for key, value in props.items()
                    if key in {"Alias", "Powered", "Discoverable"}
                }
            if "org.bluez.Device1" in interfaces:
                props = interfaces["org.bluez.Device1"]
                devices.append({
                    key: value.value
                    for key, value in props.items()
                    if key in {"Address", "Alias", "Paired", "Connected"}
                })
        pending = None
        if self.pending:
            pending = {k: v for k, v in self.pending.items() if k != "future"}
        return {
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
            ("Alias", "s", "Billy Bass"),
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
        await asyncio.sleep(120)
        await self.stop_pairing()

    async def stop_pairing(self):
        self.deadline = 0
        self.reject_pending()
        path = await self.adapter()
        for name in ("Discoverable", "Pairable"):
            await self.set_property(path, "org.bluez.Adapter1", name, "b", False)
        return {"ok": True}

    async def confirm(self, device, passkey):
        if time.monotonic() >= self.deadline or self.pending:
            raise DBusError("org.bluez.Error.Rejected", "Pairing window closed or busy")
        props = (await self.objects()).get(device, {}).get("org.bluez.Device1", {})
        future = self.loop.create_future()
        self.pending = {
            "id": secrets.token_hex(16),
            "passkey": passkey,
            "name": props.get("Alias", Variant("s", "Phone")).value,
            "future": future,
        }
        try:
            if not await asyncio.wait_for(
                future, min(60, self.deadline - time.monotonic())
            ):
                raise DBusError("org.bluez.Error.Rejected", "Pairing rejected")
            await self.set_property(device, "org.bluez.Device1", "Trusted", "b", True)
        except TimeoutError:
            raise DBusError("org.bluez.Error.Rejected", "Pairing timed out") from None
        finally:
            self.pending = None

    def reject_pending(self):
        if self.pending and not self.pending["future"].done():
            self.pending["future"].set_result(False)

    async def respond(self, request_id, accepted):
        if not self.pending or self.pending["id"] != request_id:
            raise ValueError("Pairing request expired")
        if not self.pending["future"].done():
            self.pending["future"].set_result(accepted)
        return {"ok": True}

    async def device_action(self, address, action):
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
            else:
                if (
                    action == "connect"
                    and not props.get("Paired", Variant("b", False)).value
                ):
                    raise ValueError("Pair this device from your phone first")
                device = await self.interface(path, "org.bluez.Device1")
                if action == "connect":
                    await device.call_connect()
                elif action == "disconnect":
                    await device.call_disconnect()
                else:
                    raise ValueError("Unknown device action")
            return {"ok": True}
        raise ValueError("Device not found")
