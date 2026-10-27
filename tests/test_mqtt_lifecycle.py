"""Deterministic MQTT shutdown races without a broker or hardware."""

import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


class Client:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.phase = 'loop_start'
        self.events = []
        self.loop_started = False
        self.loop_result = 0
        self.disconnect_error = None
        self.username_pw_set = Mock()
        self.reconnect_delay_set = Mock()
        self.publish = Mock()
        self.subscribe = Mock()

    def pause(self, phase):
        if self.phase == phase:
            self.entered.set()
            assert self.release.wait(3), f'Test did not release {phase}'

    def connect_async(self, *args):
        self.events.append('connect')
        self.pause('connect')

    def loop_start(self):
        self.events.append('starting')
        self.pause('loop_start')
        self.loop_started = self.loop_result == 0
        self.events.append('started')
        self.ready.set()
        return self.loop_result

    def disconnect(self):
        self.events.append('disconnect')
        if self.disconnect_error:
            raise self.disconnect_error

    def loop_stop(self):
        # This is the SDK precondition that caused the reported crash.
        assert self.loop_started, 'cannot join thread before it is started'
        self.events.append('stop')
        self.pause('loop_stop')
        self.stopped.set()


@pytest.fixture
def runtime(isolated, monkeypatch):
    client = Client()
    paho, mqtt, sdk = (
        ModuleType(name) for name in ('paho', 'paho.mqtt', 'paho.mqtt.client')
    )
    paho.mqtt, mqtt.client = mqtt, sdk
    sdk.Client = Mock(return_value=client)
    sdk.MQTT_ERR_SUCCESS = 0
    for module in (paho, mqtt, sdk):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    for name, value in dict(
        MQTT_HOST='offline.test',
        MQTT_PORT=1883,
        MQTT_USERNAME='test',
        MQTT_PASSWORD='test',
    ).items():
        setattr(isolated.config, name, value)
    mood = Mock()
    monkeypatch.setitem(
        sys.modules, 'isolated_core.mood', SimpleNamespace(mood_manager=mood)
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.song_manager',
        SimpleNamespace(
            song_manager=SimpleNamespace(get_last_song=lambda: 'test-song')
        ),
    )
    monkeypatch.setitem(
        sys.modules, 'isolated_core.movements', SimpleNamespace(stop_all_motors=Mock())
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.status_led',
        SimpleNamespace(set_status_led_state=Mock()),
    )
    module = isolated.load('mqtt')
    monkeypatch.setattr(module.subprocess, 'Popen', Mock())
    yield SimpleNamespace(
        module=module,
        client=client,
        factory=sdk.Client,
        logger=isolated.logger,
        mood=mood,
    )
    client.release.set()
    module.stop_mqtt(timeout=1)


@pytest.mark.parametrize('phase', ['connect', 'loop_start'])
def test_stop_during_start_never_joins_unstarted_network_thread(runtime, phase):
    m, client = runtime.module, runtime.client
    client.phase = phase
    client.release.clear()
    m.start_mqtt()
    assert client.entered.wait(1)
    worker = m._mqtt_thread
    before = time.monotonic()
    m.stop_mqtt(timeout=0.02)
    assert time.monotonic() - before < 0.5
    assert 'stop' not in client.events
    assert m.mqtt_connected is False
    # Neither a late CONNACK nor a late second startup can resurrect MQTT.
    m.on_connect(client, None, {}, 0)
    m.start_mqtt()
    runtime.factory.assert_called_once()
    assert not m.mqtt_connected
    client.release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert m.mqtt_client is None and m._mqtt_thread is None
    if phase == 'loop_start':
        assert client.events == ['connect', 'starting', 'started', 'disconnect', 'stop']
    else:
        assert client.events == ['connect', 'disconnect']
    client.publish.assert_not_called()
    runtime.logger.error.assert_not_called()


def test_stop_before_start_and_repeated_stops_are_harmless(runtime):
    m = runtime.module
    m.stop_mqtt()
    m.start_mqtt()
    m.stop_mqtt()
    runtime.factory.assert_not_called()
    assert m.mqtt_client is None and not m.mqtt_connected


