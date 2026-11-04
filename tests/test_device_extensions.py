"""Extension-device hooks: session events, outside start/stop and loading.

Hardware-free. Session code is exercised through the real button.py helpers
with fake sessions; nothing opens audio, GPIO or a provider connection.
"""

import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions


# --- core.session_events -------------------------------------------------


def test_events_reach_subscribers_until_they_unsubscribe(isolated):
    events = isolated.load('session_events')
    seen = []
    unsubscribe = events.subscribe('session_ended', lambda **data: seen.append(data))

    events.emit('session_ended', session_id=1, stopped_by=None)
    unsubscribe()
    unsubscribe()  # harmless twice
    events.emit('session_ended', session_id=2, stopped_by=None)

    assert seen == [{'session_id': 1, 'stopped_by': None}]


def test_failing_listener_never_reaches_the_session_or_other_listeners(isolated):
    events = isolated.load('session_events')
    seen = []

    def broken(**_):
        raise RuntimeError('boom')

    events.subscribe('session_started', broken)
    events.subscribe('session_started', lambda **data: seen.append(data))

    events.emit('session_started', session_id=1, source='handset')

    assert seen == [{'session_id': 1, 'source': 'handset'}]
    isolated.logger.warning.assert_called_once()


# --- button.py lifecycle bookkeeping -------------------------------------


@pytest.fixture
def lifecycle():
    emitted = []
    ns = {
        'threading': threading,
        'logger': Mock(),
        'session_events': SimpleNamespace(
            SESSION_STARTED='session_started',
            SESSION_ENDED='session_ended',
            emit=lambda event, **data: emitted.append((event, data)),
        ),
        '_session_events_lock': threading.Lock(),
        '_session_stop_lock': threading.Lock(),
        '_session_counter': 0,
        '_current_session_id': None,
        '_open_sessions': {},
        '_stop_sources': {},
        'is_active': False,
        'session_thread': None,
        '_set_situation': lambda text: situations.append(text),
        '_set_persona': lambda name: None,
    }
    situations = []
    stops = []

    def stop_active_session():
        stops.append(ns['_current_session_id'])
        ns['is_active'] = False

    ns['_stop_active_session'] = stop_active_session
    module = definitions(
        'application/core/button.py',
        [
            '_open_session_events',
            '_close_session_events',
            '_record_stop_source',
            'request_session_stop',
        ],
        ns,
    )
    return SimpleNamespace(
        module=module, ns=ns, emitted=emitted, stops=stops, situations=situations
    )


def ended(lifecycle):
    return [data for event, data in lifecycle.emitted if event == 'session_ended']


def test_stop_when_idle_does_nothing(lifecycle):
    assert lifecycle.module.request_session_stop('handset') is False
    assert lifecycle.stops == []
    assert lifecycle.emitted == []


def test_outside_stop_is_reported_once_with_its_source(lifecycle):
    m, ns = lifecycle.module, lifecycle.ns
    session_id = m._open_session_events('handset')
    ns['is_active'] = True

    assert m.request_session_stop('handset') is True
    # Hanging up again, or the session thread finishing, adds nothing.
    assert m.request_session_stop('handset') is False
    m._close_session_events(session_id)
    m._close_session_events(session_id)

    assert lifecycle.stops == [session_id]
    assert ended(lifecycle) == [
        {'session_id': session_id, 'source': 'handset', 'stopped_by': 'handset'}
    ]


def test_session_that_ends_by_itself_reports_no_stopper(lifecycle):
    m = lifecycle.module
    session_id = m._open_session_events('handset')

    m._close_session_events(session_id)

    assert ended(lifecycle) == [
        {'session_id': session_id, 'source': 'handset', 'stopped_by': None}
    ]


def test_first_stopper_wins(lifecycle):
    m, ns = lifecycle.module, lifecycle.ns
    session_id = m._open_session_events('button')
    ns['is_active'] = True

    m._record_stop_source('bluetooth')  # a takeover got there first
    m.request_session_stop('handset')
    m._close_session_events(session_id)

    assert ended(lifecycle)[0]['stopped_by'] == 'bluetooth'


def test_late_end_of_a_superseded_session_keeps_the_current_one(lifecycle):
    m, ns = lifecycle.module, lifecycle.ns
    old = m._open_session_events('button')
    new = m._open_session_events('handset')

    m._close_session_events(old)

    assert ns['_current_session_id'] == new
    ns['is_active'] = True
    m.request_session_stop('handset')
    m._close_session_events(new)
    assert [e['session_id'] for e in ended(lifecycle)] == [old, new]
    assert ended(lifecycle)[1]['stopped_by'] == 'handset'


