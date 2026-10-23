"""Local-only regressions for shared Bluetooth status delivery."""

import importlib.util
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def stream(monkeypatch):
    base = Path(__file__).parents[2] / 'webconfig/app'
    package = ModuleType('status_test_pkg')
    package.__path__ = [str(base)]
    monkeypatch.setitem(sys.modules, package.__name__, package)

    # Exercise the handler directly in this unit test; actual Flask-Sock
    # decoration registers a route and returns None. Socket transport is tested
    # separately against the supervised live WSGI server.
    class DirectSock:
        def route(self, *args, **kwargs):
            return lambda handler: handler

    monkeypatch.setitem(
        sys.modules, 'status_test_pkg.sock_compat', SimpleNamespace(Sock=DirectSock)
    )
    spec = importlib.util.spec_from_file_location(
        'status_test_pkg.websocket', base / 'websocket.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    route = ModuleType('status_test_pkg.routes.bluetooth')
    route.status_snapshot = lambda: ({'devices': [], 'pairing_seconds': 0}, 200)
    monkeypatch.setitem(sys.modules, route.__name__, route)
    audio = ModuleType('status_test_pkg.routes.audio')
    audio.device_info_snapshot = lambda **kw: (
        {'mic': 'USB mic', 'speaker': 'USB speaker'},
        200,
    )
    monkeypatch.setitem(sys.modules, audio.__name__, audio)
    monkeypatch.setattr(
        module._recovery_status, 'get', lambda: {'active': False, 'message': ''}
    )
    return module, route


def test_slow_bluetooth_is_shared_and_never_blocks_stream(stream):
    module, route = stream
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return {'devices': ['phone']}, 200

    route.status_snapshot = read
    original = module._bluetooth_status.refresh

    def refresh():
        try:
            original()
        finally:
            finished.set()

    module._bluetooth_status.refresh = refresh
    try:
        assert module._bluetooth_status.get() is None
        assert entered.wait(1)
        for _ in range(20):
            assert module._bluetooth_status.get() is None
        assert len(calls) == 1
    finally:
        release.set()
        assert finished.wait(1)
    assert module._bluetooth_status.get() == {'devices': ['phone']}
    assert len(calls) == 1


def test_failure_is_published_and_worker_can_retry(stream, monkeypatch):
    module, route = stream

    def fail():
        raise RuntimeError('D-Bus unavailable')

    route.status_snapshot = fail
    module._bluetooth_status.refreshing = True
    module._bluetooth_status.refresh()
    assert module._bluetooth_status.get() == {'error': 'D-Bus unavailable'}
    assert not module._bluetooth_status.refreshing
    started = []
    monkeypatch.setattr(
        module,
        'threading',
        SimpleNamespace(
            Thread=lambda **kw: SimpleNamespace(start=lambda: started.append(kw))
        ),
    )
    module._bluetooth_status.checked_at -= 3
    module._bluetooth_status.get()
    assert len(started) == 1
    assert started[0]['daemon'] is True


def test_new_snapshot_every_two_seconds_at_most(stream, monkeypatch):
    module, _ = stream
    started = []
    monkeypatch.setattr(
        module,
        'threading',
        SimpleNamespace(
            Thread=lambda **kw: SimpleNamespace(start=lambda: started.append(kw))
        ),
    )
    module._bluetooth_status.checked_at = 10
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: 11.99))
    module._bluetooth_status.get()
    assert started == []
    module.time.monotonic = lambda: 12
    module._bluetooth_status.get()
    module._bluetooth_status.get()
    assert len(started) == 1


def test_thread_creation_failure_does_not_disable_future_updates(stream, monkeypatch):
    module, _ = stream

    def fail():
        raise RuntimeError('no thread')

    monkeypatch.setattr(
        module,
        'threading',
        SimpleNamespace(Thread=lambda **kw: SimpleNamespace(start=fail)),
    )
    assert module._bluetooth_status.get() is None
    assert not module._bluetooth_status.refreshing


def test_status_stream_only_sends_changes_and_keeps_logs(stream, monkeypatch):
    module, _ = stream
    snapshots = iter([
        None,
        {'pending': None},
        {'pending': None},
        {'pending': {'passkey': '012345'}},
    ])
    monkeypatch.setattr(module, 'get_service_status', lambda: {'status': 'active'})
    monkeypatch.setattr(module._bluetooth_status, 'get', lambda: next(snapshots))
    monkeypatch.setattr(module._device_info_status, 'get', lambda: {'mic': 'USB mic'})
    monkeypatch.setattr(module._log_status['billy'], 'get', lambda: {'logs': 'logs'})
    monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=lambda _: None))
    messages = []
    module.websocket_handler(
        SimpleNamespace(
            receive=lambda **kw: None,
            send=lambda message: messages.append(json.loads(message)),
        )
    )
    statuses = [message['data'] for message in messages if message['type'] == 'status']
    assert len(statuses) == 3
    assert 'bluetooth' not in statuses[0]
    assert statuses[0]['device_info'] == {'mic': 'USB mic'}
    assert statuses[-1]['bluetooth']['pending']['passkey'] == '012345'
    assert [message['data'] for message in messages if message['type'] == 'logs'] == [
        'logs'
    ]


def test_device_sampler_does_one_probe_and_has_independent_interval(stream):
    module, _ = stream
    calls = []
    audio = sys.modules['status_test_pkg.routes.audio']
    audio.device_info_snapshot = lambda **kw: (
        calls.append(kw) or {'mic': 'Unknown', 'speaker': 'Unknown'},
        200,
    )
    module._device_info_status.refresh()
    assert calls == [{'max_attempts': 1}]
    assert module._device_info_status.get()['mic'] == 'Unknown'
    assert module._device_info_status.interval == 5
    assert module._bluetooth_status.interval == 2
    assert module._bluetooth_status.snapshot is None


