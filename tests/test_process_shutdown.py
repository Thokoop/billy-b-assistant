"""Exit order: stop producers and output before native hardware cleanup."""

import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions


def test_signal_stops_sessions_and_output_before_gpio_and_ignores_reentry(monkeypatch):
    events = []
    monkeypatch.setitem(
        sys.modules,
        'core.movements',
        SimpleNamespace(cleanup_gpio=lambda: events.append('gpio')),
    )

    def exit_(code):
        raise SystemExit(code)

    module = definitions(
        'application/core/application.py',
        ['signal_handler'],
        {
            '_shutting_down': False,
            'logger': Mock(),
            'clear_ready': lambda: events.append('unready'),
            'set_status_led_state': lambda _: events.append('stopping'),
            'stop_mqtt': lambda: events.append('mqtt'),
            'core': SimpleNamespace(
                button=SimpleNamespace(
                    stop_background_services=lambda: events.append('session')
                )
            ),
            'shutdown_playback_worker': lambda: events.append('output-closed'),
            'cleanup_status_led': lambda: events.append('led-cleanup'),
            'sys': SimpleNamespace(exit=exit_),
        },
    )
    with pytest.raises(SystemExit) as exc:
        module.signal_handler(15, None)
    assert exc.value.code == 0
    assert events == [
        'unready',
        'stopping',
        'mqtt',
        'session',
        'output-closed',
        'gpio',
        'led-cleanup',
    ]
    module.signal_handler(15, None)
    assert len(events) == 7


def test_background_shutdown_stops_live_session_and_prevents_wakeword_reopen(
    monkeypatch,
):
    events = []
    flag = threading.Event()
    listener = SimpleNamespace(stop=lambda: events.append('wakeword'), start=Mock())
    monkeypatch.setitem(
        sys.modules,
        'core.bluetooth',
        SimpleNamespace(
            watcher=SimpleNamespace(stop=lambda: events.append('bluetooth'))
        ),
    )

    def stop_session():
        assert flag.is_set()
        events.append('session')

    module = definitions(
        'application/core/button.py',
        [
            'stop_background_services',
            '_resume_wakeword_listener',
            'trigger_session_start',
        ],
        {
            '__package__': 'core',
            'wakeword_listener': listener,
            '_shutdown_requested': flag,
            'bluetooth_controls': SimpleNamespace(
                clear_resume=lambda: events.append('resume-cleared')
            ),
            '_setup_monitor_stop': threading.Event(),
            '_stop_active_session': stop_session,
            'config': SimpleNamespace(WAKE_WORD_ENABLED=True),
            'logger': Mock(),
        },
    )
    module.stop_background_services()
    module._resume_wakeword_listener()
    for trigger in ('button', 'wakeword', 'mqtt'):
        assert module.trigger_session_start(trigger) is False
    assert events == ['bluetooth', 'resume-cleared', 'wakeword', 'session']
    listener.start.assert_not_called()