# --- core.devices --------------------------------------------------------


class _Device:
    def __init__(self):
        self.controls = None
        self.stopped = False

    def start(self, controls):
        self.controls = controls

    def stop(self):
        self.stopped = True


@pytest.fixture
def device_module(monkeypatch):
    module = ModuleType('fake_device_module')
    module.Device = _Device
    module.instance = _Device()
    module.not_a_device = object()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def test_fish_loads_no_device(isolated):
    devices = isolated.load('devices')
    assert devices.load_device('fish') is None
    assert devices.start_device(None) is False
    devices.stop_device(None)


def test_import_path_accepts_a_class_or_an_instance(isolated, device_module):
    devices = isolated.load('devices')

    built = devices.load_device('fake_device_module:Device')
    given = devices.load_device('fake_device_module:instance')

    assert isinstance(built, _Device)
    assert given is device_module.instance


@pytest.mark.parametrize(
    'name', ['missing_device', 'fake_device_module:not_a_device', 'nope.mod:X']
)
def test_unloadable_device_is_logged_not_fatal(isolated, device_module, name):
    devices = isolated.load('devices')

    assert devices.load_device(name) is None
    isolated.logger.error.assert_called_once()


def test_plain_name_finds_a_private_device(isolated, monkeypatch):
    package = ModuleType('private.demo')
    package.DEVICE = _Device
    monkeypatch.setitem(sys.modules, 'private', ModuleType('private'))
    monkeypatch.setitem(sys.modules, 'private.demo', package)
    devices = isolated.load('devices')

    assert isinstance(devices.load_device('demo'), _Device)


def test_active_device_gets_controls_and_is_stopped_once(isolated, device_module):
    devices = isolated.load('devices')

    device = devices.load_active_device('fake_device_module:Device')
    assert devices.start_active_device() is True
    devices.stop_active_device()
    devices.stop_active_device()

    assert isinstance(device.controls, devices.DeviceControls)
    assert device.stopped is True


def test_device_failures_are_contained(isolated):
    devices = isolated.load('devices')
    broken = SimpleNamespace(
        start=Mock(side_effect=OSError('no sip')),
        stop=Mock(side_effect=OSError('gone')),
    )

    assert devices.start_device(broken) is False
    devices.stop_device(broken)


# --- config: motors follow the device unless told otherwise -------------


@pytest.mark.parametrize(
    ('device', 'setting', 'expected'),
    [
        ('fish', None, True),
        ('fish', 'false', False),
        ('fake_device_module:Device', None, False),
        ('fake_device_module:Device', 'auto', False),
        ('fake_device_module:Device', 'true', True),
    ],
)
def test_motors_enabled(monkeypatch, device, setting, expected):
    import os

    if setting is None:
        monkeypatch.delenv('MOTORS_ENABLED', raising=False)
    else:
        monkeypatch.setenv('MOTORS_ENABLED', setting)
    ns = {'os': os, 'BILLY_DEVICE': device}
    module = definitions('application/core/config.py', ['_motors_enabled'], ns)
    assert module._motors_enabled() is expected


def test_disabled_motors_never_open_the_gpio_chip(isolated, monkeypatch):
    lgpio = Mock()
    monkeypatch.setitem(sys.modules, 'lgpio', lgpio)
    isolated.config.MOCKFISH = False
    isolated.config.MOTORS_ENABLED = False

    movements = isolated.load('movements')
    movements.move_mouth(80, 0.05)
    movements.stop_all_motors()

    lgpio.gpiochip_open.assert_not_called()
    lgpio.gpio_claim_output.assert_not_called()


# --- opening line for device-started sessions ----------------------------


def opening_options():
    return definitions(
        'application/core/button.py', ['_opening_session_options'], {}
    )._opening_session_options


@pytest.mark.parametrize('opening', [None, {}, {'text': '   '}])
def test_no_opening_means_a_normal_session(opening):
    assert opening_options()(opening) == {}


@pytest.mark.parametrize(
    ('interactive', 'follow_up', 'to_interactive'),
    [(True, 'always', True), (False, 'never', False), (None, 'auto', False)],
)
def test_opening_becomes_a_kickoff(interactive, follow_up, to_interactive):
    options = opening_options()({
        'text': ' Dinner! ',
        'kind': 'literal',
        'interactive': interactive,
    })
    assert options == {
        'kickoff_text': 'Dinner!',
        'kickoff_kind': 'literal',
        'kickoff_to_interactive': to_interactive,
        'autofollowup': follow_up,
    }