def device_reader(devices, raises=False):
    from conftest import definitions

    calls, sleeps = [], []

    def query():
        calls.append(True)
        if raises:
            raise RuntimeError('audio unavailable')
        return devices

    read = definitions(
        'webconfig/app/routes/audio.py',
        ['device_info_snapshot'],
        {
            'sd': SimpleNamespace(query_devices=query),
            'time': SimpleNamespace(sleep=sleeps.append),
            'core_config': SimpleNamespace(
                MIC_PREFERENCE='preferred', SPEAKER_PREFERENCE='preferred'
            ),
            '_preference_matches_device_name': lambda preference, name: (
                preference in name
            ),
        },
    ).device_info_snapshot
    return read, calls, sleeps


def test_device_labels_preserve_selection_and_fallback():
    read, calls, sleeps = device_reader([
        {'name': 'fallback', 'max_input_channels': 1, 'max_output_channels': 1},
        {'name': 'preferred mic', 'max_input_channels': 1, 'max_output_channels': 0},
    ])
    assert read(max_attempts=1) == (
        {'mic': 'preferred mic', 'speaker': 'fallback'},
        200,
    )
    assert len(calls) == 1
    assert sleeps == []


def test_device_sampler_missing_hardware_does_not_retry_inline():
    read, calls, sleeps = device_reader([])
    assert read(max_attempts=1) == ({'mic': 'Unknown', 'speaker': 'Unknown'}, 200)
    assert len(calls) == 1
    assert sleeps == []
    read()
    assert len(calls) == 7  # Existing HTTP retry behaviour remains available.
    assert sleeps == [0.4] * 5


def test_device_query_failure_has_same_error_shape():
    read, _, _ = device_reader([], raises=True)
    assert read(max_attempts=1) == (
        {'mic': 'Unknown', 'speaker': 'Unknown', 'error': 'audio unavailable'},
        500,
    )


def test_recovery_change_is_delivered_even_when_service_stays_active(
    stream, monkeypatch
):
    module, _ = stream
    samples = iter([
        {'active': True, 'message': 'Restored'},
        {'active': False, 'message': ''},
    ])
    monkeypatch.setattr(module, 'get_service_status', lambda: {'status': 'active'})
    monkeypatch.setattr(module._recovery_status, 'get', lambda: next(samples))
    monkeypatch.setattr(module._bluetooth_status, 'get', lambda: None)
    monkeypatch.setattr(module._device_info_status, 'get', lambda: None)
    monkeypatch.setattr(module._log_status['billy'], 'get', lambda: {'logs': ''})
    monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=lambda _: None))
    messages = []
    module.websocket_handler(
        SimpleNamespace(
            receive=lambda **kw: None,
            send=lambda message: messages.append(json.loads(message)),
        )
    )
    statuses = [m['data'] for m in messages if m['type'] == 'status']
    assert [s['recovery']['active'] for s in statuses] == [True, False]
    assert [s['status'] for s in statuses] == ['active', 'active']


def test_log_subscription_switches_sources_without_blocking_status(stream, monkeypatch):
    module, _ = stream
    controls = iter([
        {"type": "logs_subscribe", "source": None},
        {"type": "logs_subscribe", "source": "pipewire"},
        {"type": "logs_subscribe", "source": "../../etc"},
        {"type": "logs_subscribe", "source": []},
        {"type": "logs_subscribe", "source": "bluetooth"},
        {"type": "logs_subscribe", "source": None},
    ])
    reads = []

    def receive(**kwargs):
        assert kwargs == {"timeout": 0}
        return json.dumps(next(controls))

    for source, sampler in module._log_status.items():
        monkeypatch.setattr(
            sampler, 'get', lambda s=source: reads.append(s) or {"logs": s}
        )
    monkeypatch.setattr(module, 'get_service_status', lambda: {'status': 'active'})
    for sampler in (
        module._bluetooth_status,
        module._device_info_status,
        module._recovery_status,
    ):
        monkeypatch.setattr(sampler, 'get', lambda: None)
    monkeypatch.setattr(module, 'time', SimpleNamespace(sleep=lambda _: None))
    messages = []
    module.websocket_handler(
        SimpleNamespace(receive=receive, send=lambda s: messages.append(json.loads(s)))
    )
    assert reads == ['pipewire', 'pipewire', 'pipewire', 'bluetooth']
    logs = [m for m in messages if m['type'] == 'logs']
    assert [m['source'] for m in logs] == ['pipewire', 'bluetooth']
    assert any(m['type'] == 'status' for m in messages)


def test_slow_journal_is_shared_and_does_not_block_other_sources(stream, monkeypatch):
    module, _ = stream
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    sampler = module._log_status['pipewire']

    def read():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return {'logs': 'pipewire log'}

    sampler.read = read
    original = sampler.refresh

    def refresh():
        try:
            original()
        finally:
            finished.set()

    sampler.refresh = refresh
    try:
        assert sampler.get() is None
        assert entered.wait(1)
        for _ in range(20):
            assert sampler.get() is None
        assert len(calls) == 1
        monkeypatch.setattr(
            module._log_status['billy'], 'read', lambda: {'logs': 'core'}
        )
        module._log_status['billy'].refresh()
        assert module._log_status['billy'].get()['logs'] == 'core'
    finally:
        release.set()
        assert finished.wait(1)
    assert sampler.get()['logs'] == 'pipewire log'
