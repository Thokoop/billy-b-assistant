"""One bounded background connection pass per core startup; never an audio loop."""

import asyncio
import logging
import threading

from billy_runtime import bluetooth_preferences


logger = logging.getLogger(__name__)


async def connect_selected(manager):
    # BlueZ and the user audio services may still be starting at boot.
    for _ in range(15):
        if not bluetooth_preferences.selected():
            return
        try:
            state = await asyncio.wait_for(manager.status(), timeout=3)
            if (state.get("adapter") or {}).get("Powered"):
                break
        except Exception:
            pass
        await asyncio.sleep(2)
    else:
        logger.info("Bluetooth auto-connect skipped: adapter not ready")
        return

    for address in bluetooth_preferences.selected():
        state = await asyncio.wait_for(manager.status(), timeout=3)
        devices = state["devices"]
        # Do not add another phone to an existing call or media connection.
        if any(d.get("Connected") for d in devices):
            return
        if address not in bluetooth_preferences.selected():
            continue
        if not any(d.get("Address") == address and d.get("Paired") for d in devices):
            continue
        try:
            await manager.device_action(address, "connect")
            logger.info("Bluetooth auto-connect completed")
            return
        except Exception as exc:
            logger.info("Bluetooth auto-connect attempt failed: %s", exc)


async def _run(stop):
    from .bluez import BluezController

    manager = BluezController(threaded=False)
    task = asyncio.create_task(asyncio.wait_for(connect_selected(manager), timeout=60))
    try:
        while not task.done() and not stop.is_set():
            await asyncio.sleep(0.25)
        if stop.is_set():
            task.cancel()
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.info("Bluetooth auto-connect stopped: %s", exc)
    finally:
        task.cancel()
        if manager.bus is not None:
            manager.bus.disconnect()


def start(stop):
    def worker():
        try:
            if not stop.is_set() and bluetooth_preferences.selected():
                asyncio.run(_run(stop))
        except Exception as exc:
            logger.warning("Bluetooth auto-connect unavailable: %s", exc)

    thread = threading.Thread(target=worker, daemon=True, name="bluetooth-autoconnect")
    thread.start()
    return thread