def test_prompt_opening_keeps_its_kind():
    options = opening_options()({'text': 'greet them', 'kind': 'prompt'})
    assert options['kickoff_kind'] == 'prompt'


# --- MQTT topics for extension devices -----------------------------------


@pytest.fixture
def mqtt_topics():
    import contextlib
    import json

    client = Mock()
    ns = {
        'contextlib': contextlib,
        'json': json,
        'threading': threading,
        'logger': Mock(),
        '_extra_topics': {},
        '_extra_topics_lock': threading.Lock(),
        'mqtt_client': None,
        'mqtt_connected': False,
        'MQTT_DEVICE_ID': 'billy',
        'mqtt_wire_topic': lambda topic: topic,
    }
    module = definitions(
        'application/core/mqtt.py',
        [
            'register_topic',
            '_publish_extra_discovery',
            '_announce',
            '_dispatch_extra_topic',
            '_discovery_device',
        ],
        ns,
    )
    return SimpleNamespace(module=module, ns=ns, client=client)


def test_topic_registered_before_connecting_is_dispatched(mqtt_topics):
    m, ns = mqtt_topics.module, mqtt_topics.ns
    seen = []
    m.register_topic('billy/x', seen.append)

    assert m._dispatch_extra_topic('billy/x', 'hello') is True
    assert m._dispatch_extra_topic('billy/say', 'hi') is False
    assert seen == ['hello']
    assert ns['_extra_topics']['billy/x'][0] == seen.append


def test_topic_registered_while_connected_subscribes_and_announces(mqtt_topics):
    import json

    m, ns, client = mqtt_topics.module, mqtt_topics.ns, mqtt_topics.client
    ns['mqtt_client'], ns['mqtt_connected'] = client, True

    unregister = m.register_topic(
        'billy/x', Mock(), discovery=('homeassistant/text/billy/x/config', {'a': 1})
    )
    client.subscribe.assert_called_once_with('billy/x')
    topic, payload = client.publish.call_args.args
    assert topic == 'homeassistant/text/billy/x/config'
    assert json.loads(payload)['device']['identifiers'] == ['billy_bass']

    unregister()
    client.unsubscribe.assert_called_once_with('billy/x')
    assert m._dispatch_extra_topic('billy/x', '') is False


def test_failing_topic_handler_is_contained(mqtt_topics):
    m = mqtt_topics.module
    m.register_topic('billy/x', Mock(side_effect=RuntimeError('boom')))
    assert m._dispatch_extra_topic('billy/x', '') is True
    mqtt_topics.ns['logger'].error.assert_called_once()


# --- MQTT identity: fish keeps "billy", other devices use the hostname ---


def mqtt_identity(monkeypatch, device, setting=None, hostname='billyphone'):
    import os
    import re
    import socket

    monkeypatch.setattr(socket, 'gethostname', lambda: hostname)
    if setting is None:
        monkeypatch.delenv('MQTT_DEVICE_ID', raising=False)
    else:
        monkeypatch.setenv('MQTT_DEVICE_ID', setting)
    ns = {'os': os, 're': re, 'BILLY_DEVICE': device}
    module = definitions(
        'application/core/config.py',
        ['_mqtt_device_id', 'mqtt_wire_topic', 'mqtt_canonical_topic'],
        ns,
    )
    ns['MQTT_DEVICE_ID'] = module._mqtt_device_id()
    return SimpleNamespace(id=ns['MQTT_DEVICE_ID'], **vars(module))


@pytest.mark.parametrize(
    ('device', 'setting', 'hostname', 'expected'),
    [
        ('fish', None, 'billyphone', 'billy'),  # existing installs unchanged
        ('fish', 'auto', 'kitchen-fish', 'kitchen-fish'),
        ('x:Phone', None, 'billyphone', 'billyphone'),
        ('x:Phone', 'Billy Phone #2', 'h', 'billy_phone_2'),
        ('x:Phone', 'billy', 'billyphone', 'billy'),
        ('x:Phone', None, '...', 'billy'),
    ],
)
def test_mqtt_device_id(monkeypatch, device, setting, hostname, expected):
    assert mqtt_identity(monkeypatch, device, setting, hostname).id == expected


def test_fish_topics_are_untouched(monkeypatch):
    names = mqtt_identity(monkeypatch, 'fish')
    for topic in ['billy/say', 'billy/state', 'homeassistant/x', 'billyx/y']:
        assert names.mqtt_wire_topic(topic) == topic
        assert names.mqtt_canonical_topic(topic) == topic


