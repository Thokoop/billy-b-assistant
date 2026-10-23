"""Startup preferences, bounded connection policy and cancellation without hardware."""

import asyncio
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from billy_runtime import bluetooth_preferences as prefs


ADDRESS = "AA:BB:CC:DD:EE:FF"
OTHER = "11:22:33:44:55:66"
ROOT = Path(__file__).parents[2]
package = ModuleType("autoconnect_test_core")
package.__path__ = [str(ROOT / "core")]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    "autoconnect_test_core.bluetooth.autoconnect",
    ROOT / "core/bluetooth/autoconnect.py",
)
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)


@pytest.fixture(autouse=True)
def preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PATH", tmp_path / "state" / "bluetooth.json")


def test_preferences_default_off_and_survive_new_read():
    assert prefs.selected() == []
    prefs.set_enabled(ADDRESS, True)
    prefs.set_enabled(OTHER, True)
    prefs.set_enabled(ADDRESS, True)
    assert prefs.selected() == [ADDRESS, OTHER]
    prefs.set_enabled(ADDRESS, False)
    assert prefs.selected() == [OTHER]


@pytest.mark.parametrize("value", [None, "true", 1, [], {}])
def test_invalid_checkbox_values_are_not_persisted(value):
    with pytest.raises(ValueError):
        prefs.set_enabled(ADDRESS, value)
    assert not prefs.PATH.exists()


def test_corruption_skips_startup_and_is_preserved():
    prefs.PATH.parent.mkdir()
    prefs.PATH.write_text("broken")
    assert prefs.selected() == []
    with pytest.raises(ValueError):
        prefs.set_enabled(ADDRESS, True)
    assert prefs.PATH.read_text() == "broken"


def manager(*, connected=False, paired=True):
    return SimpleNamespace(
        status=AsyncMock(
            return_value={
                "adapter": {"Powered": True},
                "devices": [
                    {"Address": ADDRESS, "Paired": paired, "Connected": connected},
                    {"Address": OTHER, "Paired": True, "Connected": False},
                ],
            }
        ),
        device_action=AsyncMock(return_value={"ok": True}),
    )


def test_no_opt_in_never_touches_bluez():
    m = manager()
    asyncio.run(auto.connect_selected(m))
    m.status.assert_not_awaited()


def test_startup_uses_full_connect_and_stops_after_first_success():
    prefs.set_enabled(ADDRESS, True)
    prefs.set_enabled(OTHER, True)
    m = manager()
    asyncio.run(auto.connect_selected(m))
    m.device_action.assert_awaited_once_with(ADDRESS, "connect")


def test_failed_phone_tries_next_and_does_not_loop_forever():
    prefs.set_enabled(ADDRESS, True)
    prefs.set_enabled(OTHER, True)
    m = manager()
    m.device_action.side_effect = RuntimeError("unreachable")
    asyncio.run(auto.connect_selected(m))
    assert m.device_action.await_count == 2


@pytest.mark.parametrize("connected,paired", [(True, True), (False, False)])
def test_existing_connection_or_forgotten_pairing_is_not_disturbed(connected, paired):
    prefs.set_enabled(ADDRESS, True)
    m = manager(connected=connected, paired=paired)
    asyncio.run(auto.connect_selected(m))
    m.device_action.assert_not_awaited()


def test_waits_for_adapter_startup(monkeypatch):
    prefs.set_enabled(ADDRESS, True)
    m = manager()
    good = m.status.return_value
    m.status.side_effect = [
        RuntimeError("BlueZ starting"),
        {"adapter": None},
        good,
        good,
    ]
    monkeypatch.setattr(auto.asyncio, "sleep", AsyncMock())
    asyncio.run(auto.connect_selected(m))
    m.device_action.assert_awaited_once()


def test_missing_adapter_eventually_gives_up(monkeypatch):
    prefs.set_enabled(ADDRESS, True)
    m = manager()
    m.status.side_effect = RuntimeError("BlueZ unavailable")
    monkeypatch.setattr(auto.asyncio, "sleep", AsyncMock())
    asyncio.run(auto.connect_selected(m))
    assert m.status.await_count == 15
    m.device_action.assert_not_awaited()


def test_shutdown_cancels_pending_connect_and_closes_bus(isolated, monkeypatch):
    module = isolated.load("bluetooth.autoconnect")
    bluez = isolated.load("bluetooth.bluez")
    m = manager()
    from unittest.mock import Mock

    m.bus = Mock()
    monkeypatch.setattr(bluez, "BluezController", lambda **_: m)
    cancelled = []

    async def hang(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(module, "connect_selected", hang)

    async def scenario():
        stop = threading.Event()
        task = asyncio.create_task(module._run(stop))
        await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())
    assert cancelled == [True]
    m.bus.disconnect.assert_called_once()


def test_watcher_starts_once_and_stops_auto_connect_with_audio(isolated, monkeypatch):
    from unittest.mock import Mock

    isolated.config.BLUETOOTH_AUTO_SWITCH = True
    module = isolated.load("bluetooth.watcher")
    auto_module = isolated.load("bluetooth.autoconnect")
    # Bind the replacement to the package used by the watcher's lazy import.
    import sys

    monkeypatch.setattr(
        sys.modules['isolated_core.bluetooth'],
        'autoconnect',
        auto_module,
        raising=False,
    )
    auto_thread = Mock()
    monkeypatch.setattr(auto_module, 'start', Mock(return_value=auto_thread))
    monkeypatch.setattr(module.runtime, 'status', Mock())
    monkeypatch.setattr(module, '_run', lambda: module._stop.wait(2))
    module.start()
    module.start()
    auto_module.start.assert_called_once_with(module._stop)
    module.stop()
    assert module._stop.is_set()
    auto_thread.join.assert_called_once()
    assert module._thread is None
    isolated.config.BLUETOOTH_AUTO_SWITCH = False
    module.start()
    auto_module.start.assert_called_once()


def test_shutdown_before_first_task_turn_does_not_leak_coroutine(isolated, monkeypatch):
    import gc
    import warnings
    from unittest.mock import Mock

    module = isolated.load("bluetooth.autoconnect")
    bluez = isolated.load("bluetooth.bluez")
    stop = threading.Event()
    bus = Mock()

    def controller(**_):
        stop.set()
        return SimpleNamespace(bus=bus)

    connect = AsyncMock()
    monkeypatch.setattr(bluez, "BluezController", controller)
    monkeypatch.setattr(module, "connect_selected", connect)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", RuntimeWarning)
        asyncio.run(module._run(stop))
        gc.collect()
    assert not [w for w in recorded if "never awaited" in str(w.message)]
    connect.assert_not_called()
    bus.disconnect.assert_called_once()
