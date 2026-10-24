"""Reconnect must establish media too, without disturbing calls or streams."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_next import Variant
from dbus_next.errors import DBusError


spec = importlib.util.spec_from_file_location(
    'connect_bluez', Path(__file__).parents[2] / 'application/core/bluetooth/bluez.py'
)
bluez = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bluez)
PHONE = '/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF'
ADDRESS = 'AA:BB:CC:DD:EE:FF'


@pytest.fixture(autouse=True)
def fast_connect_clock(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bluez.bluetooth_preferences, "PATH", tmp_path / "bluetooth.json"
    )
    clock = [0.0]
    sleep = asyncio.sleep

    async def advance(seconds):
        clock[0] += seconds
        await sleep(0)

    monkeypatch.setattr(bluez, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(bluez.asyncio, 'sleep', advance)


def fixture(*, paired=True, connected=True, media=True, transport=False):
    manager = bluez.BluezController.__new__(bluez.BluezController)
    manager.device_lock = asyncio.Lock()
    manager.pending = None
    manager.deadline = 0
    objects = {
        PHONE: {
            'org.bluez.Device1': {
                'Address': Variant('s', ADDRESS),
                'Paired': Variant('b', paired),
                'Connected': Variant('b', connected),
                'UUIDs': Variant('as', [bluez.A2DP_SOURCE] if media else []),
            }
        }
    }
    if transport:
        objects[PHONE + '/sep/fd0'] = {
            'org.bluez.MediaTransport1': {
                'Device': Variant('o', PHONE),
                'UUID': Variant('s', bluez.A2DP_SINK),
                'State': Variant('s', 'idle'),
            }
        }
    manager.objects = AsyncMock(return_value=objects)
    device = AsyncMock()

    async def establish_media(*_):
        objects[PHONE + '/sep/fd0'] = {
            'org.bluez.MediaTransport1': {
                'Device': Variant('o', PHONE),
                'UUID': Variant('s', bluez.A2DP_SINK),
                'State': Variant('s', 'idle'),
            }
        }

    async def disconnect():
        objects[PHONE]['org.bluez.Device1']['Connected'] = Variant('b', False)

    device.call_disconnect.side_effect = disconnect
    device.call_connect_profile.side_effect = establish_media
    manager.interface = AsyncMock(return_value=device)
    return manager, device, objects


def connect(manager):
    return asyncio.run(manager.device_action(ADDRESS, 'connect'))


def test_partial_generic_connection_explicitly_connects_remote_audio_source():
    manager, device, _ = fixture()
    assert connect(manager) == {'ok': True}
    device.call_connect.assert_awaited_once()
    device.call_connect_profile.assert_awaited_once_with(bluez.A2DP_SOURCE)
    device.call_disconnect.assert_not_awaited()


def test_existing_idle_media_transport_is_not_reconnected():
    manager, device, _ = fixture(transport=True)
    connect(manager)
    device.call_connect_profile.assert_not_awaited()


def test_already_connected_can_still_need_media():
    manager, device, _ = fixture()
    device.call_connect.side_effect = DBusError(
        'org.bluez.Error.AlreadyConnected', 'connected'
    )
    connect(manager)
    device.call_connect_profile.assert_awaited_once()


def test_already_connected_media_race_is_success():
    manager, device, _ = fixture()
    establish = device.call_connect_profile.side_effect

    async def already_connected(*args):
        await establish(*args)
        raise DBusError('org.bluez.Error.AlreadyConnected', 'connected')

    device.call_connect_profile.side_effect = already_connected
    assert connect(manager) == {'ok': True}


def test_failed_media_connection_is_reported_without_disconnecting_call():
    manager, device, _ = fixture()
    device.call_connect_profile.side_effect = DBusError(
        'org.bluez.Error.NotAvailable', 'no endpoint'
    )
    with pytest.raises(RuntimeError, match='Media audio could not connect'):
        connect(manager)
    device.call_disconnect.assert_not_awaited()


def test_non_media_device_does_not_request_unsupported_profile():
    manager, device, _ = fixture(media=False)
    connect(manager)
    device.call_connect_profile.assert_not_awaited()


def test_unpaired_device_requires_pairing():
    manager, device, _ = fixture(paired=False)
    with pytest.raises(ValueError, match='Pair this device'):
        connect(manager)
    manager.interface.assert_not_awaited()


@pytest.mark.parametrize(
    'action,method',
    [('disconnect', 'call_disconnect'), ('forget', 'call_remove_device')],
)
def test_disconnect_and_forget_do_not_reconnect(action, method):
    manager, device, _ = fixture()
    asyncio.run(manager.device_action(ADDRESS, action))
    getattr(device, method).assert_awaited_once()
    device.call_connect.assert_not_awaited()
    device.call_connect_profile.assert_not_awaited()


@pytest.mark.parametrize(
    'transport,connected,expected',
    [(False, True, False), (True, True, True), (True, False, False)],
)
def test_status_distinguishes_link_from_idle_media(transport, connected, expected):
    manager, _, _ = fixture(transport=transport, connected=connected)
    state = asyncio.run(manager.status())
    assert state['devices'][0]['MediaSupported'] is True
    assert state['devices'][0]['MediaConnected'] is expected


def test_other_phone_transport_does_not_count():
    manager, device, objects = fixture(transport=True)
    objects[PHONE + '/sep/fd0']['org.bluez.MediaTransport1']['Device'] = Variant(
        'o', '/other'
    )
    connect(manager)
    device.call_connect_profile.assert_awaited_once()


def test_call_transport_does_not_count_as_media():
    manager, device, objects = fixture(transport=True)
    objects[PHONE + '/sep/fd0']['org.bluez.MediaTransport1']['UUID'] = Variant(
        's', '0000111e-0000-1000-8000-00805f9b34fb'
    )
    connect(manager)
    device.call_connect_profile.assert_awaited_once()


@pytest.mark.parametrize('error', sorted(bluez.MEDIA_RETRY_ERRORS))
def test_one_click_from_disconnected_waits_for_discovery_and_retries(error):
    manager, device, objects = fixture(connected=False)
    props = objects[PHONE]['org.bluez.Device1']
    props['ServicesResolved'] = Variant('b', False)
    props['UUIDs'] = Variant('as', [])
    establish = device.call_connect_profile.side_effect

    async def generic_connect():
        props['Connected'] = Variant('b', True)

    async def discovery():
        if bluez.time.monotonic() >= 0.5:
            props['ServicesResolved'] = Variant('b', True)
            props['UUIDs'] = Variant('as', [bluez.A2DP_SOURCE])
        return objects

    async def media_connect(*args):
        assert props['ServicesResolved'].value
        if device.call_connect_profile.await_count == 1:
            raise DBusError(error, 'endpoint still starting')
        await establish(*args)

    manager.objects.side_effect = discovery
    device.call_connect.side_effect = generic_connect
    device.call_connect_profile.side_effect = media_connect
    assert connect(manager) == {'ok': True}
    device.call_connect.assert_awaited_once()
    assert device.call_connect_profile.await_count == 2
    device.call_disconnect.assert_not_awaited()


@pytest.mark.parametrize('already_connected', [False, True])
def test_profile_reply_waits_for_actual_transport(already_connected):
    manager, device, objects = fixture()
    establish = device.call_connect_profile.side_effect

    async def pending_profile(*_):
        if already_connected:
            raise DBusError('org.bluez.Error.AlreadyConnected', 'connecting')

    async def delayed_transport():
        if bluez.time.monotonic() >= 1:
            await establish()
        return objects

    device.call_connect_profile.side_effect = pending_profile
    manager.objects.side_effect = delayed_transport
    assert connect(manager) == {'ok': True}
    assert bluez.time.monotonic() >= 1
    device.call_connect_profile.assert_awaited_once()


def test_generic_connect_in_progress_waits_for_link():
    manager, device, objects = fixture(connected=False)
    device.call_connect.side_effect = DBusError(
        'org.bluez.Error.InProgress', 'connecting'
    )

    async def delayed_link():
        if bluez.time.monotonic() >= 0.5:
            objects[PHONE]['org.bluez.Device1']['Connected'] = Variant('b', True)
        return objects

    manager.objects.side_effect = delayed_link
    assert connect(manager) == {'ok': True}
    device.call_connect.assert_awaited_once()
    device.call_connect_profile.assert_awaited_once()


def test_persistent_failure_has_bounded_backoff():
    manager, device, _ = fixture()
    attempts = []

    async def unavailable(*_):
        attempts.append(bluez.time.monotonic())
        raise DBusError('org.bluez.Error.NotAvailable', 'unavailable')

    device.call_connect_profile.side_effect = unavailable
    with pytest.raises(RuntimeError, match='could not connect in time'):
        connect(manager)
    assert attempts == [0, 1, 3, 7]
    assert bluez.time.monotonic() == 10
    device.call_disconnect.assert_not_awaited()


@pytest.mark.parametrize('error', ['AuthenticationRejected', 'InvalidArguments'])
def test_permanent_profile_error_is_not_retried(error):
    manager, device, _ = fixture()
    device.call_connect_profile.side_effect = DBusError(
        'org.bluez.Error.' + error, 'rejected'
    )
    with pytest.raises(RuntimeError, match='Check Bluetooth'):
        connect(manager)
    device.call_connect_profile.assert_awaited_once()


@pytest.mark.parametrize('change', ['disconnect', 'remove', 'unpair'])
def test_device_loss_during_retry_aborts_without_reconnecting(change):
    manager, device, objects = fixture()

    async def device_lost(*_):
        props = objects[PHONE]['org.bluez.Device1']
        if change == 'remove':
            del objects[PHONE]
        elif change == 'unpair':
            props['Paired'] = Variant('b', False)
        else:
            props['Connected'] = Variant('b', False)
        raise DBusError('org.bluez.Error.Failed', 'link lost')

    device.call_connect_profile.side_effect = device_lost
    with pytest.raises(RuntimeError, match='disconnected|no longer paired'):
        connect(manager)
    device.call_connect.assert_awaited_once()
    device.call_connect_profile.assert_awaited_once()
    device.call_disconnect.assert_not_awaited()


def test_unresolved_services_timeout_without_requesting_profile():
    manager, device, objects = fixture()
    objects[PHONE]['org.bluez.Device1']['ServicesResolved'] = Variant('b', False)
    with pytest.raises(RuntimeError, match='could not connect in time'):
        connect(manager)
    device.call_connect_profile.assert_not_awaited()


@pytest.mark.parametrize('method', ['call_connect', 'call_connect_profile'])
def test_hung_dbus_call_is_cancelled_within_action_timeout(monkeypatch, method):
    manager, device, _ = fixture()
    cancelled = []

    async def hang(*_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    getattr(device, method).side_effect = hang
    monkeypatch.setattr(bluez, 'CONNECT_TIMEOUT_SECONDS', 0.02)
    with pytest.raises(RuntimeError, match='could not connect in time'):
        connect(manager)
    assert cancelled == [True]
    device.call_disconnect.assert_not_awaited()


def test_auto_connect_preference_only_for_paired_devices():
    manager, device, _ = fixture()
    asyncio.run(manager.set_auto_connect(ADDRESS, True))
    assert asyncio.run(manager.status())['devices'][0]['AutoConnect'] is True
    device.call_connect.assert_not_awaited()
    asyncio.run(manager.device_action(ADDRESS, 'forget'))
    assert bluez.bluetooth_preferences.selected() == []
    unpaired, _, _ = fixture(paired=False)
    with pytest.raises(ValueError, match='Pair this device'):
        asyncio.run(unpaired.set_auto_connect(ADDRESS, True))
    with pytest.raises(ValueError, match='Device not found'):
        asyncio.run(manager.set_auto_connect('00:00:00:00:00:00', True))


@pytest.mark.parametrize('briefly_disconnect', [False, True])
def test_one_disconnect_action_retries_a_lingering_or_racing_link(briefly_disconnect):
    manager, device, objects = fixture(transport=True)
    props = objects[PHONE]['org.bluez.Device1']

    async def disconnect():
        if device.call_disconnect.await_count > 1 or briefly_disconnect:
            props['Connected'] = Variant('b', False)

    async def snapshot():
        if (
            briefly_disconnect
            and device.call_disconnect.await_count == 1
            and bluez.time.monotonic() >= 0.2
        ):
            props['Connected'] = Variant('b', True)
        return objects

    device.call_disconnect.side_effect = disconnect
    manager.objects.side_effect = snapshot
    assert asyncio.run(manager.device_action(ADDRESS, 'disconnect')) == {'ok': True}
    assert device.call_disconnect.await_count == 2
    assert not props['Connected'].value
    device.call_connect.assert_not_awaited()
    device.call_connect_profile.assert_not_awaited()


def test_disconnect_does_not_claim_success_when_only_media_disappears():
    manager, device, _ = fixture(transport=False)
    device.call_disconnect.side_effect = None
    with pytest.raises(RuntimeError, match='still connected'):
        asyncio.run(manager.device_action(ADDRESS, 'disconnect'))
    assert device.call_disconnect.await_count == 3


def test_already_disconnected_is_success():
    manager, device, _ = fixture(connected=False)
    device.call_disconnect.side_effect = DBusError(
        'org.bluez.Error.NotConnected', 'disconnected'
    )
    assert asyncio.run(manager.device_action(ADDRESS, 'disconnect')) == {'ok': True}
    device.call_disconnect.assert_awaited_once()


def test_hung_disconnect_is_cancelled(monkeypatch):
    manager, device, _ = fixture()
    cancelled = []

    async def hang():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    device.call_disconnect.side_effect = hang
    wait_for = asyncio.wait_for

    async def short_wait(coro, timeout):
        return await wait_for(coro, timeout=0.01)

    monkeypatch.setattr(bluez.asyncio, 'wait_for', short_wait)
    with pytest.raises(RuntimeError, match='still connected'):
        asyncio.run(manager.device_action(ADDRESS, 'disconnect'))
    assert cancelled == [True]


OTHER = '/org/bluez/hci0/dev_11_22_33_44_55_66'


def switch_fixture():
    manager, selected, objects = fixture(connected=False, media=False)
    objects[OTHER] = {
        'org.bluez.Device1': {
            'Address': Variant('s', '11:22:33:44:55:66'),
            'Paired': Variant('b', True),
            'Connected': Variant('b', True),
        }
    }
    old = AsyncMock()
    events = []

    async def disconnect():
        events.append('disconnect')
        objects[OTHER]['org.bluez.Device1']['Connected'] = Variant('b', False)

    async def connect_new():
        assert not objects[OTHER]['org.bluez.Device1']['Connected'].value
        events.append('connect')
        objects[PHONE]['org.bluez.Device1']['Connected'] = Variant('b', True)

    old.call_disconnect.side_effect = disconnect
    selected.call_connect.side_effect = connect_new
    manager.interface = AsyncMock(
        side_effect=lambda path, _: old if path == OTHER else selected
    )
    return manager, selected, old, objects, events


def test_select_disconnects_old_phone_before_connecting_new_one():
    manager, selected, old, objects, events = switch_fixture()
    assert asyncio.run(manager.device_action(ADDRESS, 'select')) == {'ok': True}
    assert events == ['disconnect', 'connect']
    old.call_disconnect.assert_awaited_once()
    assert objects[PHONE]['org.bluez.Device1']['Connected'].value


@pytest.mark.parametrize('failure', ['timeout', 'dbus', 'reconnected'])
def test_select_does_not_connect_when_teardown_fails(failure):
    manager, selected, old, objects, _ = switch_fixture()
    if failure == 'timeout':
        old.call_disconnect.side_effect = None
    elif failure == 'dbus':
        old.call_disconnect.side_effect = DBusError('org.bluez.Error.Failed', 'failed')
    else:
        # A different phone returned after disconnect_audio completed.
        manager.disconnect_audio = AsyncMock()
    with pytest.raises(Exception):
        asyncio.run(manager.device_action(ADDRESS, 'select'))
    selected.call_connect.assert_not_awaited()
    assert not manager.device_lock.locked()


@pytest.mark.parametrize('invalid', ['missing', 'unpaired'])
def test_select_validates_target_before_disconnecting(invalid):
    manager, selected, old, objects, _ = switch_fixture()
    if invalid == 'missing':
        del objects[PHONE]
    else:
        objects[PHONE]['org.bluez.Device1']['Paired'] = Variant('b', False)
    with pytest.raises(ValueError):
        asyncio.run(manager.device_action(ADDRESS, 'select'))
    old.call_disconnect.assert_not_awaited()
    selected.call_connect.assert_not_awaited()


def test_select_failure_leaves_actual_disconnected_state_and_releases_lock():
    manager, selected, old, objects, _ = switch_fixture()
    selected.call_connect.side_effect = DBusError(
        'org.bluez.Error.Failed', 'unreachable'
    )
    with pytest.raises(DBusError):
        asyncio.run(manager.device_action(ADDRESS, 'select'))
    assert not objects[OTHER]['org.bluez.Device1']['Connected'].value
    assert not objects[PHONE]['org.bluez.Device1']['Connected'].value
    assert not manager.device_lock.locked()


def test_select_rejects_concurrent_device_action_and_releases_lock_on_cancellation():
    async def scenario():
        manager, selected, old, objects, _ = switch_fixture()
        entered = asyncio.Event()

        async def wait():
            entered.set()
            await asyncio.Event().wait()

        old.call_disconnect.side_effect = wait
        task = asyncio.create_task(manager.device_action(ADDRESS, 'select'))
        await entered.wait()
        with pytest.raises(RuntimeError, match='busy'):
            await manager.device_action(ADDRESS, 'forget')
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager.device_lock.locked()
        selected.call_connect.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize('connected', [False, True])
def test_le_abort_uses_known_audio_profile_and_waits_for_media(connected, caplog):
    manager, device, objects = fixture(connected=connected)
    props = objects[PHONE]['org.bluez.Device1']
    props['ServicesResolved'] = Variant('b', False)
    device.call_connect.side_effect = DBusError(
        'org.bluez.Error.Failed', 'le-connection-abort-by-local'
    )
    establish = device.call_connect_profile.side_effect
    device.call_connect_profile.side_effect = None

    async def snapshot():
        if device.call_connect_profile.await_count and bluez.time.monotonic() >= 0.5:
            props['Connected'] = Variant('b', True)
            await establish()
        return objects

    manager.objects.side_effect = snapshot
    with caplog.at_level('INFO'):
        assert connect(manager) == {'ok': True}
    assert 'le-connection-abort-by-local' in caplog.text
    assert bluez.time.monotonic() >= 0.5
    device.call_connect.assert_awaited_once()
    device.call_connect_profile.assert_awaited_once_with(bluez.A2DP_SOURCE)
    device.call_disconnect.assert_not_awaited()


def test_le_abort_without_known_audio_profile_has_actionable_message():
    manager, device, _ = fixture(connected=False, media=False)
    error = DBusError('org.bluez.Error.Failed', 'le-connection-abort-by-local')
    device.call_connect.side_effect = error
    with pytest.raises(RuntimeError, match='Make sure Bluetooth is on') as failure:
        connect(manager)
    assert failure.value.__cause__ is error
    assert 'le-connection-abort-by-local' not in str(failure.value)
    device.call_connect_profile.assert_not_awaited()
    device.call_disconnect.assert_not_awaited()


def test_le_audio_fallback_failure_is_bounded_without_restarting_generic_connect():
    manager, device, _ = fixture(connected=False)
    device.call_connect.side_effect = DBusError(
        'org.bluez.Error.Failed', 'le-connection-abort-by-local'
    )
    attempts = []

    async def unavailable(*_):
        attempts.append(bluez.time.monotonic())
        raise DBusError('org.bluez.Error.NotAvailable', 'unavailable')

    device.call_connect_profile.side_effect = unavailable
    with pytest.raises(RuntimeError, match='could not connect in time'):
        connect(manager)
    assert attempts == [0, 1, 3, 7]
    device.call_connect.assert_awaited_once()
    device.call_disconnect.assert_not_awaited()
    assert not manager.device_lock.locked()


@pytest.mark.parametrize('change', ['remove', 'unpair'])
def test_le_fallback_rechecks_pairing_before_connecting_profile(change):
    manager, device, objects = fixture(connected=False)

    async def abort():
        if change == 'remove':
            del objects[PHONE]
        else:
            objects[PHONE]['org.bluez.Device1']['Paired'] = Variant('b', False)
        raise DBusError('org.bluez.Error.Failed', 'le-connection-abort-by-local')

    device.call_connect.side_effect = abort
    with pytest.raises(RuntimeError, match='no longer paired'):
        connect(manager)
    device.call_connect_profile.assert_not_awaited()


@pytest.mark.parametrize(
    'kind,text',
    [
        ('AuthenticationRejected', 'le-connection-abort-by-local'),
        ('Failed', 'br-connection-key-missing'),
        ('Failed', 'le-connection-abort-by-remote'),
    ],
)
def test_other_connection_errors_do_not_trigger_audio_fallback(kind, text):
    manager, device, _ = fixture(connected=False)
    device.call_connect.side_effect = DBusError('org.bluez.Error.' + kind, text)
    with pytest.raises(DBusError):
        connect(manager)
    device.call_connect_profile.assert_not_awaited()
    device.call_disconnect.assert_not_awaited()


@pytest.mark.parametrize('state', ['link-only', 'transport-only'])
def test_le_profile_reply_does_not_report_success_without_link_and_media(state):
    manager, device, objects = fixture(connected=False)
    device.call_connect.side_effect = DBusError(
        'org.bluez.Error.Failed', 'le-connection-abort-by-local'
    )
    establish = device.call_connect_profile.side_effect

    async def partial(*_):
        if state == 'transport-only':
            await establish()
        else:
            objects[PHONE]['org.bluez.Device1']['Connected'] = Variant('b', True)

    device.call_connect_profile.side_effect = partial
    with pytest.raises(RuntimeError, match='could not connect in time'):
        connect(manager)
    device.call_connect_profile.assert_awaited_once()
    device.call_disconnect.assert_not_awaited()
