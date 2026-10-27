"""Local button/phone regressions, with real state and mocked D-Bus boundaries."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import definitions
from dbus_next import Variant


ADDRESS = "1C:3C:78:C9:7C:61"
DEVICE = "/org/bluez/hci0/dev_1C_3C_78_C9_7C_61"
PLAYER = DEVICE + "/player0"
SOURCE = "bluez_input.1C_3C_78_C9_7C_61.2"
MEDIA = (SOURCE, None)
PHONE_CALL = (SOURCE, "bluez_output.1C_3C_78_C9_7C_61.1")
AG = "/org/pipewire/Telephony/ag0"


@pytest.fixture
def controls(isolated, monkeypatch):
    controls = isolated.load("bluetooth.controls")
    monkeypatch.setattr(controls, "mode", isolated.load("bluetooth.mode"))
    return controls


def media_objects(c, *, connected=True):
    return {
        DEVICE: {
            "org.bluez.Device1": {
                "Address": Variant("s", ADDRESS),
                "Connected": Variant("b", connected),
            },
            "org.bluez.MediaControl1": {
                "Connected": Variant("b", True),
                "Player": Variant("o", PLAYER),
            },
        },
        PLAYER: {c.PLAYER: {"Device": Variant("o", DEVICE)}},
    }


def setup_media(c, monkeypatch, status="playing", objects=None):
    c._objects = AsyncMock(return_value=objects or media_objects(c))
    props = SimpleNamespace(call_get=AsyncMock(return_value=Variant("s", status)))
    player = SimpleNamespace(call_pause=AsyncMock(), call_play=AsyncMock())

    async def interface(bus, service, path, name):
        assert service == c.BLUEZ
        assert path == PLAYER
        return props if name == c.PROPERTIES else player

    monkeypatch.setattr(c, "_interface", interface)
    return player, props


@pytest.mark.parametrize(
    "status,action",
    [
        ("playing", "pause"),
        ("paused", "play"),
        ("stopped", "play"),
        ("forward-seek", "pause"),
        ("reverse-seek", "pause"),
    ],
)
def test_media_toggles_remote_player(controls, monkeypatch, status, action):
    c = controls
    player, _ = setup_media(c, monkeypatch, status)
    assert asyncio.run(c._toggle_media(None, ADDRESS, None, lambda: True)) == (
        action,
        PLAYER,
    )
    getattr(player, "call_" + action).assert_awaited_once()
    getattr(
        player, "call_" + ("play" if action == "pause" else "pause")
    ).assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["disconnected", "changed", "ambiguous", "no_control", "error", "stale"]
)
def test_invalid_media_never_sends_command(controls, monkeypatch, case):
    c = controls
    objects = media_objects(c, connected=case != "disconnected")
    if case == "ambiguous":
        objects[DEVICE]["org.bluez.MediaControl1"].pop("Player")
        objects[DEVICE + "/player1"] = {c.PLAYER: {"Device": Variant("o", DEVICE)}}
    if case == "no_control":
        objects[DEVICE].pop("org.bluez.MediaControl1")
    player, _ = setup_media(
        c, monkeypatch, "error" if case == "error" else "playing", objects
    )
    with pytest.raises(RuntimeError):
        asyncio.run(
            c._toggle_media(
                None,
                ADDRESS,
                "/old" if case == "changed" else None,
                lambda: case != "stale",
            )
        )
    player.call_pause.assert_not_awaited()
    player.call_play.assert_not_awaited()


def test_resume_only_the_remembered_paused_player(controls, monkeypatch):
    c = controls
    player, props = setup_media(c, monkeypatch, "paused")
    asyncio.run(c._toggle_media(None, ADDRESS, PLAYER, lambda: True))
    player.call_play.assert_awaited_once()
    props.call_get.return_value = Variant("s", "playing")
    with pytest.raises(RuntimeError, match="no longer paused"):
        asyncio.run(c._toggle_media(None, ADDRESS, PLAYER, lambda: True))
    player.call_pause.assert_not_awaited()


def setup_call(c, monkeypatch, states=("active", "held", "waiting")):
    objects = {AG: {c.GATEWAY: {"Address": Variant("s", ADDRESS)}}}
    calls = {
        AG + f"/call{i}": {c.CALL: {"State": Variant("s", s)}}
        for i, s in enumerate(states)
    }
    c._objects = AsyncMock(side_effect=[objects, calls])
    props = SimpleNamespace(
        call_get=AsyncMock(
            return_value=Variant("s", states[0] if states else "disconnected")
        )
    )
    call = SimpleNamespace(call_hangup=AsyncMock())

    async def interface(bus, service, path, name):
        assert service == c.TELEPHONY
        assert path == AG + "/call0"
        return props if name == c.PROPERTIES else call

    monkeypatch.setattr(c, "_interface", interface)
    return call, props


def test_hangup_targets_active_call_not_held_or_waiting(controls, monkeypatch):
    c = controls
    call, _ = setup_call(c, monkeypatch)
    asyncio.run(c._hangup(None, ADDRESS, lambda: True))
    call.call_hangup.assert_awaited_once()
    assert c._objects.await_args_list[1].args[2] == AG


@pytest.mark.parametrize(
    "states", [(), ("held",), ("waiting",), ("incoming",), ("active", "active")]
)
def test_no_ambiguous_or_unrelated_call_is_ended(controls, monkeypatch, states):
    c = controls
    call, _ = setup_call(c, monkeypatch, states)
    with pytest.raises(RuntimeError, match="single active"):
        asyncio.run(c._hangup(None, ADDRESS, lambda: True))
    call.call_hangup.assert_not_awaited()


@pytest.mark.parametrize(
    "case", ["wrong_phone", "stale_route", "ended_call", "unsupported"]
)
def test_hangup_failure_leaves_call_alone(controls, monkeypatch, case):
    c = controls
    call, props = setup_call(c, monkeypatch)
    if case == "ended_call":
        props.call_get.return_value = Variant("s", "disconnected")
    if case == "unsupported":
        c._objects.side_effect = RuntimeError("service missing")
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(
            c._hangup(
                None,
                "FF:FF:FF:FF:FF:FF" if case == "wrong_phone" else ADDRESS,
                lambda: case != "stale_route",
            )
        )
    call.call_hangup.assert_not_awaited()
    if case == "unsupported":
        assert "PipeWire 1.4+" in str(failure.value)


def capture_worker(c, monkeypatch):
    pending = []

    class Thread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            pending.append(lambda: self.target(*self.args))

    current = [None]

    class Timer:
        def __init__(self, interval, target):
            self.target = target
            self.cancelled = False

        def start(self):
            def fire():
                if not self.cancelled:
                    current[0] = self
                    self.target()
                    current[0] = None

            pending.append(fire)

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(
        c,
        "threading",
        SimpleNamespace(Thread=Thread, Timer=Timer, current_thread=lambda: current[0]),
    )
    return pending


def test_pause_can_resume_after_route_disappears(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    with c.mode.claim(MEDIA):
        generation = c.mode.snapshot()[2]
        c._bounded_control = AsyncMock(return_value=(ADDRESS, PLAYER, generation))
        assert c.handle_button()
    pending.pop()()  # phone removes route before Pause reply reaches us
    assert c._paused == (ADDRESS, PLAYER, generation)
    assert c.handle_button()
    c._bounded_control.return_value = None
    pending.pop()()
    assert c._paused is None
    assert not c.handle_button()  # ordinary Billy button again


def test_new_call_invalidates_paused_media_and_selects_hangup(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    c._paused = (ADDRESS, PLAYER, 0)
    with c.mode.claim(PHONE_CALL):
        c._bounded_control = AsyncMock(return_value=None)
        assert c.handle_button(ai_active=True)  # takeover still stopping AI
        pending.pop()()
        assert c._bounded_control.await_args.args[0][1] == PHONE_CALL
    assert c._paused is None


def test_busy_press_does_not_queue_or_start_ai(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    with c.mode.claim(PHONE_CALL):
        assert c.handle_button()
        assert c.handle_button()
        assert len(pending) == 1
    # Calls remain immediate and extra presses do not queue commands.
    assert c.handle_button()
    assert len(pending) == 1


def test_ai_button_retains_its_existing_meaning(controls):
    c = controls
    c._paused = (ADDRESS, PLAYER, 0)
    assert not c.handle_button(ai_active=True)
    assert c._paused is None


def test_new_ai_session_cancels_pending_bluetooth_command(controls):
    c = controls
    snapshot = c.mode.snapshot()
    epoch = c._epoch
    assert c._current(snapshot, epoch)
    c.clear_resume()
    assert not c._current(snapshot, epoch)


def test_running_promotion_valid_but_new_takeover_is_stale(controls):
    c = controls
    with c.mode.claim(MEDIA) as running:
        snapshot = c.mode.snapshot()
        running()
        assert c._current(snapshot, c._epoch)
    with c.mode.claim(MEDIA):
        assert not c._current(snapshot, c._epoch)
    assert c.mode.snapshot()[1] is None


def test_worker_timeout_releases_busy_and_allows_retry(controls, monkeypatch):
    c = controls
    monkeypatch.setattr(c, "CONTROL_TIMEOUT", 0.01)
    cancelled = []

    async def hang(*args):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(c, "_control", hang)
    with c.mode.claim(PHONE_CALL):
        assert c.handle_button()
        deadline = time.monotonic() + 2
        while c._busy and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not c._busy
    assert cancelled == [True]
    c.logger.warning.assert_called_once()


@pytest.mark.parametrize("handled", [True, False])
def test_physical_button_debounce_and_ai_dispatch(controls, handled):
    dispatch = Mock(return_value=handled)
    trigger = Mock()
    ns = dict(
        time=time,
        threading=threading,
        last_button_time=0,
        _button_was_pressed=False,
        _button_released_since=0,
        _button_rearm_required=False,
        _button_state_lock=threading.Lock(),
        button_debounce_delay=0.12,
        is_active=False,
        bluetooth_controls=SimpleNamespace(handle_button=dispatch),
        trigger_session_start=trigger,
        logger=Mock(),
    )
    button = definitions("application/core/button.py", ["on_button"], ns)
    button.on_button()
    button.on_button(source="poll")
    dispatch.assert_called_once_with(ai_active=False)
    assert trigger.call_count == (0 if handled else 1)


def test_invalid_node_address_fails_closed(controls):
    assert controls._address(MEDIA) == ADDRESS
    with pytest.raises(RuntimeError):
        controls._address(("unrelated_node", None))


@pytest.mark.parametrize("route", [MEDIA, PHONE_CALL])
def test_real_dispatch_uses_correct_bus_and_closes_connection(
    controls, monkeypatch, route
):
    import dbus_next.aio
    from dbus_next import BusType

    c = controls
    bus = SimpleNamespace(connect=AsyncMock(), disconnect=Mock())
    factory = Mock(return_value=bus)
    monkeypatch.setattr(dbus_next.aio, "MessageBus", factory)
    c._hangup = AsyncMock()
    c._toggle_media = AsyncMock(return_value=("pause", PLAYER))
    with c.mode.claim(route):
        snapshot = c.mode.snapshot()
        result = asyncio.run(c._control(snapshot, None, c._epoch))
    if route[1]:
        factory.assert_called_once_with(
            bus_address=c.pipewire_env()["DBUS_SESSION_BUS_ADDRESS"]
        )
        c._hangup.assert_awaited_once()
        c._toggle_media.assert_not_awaited()
        assert result is None
    else:
        factory.assert_called_once_with(bus_type=BusType.SYSTEM)
        c._toggle_media.assert_awaited_once()
        c._hangup.assert_not_awaited()
        assert result == (ADDRESS, PLAYER, snapshot[2])
    bus.disconnect.assert_called_once()


def test_timeout_disconnects_bus_even_during_connect(controls, monkeypatch):
    import dbus_next.aio

    c = controls

    async def connect():
        await asyncio.sleep(10)

    bus = SimpleNamespace(connect=connect, disconnect=Mock())
    monkeypatch.setattr(dbus_next.aio, "MessageBus", Mock(return_value=bus))
    c.CONTROL_TIMEOUT = 0.01
    with c.mode.claim(PHONE_CALL), pytest.raises(TimeoutError):
        asyncio.run(c._bounded_control(c.mode.snapshot(), None, c._epoch))
    bus.disconnect.assert_called_once()


@pytest.mark.parametrize("source", ["wake-word", "mqtt", "button", "button-double"])
def test_nonphysical_triggers_do_not_control_bluetooth(controls, source):
    controls.clear_resume = Mock()
    ns = dict(
        bluetooth_mode=controls.mode,
        bluetooth_controls=controls,
        logger=Mock(),
        is_active=False,
        _handle_active_session_button_press=Mock(),
    )
    button = definitions(
        "application/core/button.py",
        ["trigger_session_start"],
        {**ns, "_shutdown_requested": threading.Event()},
    )
    with controls.mode.claim(PHONE_CALL):
        assert button.trigger_session_start(source) is False
    controls.clear_resume.assert_not_called()
    ns["_handle_active_session_button_press"].assert_not_called()


@pytest.mark.parametrize(
    "source,kwargs",
    [
        ("button", {}),
        ("button-double", {"force_stop": True}),
        ("wake-word", None),
    ],
)
def test_realtime_active_session_dispatch_is_unchanged(controls, source, kwargs):
    ns = dict(
        bluetooth_mode=controls.mode,
        bluetooth_controls=controls,
        logger=Mock(),
        is_active=True,
        _handle_active_session_button_press=Mock(),
    )
    button = definitions(
        "application/core/button.py",
        ["trigger_session_start"],
        {**ns, "_shutdown_requested": threading.Event()},
    )
    assert button.trigger_session_start(source) is False
    if kwargs is None:
        ns["_handle_active_session_button_press"].assert_not_called()
    else:
        ns["_handle_active_session_button_press"].assert_called_once_with(**kwargs)


def connect_real_watcher(c, monkeypatch):
    import importlib
    import sys

    from test_watcher import load_watcher

    watcher = load_watcher()
    watcher['mode'] = c.mode
    module = SimpleNamespace(exit_media=watcher['exit_media'])
    monkeypatch.setitem(sys.modules, c.__package__ + '.watcher', module)
    monkeypatch.setattr(
        importlib.import_module(c.__package__), 'watcher', module, raising=False
    )
    return watcher


def test_single_media_press_waits_for_doubletap_window(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    c._bounded_control = AsyncMock(return_value=None)
    with c.mode.claim(MEDIA):
        assert c.handle_button()
        c._bounded_control.assert_not_awaited()
        assert len(pending) == 1
        pending.pop()()
        c._bounded_control.assert_awaited_once()
        assert not c._busy


def test_doubletap_pauses_and_releases_media_for_next_ai_press(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    watcher = connect_real_watcher(c, monkeypatch)
    stop = watcher['_session_stop'] = threading.Event()
    c._bounded_control = AsyncMock()
    c._pause_for_exit = AsyncMock()
    with c.mode.claim(MEDIA):
        assert c.handle_button()
        assert c.handle_button()
        for task in pending:
            task()
        c._bounded_control.assert_not_awaited()
        c._pause_for_exit.assert_awaited_once()
        assert stop.is_set()
        assert c.mode.active(), 'Only watcher cleanup may release hardware ownership'
        assert c._paused is None
        assert not c.mode.media_allowed(MEDIA)
    assert not c.handle_button(), 'Next physical press must follow the normal AI flow'
    with pytest.raises(c.mode.MediaDismissed), c.mode.claim(MEDIA):
        pass
    c.mode.observe_route(None)
    with c.mode.claim(MEDIA):
        assert c.mode.active(), (
            'A new phone stream can take over after the old one disappeared'
        )


def test_doubletap_exits_even_if_phone_cannot_pause(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    watcher = connect_real_watcher(c, monkeypatch)
    stop = watcher['_session_stop'] = threading.Event()
    c._pause_for_exit = AsyncMock(side_effect=RuntimeError('AVRCP unavailable'))
    with c.mode.claim(MEDIA):
        c.handle_button()
        c.handle_button()
        for task in pending:
            task()
        assert stop.is_set()
    assert not c.handle_button()


def test_doubletap_while_paused_clears_resume_without_playing(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    connect_real_watcher(c, monkeypatch)
    c._paused = (ADDRESS, PLAYER, 0)
    c._pause_for_exit = AsyncMock()
    c._bounded_control = AsyncMock()
    c.handle_button()
    c.handle_button()
    for task in pending:
        task()
    assert c._paused is None
    assert not c.handle_button()
    c._bounded_control.assert_not_awaited()
    c._pause_for_exit.assert_awaited_once()


@pytest.mark.parametrize('status', ['playing', 'paused', 'stopped'])
def test_exit_media_never_resumes_a_paused_phone(controls, monkeypatch, status):
    c = controls
    player, _ = setup_media(c, monkeypatch, status)
    asyncio.run(c._toggle_media(None, ADDRESS, None, lambda: True, pause_only=True))
    player.call_play.assert_not_awaited()
    assert player.call_pause.await_count == (1 if status == 'playing' else 0)


def test_phone_call_arriving_between_taps_still_hangs_up(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    c._bounded_control = AsyncMock(return_value=None)
    c._exit_worker = Mock()
    with c.mode.claim(MEDIA):
        c.handle_button()
    with c.mode.claim(PHONE_CALL):
        c.handle_button()
        for task in pending:
            task()
        c._exit_worker.assert_not_called()
        c._bounded_control.assert_awaited_once()
        assert c._bounded_control.await_args.args[0][1] == PHONE_CALL


def test_delayed_exit_cannot_stop_a_new_phone_call(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    watcher = connect_real_watcher(c, monkeypatch)
    stop = watcher['_session_stop'] = threading.Event()
    c._pause_for_exit = AsyncMock()
    with c.mode.claim(MEDIA):
        c.handle_button()
        c.handle_button()
    with c.mode.claim(PHONE_CALL):
        for task in pending:
            task()
        assert not stop.is_set()
        assert c.mode.media_allowed(PHONE_CALL)


def test_ai_start_cancels_pending_single_press(controls, monkeypatch):
    c = controls
    pending = capture_worker(c, monkeypatch)
    c._paused = (ADDRESS, PLAYER, 0)
    c._bounded_control = AsyncMock()
    c.handle_button()
    c.clear_resume()
    for task in pending:
        task()
    c._bounded_control.assert_not_awaited()
    assert not c._busy
    assert not c.handle_button(ai_active=True)


def test_failed_route_discovery_does_not_rearm_dismissed_media(controls):
    from test_watcher import load_watcher

    c = controls
    watcher = load_watcher()
    watcher['mode'] = c.mode
    with c.mode.claim(MEDIA):
        assert c.mode.dismiss_media(c.mode.snapshot(), ADDRESS)
    watcher['discover'] = Mock(side_effect=RuntimeError('pw-dump timed out'))
    assert watcher['active_route']() is None
    assert not c.mode.media_allowed(MEDIA)
    watcher['discover'] = lambda: []
    watcher['select_route'] = lambda nodes: None
    assert watcher['active_route']() is None
    assert c.mode.media_allowed(MEDIA)


def test_dismissed_media_does_not_block_phone_calls(controls):
    c = controls
    with c.mode.claim(MEDIA):
        assert c.mode.dismiss_media(c.mode.snapshot(), ADDRESS)
    assert c.mode.observe_route(PHONE_CALL)
    with c.mode.claim(PHONE_CALL):
        assert c.mode.active()


def test_real_timer_runs_single_press_once(controls, monkeypatch):
    c = controls
    monkeypatch.setattr(c, 'DOUBLE_TAP_SECONDS', 0.02)
    c._bounded_control = AsyncMock(return_value=None)
    with c.mode.claim(MEDIA):
        assert c.handle_button()
        deadline = time.monotonic() + 2
        while c._busy and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not c._busy
        c._bounded_control.assert_awaited_once()


def test_exit_timeout_still_releases_media(controls, monkeypatch):
    c = controls
    watcher = connect_real_watcher(c, monkeypatch)
    stop = watcher['_session_stop'] = threading.Event()
    monkeypatch.setattr(c, 'CONTROL_TIMEOUT', 0.01)

    async def hang(*args):
        await asyncio.sleep(10)

    c._pause_for_exit = hang
    with c.mode.claim(MEDIA):
        c._busy = True
        c._exit_worker(c.mode.snapshot(), None, c._epoch)
        assert stop.is_set()
        assert not c._busy
    assert not c.handle_button()