def test_device_topics_move_to_the_hostname(monkeypatch):
    names = mqtt_identity(monkeypatch, 'x:Phone')
    assert names.mqtt_wire_topic('billy/say') == 'billyphone/say'
    assert names.mqtt_wire_topic('billy/phone/ring') == 'billyphone/phone/ring'
    assert names.mqtt_wire_topic('homeassistant/text/x') == 'homeassistant/text/x'
    assert names.mqtt_canonical_topic('billyphone/say') == 'billy/say'
    # Another Billy's topics are not ours.
    assert names.mqtt_canonical_topic('billy/say') == 'billy/say'
    assert names.mqtt_canonical_topic('billyphone2/say') == 'billyphone2/say'


@pytest.mark.parametrize('device_id', ['billy', 'billyphone'])
def test_discovery_follows_the_identity(device_id):
    import json

    def wire(topic):
        if device_id != 'billy' and topic.startswith('billy/'):
            return device_id + topic[len('billy') :]
        return topic

    client = Mock()
    import contextlib

    ns = {
        'json': json,
        'contextlib': contextlib,
        'MQTT_DEVICE_ID': device_id,
        'mqtt_wire_topic': wire,
    }
    module = definitions(
        'application/core/mqtt.py', ['_announce', '_discovery_device'], ns
    )
    module._announce(
        client,
        'homeassistant/text/billy/say/config',
        {'unique_id': 'billy_say', 'command_topic': 'billy/say', 'name': 'Billy Say'},
    )
    topic, payload = client.publish.call_args.args
    payload = json.loads(payload)
    if device_id == 'billy':
        assert topic == 'homeassistant/text/billy/say/config'
        assert payload['unique_id'] == 'billy_say'
        assert payload['command_topic'] == 'billy/say'
        assert payload['device']['identifiers'] == ['billy_bass']
    else:
        assert topic == 'homeassistant/text/billyphone/say/config'
        assert payload['unique_id'] == 'billyphone_say'
        assert payload['command_topic'] == 'billyphone/say'
        assert payload['device']['identifiers'] == ['billy_billyphone']
        assert payload['device']['name'] == 'billyphone'


def test_one_topic_can_announce_several_entities(mqtt_topics):
    m, ns, client = mqtt_topics.module, mqtt_topics.ns, mqtt_topics.client
    ns['mqtt_client'], ns['mqtt_connected'] = client, True
    m.register_topic(
        'billy/x',
        Mock(),
        discovery=[
            ('homeassistant/button/billy/x/config', {'a': 1}),
            ('homeassistant/text/billy/x/config', {'b': 2}),
        ],
    )
    topics = [c.args[0] for c in client.publish.call_args_list]
    assert topics == [
        'homeassistant/button/billy/x/config',
        'homeassistant/text/billy/x/config',
    ]


def test_device_model_comes_from_the_active_device(isolated, device_module):
    devices = isolated.load('devices')
    assert devices.active_device_model() is None
    device = devices.load_active_device('fake_device_module:Device')
    device.model = 'Phone'
    assert devices.active_device_model() == 'Phone'
    devices.stop_active_device()
    assert devices.active_device_model() is None


def test_device_can_take_over_say(isolated, device_module):
    devices = isolated.load('devices')
    assert devices.device_handles_say({'text': 'hi'}) is False  # fish: no device

    device = devices.load_active_device('fake_device_module:Device')
    device.handle_say = Mock(return_value=True)
    assert devices.device_handles_say({'text': 'hi'}) is True
    device.handle_say.assert_called_once_with({'text': 'hi'})

    device.handle_say = Mock(side_effect=RuntimeError('boom'))
    assert devices.device_handles_say({'text': 'hi'}) is False
    devices.stop_active_device()


def test_none_discovery_removes_the_entity(mqtt_topics):
    m, ns, client = mqtt_topics.module, mqtt_topics.ns, mqtt_topics.client
    ns['mqtt_client'], ns['mqtt_connected'] = client, True
    m.register_topic(
        'billy/x', Mock(), discovery=[('homeassistant/text/billy/x/config', None)]
    )
    client.publish.assert_called_once_with(
        'homeassistant/text/billy/x/config', '', retain=True
    )


def test_retained_commands_are_not_replayed(mqtt_topics):
    m = mqtt_topics.module
    handler = Mock()
    m.register_topic('billy/x', handler)
    assert m._dispatch_extra_topic('billy/x', 'ring', retained=True) is True
    handler.assert_not_called()
    m._dispatch_extra_topic('billy/x', 'ring')
    handler.assert_called_once_with('ring')


