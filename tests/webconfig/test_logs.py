"""Read-only journal boundaries; never need systemd or a running Pi."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions
from flask import Flask, jsonify, request


@pytest.fixture
def journal(monkeypatch):
    base = Path(__file__).parents[2] / 'webconfig/app'
    spec = importlib.util.spec_from_file_location(
        'journal_under_test', base / 'journal.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_sources_include_user_audio_and_keep_unix_user_scope(
    journal, monkeypatch
):
    run = Mock(return_value=SimpleNamespace(stdout='line\n', stderr='', returncode=0))
    monkeypatch.setattr(journal.subprocess, 'run', run)
    for source in journal.SOURCES:
        result = journal.read_logs(source)
        args = run.call_args.args[0]
        assert args[:2] == ['journalctl', '--no-pager']
        assert '-b' in args and args[args.index('-n') + 1] == '200'
        assert run.call_args.kwargs['timeout'] == 2
        assert 'sudo' not in args and '--follow' not in args
        assert result == {'source': source, 'logs': 'line\n', 'error': ''}
        if source in {'all', 'pipewire', 'bluetooth'}:
            for unit in journal.AUDIO_UNITS:
                index = args.index(f'_SYSTEMD_USER_UNIT={unit}')
                assert args[index + 1] == f'_UID={os.getuid()}'
                if source != 'pipewire' or unit != journal.AUDIO_UNITS[0]:
                    assert args[index - 1] == '+'
        else:
            assert not any(arg.startswith('_SYSTEMD_USER_UNIT=') for arg in args)
        if source in {'bluetooth', 'all'}:
            assert '_SYSTEMD_UNIT=bluetooth.service' in args
        if source in {'billy', 'all'}:
            assert '_SYSTEMD_UNIT=billy.service' in args
        else:
            assert '_SYSTEMD_UNIT=billy.service' not in args
        if source in {'web', 'all'}:
            assert '_SYSTEMD_UNIT=billy-webconfig.service' in args
        else:
            assert '_SYSTEMD_UNIT=billy-webconfig.service' not in args


@pytest.mark.parametrize(
    'source', ['../../etc/shadow', '--vacuum-time=1s', '', [], None]
)
def test_unknown_source_never_starts_a_process(journal, monkeypatch, source):
    run = Mock()
    monkeypatch.setattr(journal.subprocess, 'run', run)
    with pytest.raises(ValueError):
        journal.read_logs(source)
    run.assert_not_called()


@pytest.mark.parametrize(
    'failure',
    [
        FileNotFoundError(),
        PermissionError(),
        subprocess.TimeoutExpired('journalctl', 2),
    ],
)
def test_reader_failure_is_explicit_and_retryable(journal, monkeypatch, failure):
    monkeypatch.setattr(journal.subprocess, 'run', Mock(side_effect=failure))
    result = journal.read_logs('pipewire')
    assert result['error'] and result['logs'] == ''


def test_partial_access_and_large_records_are_bounded(journal, monkeypatch):
    monkeypatch.setattr(
        journal.subprocess,
        'run',
        Mock(
            return_value=SimpleNamespace(
                stdout='x' * 500_000,
                stderr='Not seeing messages from other users.',
                returncode=0,
            )
        ),
    )
    result = journal.read_logs('all')
    assert len(result['logs']) == journal.MAX_LOG_CHARS
    assert 'other users' in result['error']


def test_http_route_defaults_validates_and_disables_cache(journal, monkeypatch):
    # Load just the route so this test cannot initialize audio or credentials.
    package = ModuleType('journal_route_test')
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, package.__name__ + '.journal', journal)
    app = Flask('journal-route')
    definitions(
        'webconfig/app/routes/misc.py',
        ['logs'],
        {
            '__package__': 'journal_route_test.routes',
            'bp': app,
            'jsonify': jsonify,
            'request': request,
        },
    )
    monkeypatch.setattr(
        journal.subprocess,
        'run',
        Mock(
            return_value=SimpleNamespace(
                stdout='Bluetooth transport stopped',
                stderr='',
                returncode=0,
            )
        ),
    )
    client = app.test_client()
    assert client.get('/logs').json['source'] == 'billy'
    response = client.get('/logs?source=pipewire')
    assert response.status_code == 200 and response.json['source'] == 'pipewire'
    assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/logs?source=--vacuum-time%3D1s').status_code == 400
