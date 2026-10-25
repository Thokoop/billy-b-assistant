"""The 10-second button tells a later Wi-Fi save that a reboot is needed."""

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import core.wifi_setup as wifi_setup
from conftest import definitions


def test_marker_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(wifi_setup, 'runtime_directory', lambda _root: tmp_path / 'run')
    assert not wifi_setup.setup_follows_client()
    wifi_setup.mark_setup_after_client()
    assert wifi_setup.setup_follows_client()


def test_marker_failure_is_not_fatal(monkeypatch, tmp_path):
    blocker = tmp_path / 'file'
    blocker.write_text('')
    monkeypatch.setattr(wifi_setup, 'runtime_directory', lambda _root: blocker)
    wifi_setup.mark_setup_after_client()
    assert not wifi_setup.setup_follows_client()


def test_the_setup_button_marks_before_opening_the_hotspot():
    calls = []
    namespace = {
        '_require_button_release': lambda: None,
        'mark_setup_after_client': lambda: calls.append('mark'),
        'subprocess': SimpleNamespace(
            run=lambda *a, **k: calls.append('hotspot'),
            SubprocessError=subprocess.SubprocessError,
        ),
        'status_led': SimpleNamespace(set_wifi_setup_starting=lambda *_: None),
        'logger': Mock(),
        'flash_status_led_state': Mock(),
        'PROJECT_ROOT': wifi_setup.PROJECT_ROOT,
    }
    definitions('application/core/button.py', ['_force_wifi_setup'], namespace)
    namespace['_force_wifi_setup']()
    assert calls == ['mark', 'hotspot']