def test_situation_is_cleared_when_the_current_session_ends(lifecycle):
    m = lifecycle.module
    old = m._open_session_events('button')
    new = m._open_session_events('handset')
    m._close_session_events(old)  # superseded: leaves the new one alone
    assert lifecycle.situations == []
    m._close_session_events(new)
    assert lifecycle.situations == [None]


def test_situation_is_added_to_the_instructions(monkeypatch):
    ns = {}
    module = definitions(
        'application/core/session_manager.py', ['set_session_situation'], ns
    )
    module.set_session_situation('  You called the user.  ')
    assert ns['_session_situation'] == 'You called the user.'
    module.set_session_situation('')
    assert ns['_session_situation'] is None


# --- a persona for one conversation --------------------------------------


@pytest.fixture
def session_persona():
    personas = SimpleNamespace(
        current_persona='billy-b', known={'billy-b', 'billy-bro'}
    )

    def switch(name):
        if name not in personas.known:
            return False
        personas.current_persona = name
        return True

    personas.switch_persona = switch
    ns = {'persona_manager': personas, 'logger': Mock()}
    module = definitions(
        'application/core/session_manager.py',
        ['session_persona', 'set_session_persona'],
        ns,
    )
    ns['_session_persona'] = None
    ns['_persona_before_session'] = None
    return SimpleNamespace(module=module, personas=personas)


def test_session_persona_is_used_and_the_users_own_comes_back(session_persona):
    m, personas = session_persona.module, session_persona.personas
    m.set_session_persona('billy-bro')
    assert personas.current_persona == 'billy-bro'
    assert m.session_persona() == 'billy-bro'
    m.set_session_persona(None)
    assert personas.current_persona == 'billy-b'
    assert m.session_persona() is None


def test_unknown_session_persona_keeps_the_users_own(session_persona):
    m, personas = session_persona.module, session_persona.personas
    m.set_session_persona('missing')
    assert personas.current_persona == 'billy-b'
    assert m.session_persona() is None
    m.set_session_persona(None)
    assert personas.current_persona == 'billy-b'


def test_session_persona_is_cleared_with_the_session(lifecycle):
    personas = []
    lifecycle.ns['_set_persona'] = personas.append
    m = lifecycle.module
    session_id = m._open_session_events('handset')
    m._close_session_events(session_id)
    assert personas == [None]


def test_device_offers_a_settings_panel(isolated, device_module):
    devices = isolated.load('devices')
    device_module.Device.webconfig_panel = staticmethod(
        lambda: {'id': 'demo', 'template': 'demo.html'}
    )
    try:
        assert devices.webconfig_panel('fake_device_module:Device') == {
            'id': 'demo',
            'template': 'demo.html',
        }
        assert devices.webconfig_panel('fish') is None
        device_module.Device.webconfig_panel = staticmethod(lambda: {'id': 'x'})
        assert devices.webconfig_panel('fake_device_module:Device') is None
    finally:
        del device_module.Device.webconfig_panel


def test_devices_see_a_bluetooth_takeover():
    from core.bluetooth import mode
    from core.devices import DeviceControls

    controls = DeviceControls()
    assert controls.audio_takeover() is None
    with mode.claim(('bluez_input.AA_BB_CC_DD_EE_FF.1', None)):
        assert controls.audio_takeover() == 'media'
    with mode.claim(('bluez_input.AA_BB_CC_DD_EE_FF.1', 'bluez_output.x')):
        assert controls.audio_takeover() == 'call'
    assert controls.audio_takeover() is None


@pytest.mark.parametrize(
    ('states', 'expected'),
    [
        (None, None),
        ([], None),
        (['incoming'], 'incoming'),
        (['active'], 'active'),
        (['active', 'waiting'], 'incoming'),
        (['disconnected'], None),
    ],
)
def test_devices_see_a_ringing_or_running_bluetooth_call(monkeypatch, states, expected):
    from core.bluetooth import controls
    from core.devices import DeviceControls

    monkeypatch.setattr(controls, 'call_states', lambda timeout=1.0: states)
    assert DeviceControls().bluetooth_call() == expected


@pytest.mark.parametrize(
    ('states', 'ringing'),
    [(None, False), ([], False), (['incoming'], True), (['incoming', 'active'], False)],
)
def test_bluetooth_waits_until_a_ringing_call_is_answered(monkeypatch, states, ringing):
    from core.bluetooth import controls, watcher

    monkeypatch.setattr(controls, 'call_states', lambda timeout=1.0: states)
    assert watcher._only_ringing() is ringing
