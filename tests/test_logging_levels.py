"""Logging policy, including diagnostic noise and audio loss visibility."""

import contextlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import definitions


@pytest.fixture
def logger():
    spec = importlib.util.spec_from_file_location(
        'logger_test', Path(__file__).parents[1] / 'application/core/logger.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.logger


@pytest.mark.parametrize(
    'level,expected',
    [
        ('ERROR', ['error']),
        ('WARNING', ['error', 'warning']),
        ('INFO', ['error', 'warning', 'info']),
        ('VERBOSE', ['error', 'warning', 'info', 'verbose', 'debug']),
    ],
)
def test_levels_are_cumulative(logger, monkeypatch, capsys, level, expected):
    monkeypatch.setenv('LOG_LEVEL', level)
    for name in ['error', 'warning', 'info', 'verbose', 'debug']:
        getattr(logger, name)(name, emoji='')
    assert capsys.readouterr().out.splitlines() == expected


@pytest.fixture
def mic_report(logger):
    clock = SimpleNamespace(now=0.0)
    method = definitions(
        'application/core/session/mic_manager_wrapper.py',
        ['_report_uplink'],
        {
            'logger': logger,
            'contextlib': contextlib,
            'time': SimpleNamespace(monotonic=lambda: clock.now),
            'audio': SimpleNamespace(MIC_RATE=48000, CHUNK_SIZE=1920),
        },
        cls='MicManagerWrapper',
    )
    return method._report_uplink, clock


@pytest.mark.parametrize('level', ['WARNING', 'INFO'])
def test_mic_loss_warns_immediately_then_batches_without_repeating_old_loss(
    mic_report, monkeypatch, capsys, level
):
    report, clock = mic_report
    monkeypatch.setenv('LOG_LEVEL', level)
    owner = SimpleNamespace()
    report(owner, {'overflow': 0, 'sent': 100})
    assert capsys.readouterr().out == ''
    report(owner, {'overflow': 1, 'sent': 100})
    out = capsys.readouterr().out
    assert 'Microphone audio loss: 1 input overflow (1 total' in out
    assert 'timing' not in out
    for second, overflow in [(5, 2), (10, 3), (15, 5), (20, 5), (25, 5)]:
        clock.now = second
        report(owner, {'overflow': overflow, 'sent': 200})
        assert capsys.readouterr().out == ''
    clock.now = 30
    report(owner, {'overflow': 5, 'sent': 300})
    assert (
        'Microphone audio loss: 4 input overflows (5 total' in capsys.readouterr().out
    )
    clock.now = 60
    report(owner, {'overflow': 5, 'sent': 400})
    assert capsys.readouterr().out == ''
    report(owner, {'overflow': 6, 'sent': 400})
    assert 'Microphone audio loss: 1 input overflow (6 total' in capsys.readouterr().out


def test_verbose_mic_diagnostics_include_actual_stream_latency_and_load(
    mic_report, monkeypatch, capsys
):
    report, _ = mic_report
    monkeypatch.setenv('LOG_LEVEL', 'VERBOSE')
    owner = SimpleNamespace(
        mic=SimpleNamespace(stream=SimpleNamespace(latency=0.085, cpu_load=0.125))
    )
    metrics = {
        'overflow': 3,
        'overflow_during_playback': 2,
        'overflow_without_playback': 1,
    }
    report(owner, metrics)
    out = capsys.readouterr().out
    assert 'Microphone audio loss:' in out
    assert "'input_latency_ms': 85.0" in out
    assert "'input_cpu_load': 0.125" in out
    assert "'sample_rate': 48000" in out and "'block_frames': 1920" in out
    assert "'overflow_during_playback': 2" in out
    assert "'overflow_without_playback': 1" in out
    assert 'input_latency_ms' not in metrics
    report(owner, metrics)
    out = capsys.readouterr().out
    assert 'Microphone timing:' in out and 'Microphone audio loss:' not in out


def test_mic_diagnostics_survive_stream_close_and_respect_error_level(
    mic_report, monkeypatch, capsys
):
    report, _ = mic_report

    class ClosedStream:
        @property
        def latency(self):
            raise RuntimeError('stream closed')

        @property
        def cpu_load(self):
            raise RuntimeError('stream closed')

    owner = SimpleNamespace(mic=SimpleNamespace(stream=ClosedStream()))
    monkeypatch.setenv('LOG_LEVEL', 'ERROR')
    report(owner, {'overflow': 1})
    assert capsys.readouterr().out == ''
    monkeypatch.setenv('LOG_LEVEL', 'VERBOSE')
    report(owner, {'overflow': 1})
    assert 'Microphone timing:' in capsys.readouterr().out


@pytest.mark.parametrize(
    'level,threshold',
    [
        ('ERROR', 500),
        ('WARNING', 400),
        ('INFO', 400),
        ('VERBOSE', 200),
    ],
)
@pytest.mark.parametrize('status', [200, 302, 404, 500])
def test_http_access_records_respect_level(monkeypatch, level, threshold, status):
    from unittest.mock import Mock

    from werkzeug.serving import WSGIRequestHandler

    from billy_runtime.http_logging import BillyRequestHandler

    parent_log = Mock()
    monkeypatch.setattr(WSGIRequestHandler, 'log', parent_log)
    monkeypatch.setenv('LOG_LEVEL', level)
    handler = object.__new__(BillyRequestHandler)
    handler.path = '/api/bluetooth'
    handler.requestline = 'GET /api/bluetooth HTTP/1.1'
    handler.log_request(status)
    assert parent_log.called == (status >= threshold)
    if parent_log.called:
        expected = 'error' if status >= 500 else 'warning' if status >= 400 else 'info'
        assert parent_log.call_args.args[0] == expected


@pytest.mark.parametrize('path', ['/__runtime_health', '/logs?source=all'])
@pytest.mark.parametrize('status', [200, 302, 404, 503])
def test_poll_success_is_silent_even_in_verbose_but_errors_remain(
    monkeypatch, path, status
):
    from unittest.mock import Mock

    from werkzeug.serving import WSGIRequestHandler

    from billy_runtime.http_logging import BillyRequestHandler

    parent_log = Mock()
    monkeypatch.setattr(WSGIRequestHandler, 'log', parent_log)
    monkeypatch.setenv('LOG_LEVEL', 'VERBOSE')
    handler = object.__new__(BillyRequestHandler)
    handler.path = path
    handler.requestline = f'GET {path} HTTP/1.1'
    handler.log_request(status)
    assert parent_log.called == (status >= 400)


def test_http_level_changes_apply_without_restarting_and_keep_server_errors(
    monkeypatch,
):
    from unittest.mock import Mock

    from werkzeug.serving import WSGIRequestHandler

    from billy_runtime.http_logging import BillyRequestHandler

    parent_log = Mock()
    monkeypatch.setattr(WSGIRequestHandler, 'log', parent_log)
    handler = object.__new__(BillyRequestHandler)
    handler.path = '/config'
    handler.requestline = 'GET /config HTTP/1.1'
    monkeypatch.setenv('LOG_LEVEL', 'VERBOSE')
    handler.log_request(200)
    parent_log.assert_called_once()
    parent_log.reset_mock()
    monkeypatch.setenv('LOG_LEVEL', 'ERROR')
    handler.log_request(200)
    parent_log.assert_not_called()
    handler.log_error('Socket failure: %s', 'broken pipe')
    parent_log.assert_called_once_with('error', 'Socket failure: %s', 'broken pipe')
