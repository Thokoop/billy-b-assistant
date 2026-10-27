"""Release-owned status works with the legacy supervisor health format."""

import json
import os
import time
from unittest.mock import Mock

import pytest

from billy_runtime import storage, worker


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'runtime_directory', lambda root: tmp_path)
    monkeypatch.setenv('BILLY_WORKER_MODE', 'core')
    # Deliberately no activity field: this is the health report of existing units.
    (tmp_path / 'core.json').write_text(
        json.dumps({'healthy': True, 'seen': time.time()})
    )
    return tmp_path


@pytest.mark.parametrize(
    'state', ['idle', 'listening', 'speaking', 'bt_media', 'in_call']
)
def test_reads_live_activity_with_legacy_supervisor_and_no_mqtt(runtime, state):
    worker.set_activity(state)
    assert worker.read_activity() == state
    assert json.loads((runtime / 'activity.json').read_text())['pid'] == os.getpid()


@pytest.mark.parametrize(
    'report',
    [
        {},
        {'healthy': False},
        {'healthy': True, 'seen': 0},
        {'healthy': True, 'seen': 'bad'},
        [],
        None,
    ],
)
def test_unhealthy_or_stale_core_never_reports_old_activity(runtime, report):
    worker.set_activity('speaking')
    (runtime / 'core.json').write_text(json.dumps(report))
    assert worker.read_activity() is None


def test_dead_worker_and_reused_pid_do_not_revive_old_state(runtime, monkeypatch):
    worker.set_activity('bt_media')
    monkeypatch.setattr(
        worker, '_process_identity', Mock(side_effect=ProcessLookupError)
    )
    assert worker.read_activity() is None
    monkeypatch.setattr(worker, '_process_identity', lambda pid: 'new-process-start')
    assert worker.read_activity() is None


@pytest.mark.parametrize('mode', ['web', 'recovery', ''])
def test_web_led_tests_cannot_replace_core_activity(runtime, monkeypatch, mode):
    worker.set_activity('listening')
    monkeypatch.setenv('BILLY_WORKER_MODE', mode)
    worker.set_activity('idle')
    assert worker.read_activity() == 'listening'


def test_failed_status_write_cannot_interrupt_audio(runtime, monkeypatch):
    monkeypatch.setattr(storage, 'atomic_write', Mock(side_effect=PermissionError))
    worker.set_activity('speaking')
    assert worker.read_activity() is None


def test_missing_or_malformed_activity_is_safe(runtime):
    assert worker.read_activity() is None
    for text in ['partial json', '[]', 'null', '{"pid": -1}']:
        (runtime / 'activity.json').write_text(text)
        assert worker.read_activity() is None


def test_logical_led_state_reports_once_and_preserves_existing_state():
    import threading
    from types import SimpleNamespace

    from conftest import definitions

    publish = Mock()
    method = definitions(
        'application/core/status_led.py',
        ['set_state'],
        {'logger': Mock(), 'set_activity': publish},
        cls='StatusLed',
    )
    led = SimpleNamespace(
        _lock=threading.Lock(),
        _STATE_CONFIG={'idle': {}, 'bt_media': {}},
        _state='idle',
    )
    method.set_state(led, 'bt_media')
    assert led._state == 'bt_media'
    publish.assert_called_once_with('bt_media')
    method.set_state(led, 'bt_media')
    method.set_state(led, 'unknown')
    publish.assert_called_once()
