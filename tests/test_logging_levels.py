"""Logging policy, including diagnostic noise and audio loss visibility."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import definitions


@pytest.fixture
def logger():
    spec = importlib.util.spec_from_file_location(
        'logger_test', Path(__file__).parents[1] / 'core/logger.py'
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


def test_realtime_healthy_timing_is_verbose_but_drops_warn(logger, monkeypatch, capsys):
    method = definitions(
        'core/session/mic_manager_wrapper.py',
        ['_report_uplink'],
        {'logger': logger},
        cls='MicManagerWrapper',
    )
    monkeypatch.setenv('LOG_LEVEL', 'WARNING')
    owner = SimpleNamespace()
    method._report_uplink(owner, {'overflow': 0, 'sent': 100})
    assert capsys.readouterr().out == ''
    method._report_uplink(owner, {'overflow': 1, 'sent': 99})
    assert 'realtime mic timing' in capsys.readouterr().out

    method._report_uplink(owner, {'overflow': 1, 'sent': 199})
    assert capsys.readouterr().out == ''
    method._report_uplink(owner, {'overflow': 2, 'sent': 299})
    assert 'realtime mic timing' in capsys.readouterr().out


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