def test_start_is_idempotent_and_stop_cleans_up_once(runtime):
    m, client = runtime.module, runtime.client
    m.start_mqtt()
    assert client.ready.wait(1)
    m.start_mqtt()
    runtime.factory.assert_called_once()
    m.stop_mqtt()
    m.stop_mqtt()
    assert client.events.count('stop') == client.events.count('disconnect') == 1
    assert m.mqtt_client is None and not m.mqtt_connected


def test_connected_state_and_discovery_require_broker_ack_and_survive_reconnect(
    runtime,
):
    m, client = runtime.module, runtime.client
    m.start_mqtt()
    assert client.ready.wait(1)
    assert not m.mqtt_connected
    m.mqtt_publish('billy/state', 'listening')
    client.publish.assert_not_called()
    m.on_connect(client, None, {}, 0)
    assert m.mqtt_connected
    client.subscribe.assert_any_call('billy/command')
    client.subscribe.assert_any_call('billy/say')
    client.publish.assert_any_call('billy/state', 'listening', retain=True)
    client.publish.assert_any_call('billy/song/last', 'test-song', retain=True)
    assert any(
        call.args[0] == 'homeassistant/sensor/billy/state/config'
        for call in client.publish.call_args_list
    )
    m.on_disconnect(client, None, 7)
    assert not m.mqtt_connected
    client.publish.reset_mock()
    m.mqtt_publish('billy/state', 'idle')
    client.publish.assert_not_called()
    m.on_connect(client, None, {}, 0)
    assert m.mqtt_connected
    client.publish.assert_any_call('billy/state', 'idle', retain=True)
    assert client.events.count('connect') == client.events.count('starting') == 1
    m.stop_mqtt()
    client.publish.reset_mock()
    m.on_connect(client, None, {}, 0)
    m.mqtt_publish('billy/state', 'speaking')
    assert not m.mqtt_connected
    client.publish.assert_not_called()


def test_cleanup_attempts_loop_stop_even_if_disconnect_raises(runtime):
    m, client = runtime.module, runtime.client
    client.disconnect_error = OSError('disconnected socket')
    m.start_mqtt()
    assert client.ready.wait(1)
    m.stop_mqtt()
    assert client.stopped.is_set()
    assert m.mqtt_client is None and not m.mqtt_connected
    runtime.logger.warning.assert_called()


def test_slow_network_cleanup_does_not_block_process_cleanup(runtime):
    m, client = runtime.module, runtime.client
    client.phase = 'loop_stop'
    client.release.clear()
    m.start_mqtt()
    assert client.ready.wait(1)
    worker = m._mqtt_thread
    before = time.monotonic()
    m.stop_mqtt(timeout=0.02)
    assert time.monotonic() - before < 0.5
    assert client.entered.is_set()
    assert not m.mqtt_connected
    client.release.set()
    worker.join(1)
    assert not worker.is_alive() and client.stopped.is_set()


def test_failed_loop_start_is_reported_without_joining_it(runtime):
    m, client = runtime.module, runtime.client
    client.loop_result = 3
    m.start_mqtt()
    worker = m._mqtt_thread
    assert client.ready.wait(1)
    if worker is not None:
        worker.join(1)
    assert 'stop' not in client.events
    assert 'disconnect' in client.events
    runtime.logger.error.assert_called_once()


@pytest.mark.parametrize('command', ['shutdown', 'reboot'])
def test_shutdown_command_does_not_wait_for_its_own_network_callback(
    runtime, monkeypatch, command
):
    m, client = runtime.module, runtime.client
    m.start_mqtt()
    assert client.ready.wait(1)
    stop = Mock(wraps=m.stop_mqtt)
    monkeypatch.setattr(m, 'stop_mqtt', stop)
    m.on_message(
        client, None, SimpleNamespace(topic='billy/command', payload=command.encode())
    )
    stop.assert_called_once_with(wait=False)
    m.subprocess.Popen.assert_called_once()
    # Late commands after shutdown are ignored.
    m.on_message(
        client, None, SimpleNamespace(topic='billy/command', payload=b'restart-billy')
    )
    m.subprocess.Popen.assert_called_once()
