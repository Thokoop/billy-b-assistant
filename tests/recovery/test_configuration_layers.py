"""The bootstrap must not decide which application features a release supports."""

import os
import sys
from unittest.mock import Mock, patch

import pytest
from test_configuration_recovery import promote

from billy_runtime import configuration, configuration_schema, supervisor, worker
from billy_runtime.configuration import (
    ConfigError,
    revision,
    validate,
    validate_bootstrap,
)
from billy_runtime.recovery import Recovery
from billy_runtime.recovery_ui import create_recovery_app


@pytest.mark.parametrize(
    'setting',
    [
        'REALTIME_AI_PROVIDER=future-provider',
        'UPDATE_CHANNEL=future-channel',
        'CHUNK_MS=0',
        'AEC_ENABLED=not-a-boolean',
        'STATUS_LED_PIN=999',
    ],
)
def test_only_release_validates_application_values(setting):
    content = f'FLASK_PORT=8080\n{setting}\n'
    assert validate_bootstrap(content, complete=True)
    with pytest.raises(ConfigError, match='at line 2'):
        validate(content, complete=True)


@pytest.mark.parametrize(
    'content',
    [
        '',
        '# no settings',
        'KEY',
        'KEY="unterminated',
        'A=1\nA=2',
        'A=\x00',
        'FLASK_PORT=0',
        'FLASK_PORT=65536',
        'FLASK_PORT=invalid',
    ],
)
def test_bootstrap_still_rejects_unusable_files(content):
    with pytest.raises(ConfigError):
        validate_bootstrap(content, complete=True)


@pytest.fixture
def worker_input(monkeypatch):
    notifications = Mock()
    monkeypatch.setattr(worker, 'notify', notifications)
    monkeypatch.setattr(configuration, '_loaded_keys', set())
    monkeypatch.setattr(configuration, '_original_environment', {})

    def start(mode, content):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, content.encode())
        os.close(write_fd)
        os.environ['BILLY_CONFIG_FD'] = str(read_fd)
        os.environ['BILLY_WORKER_MODE'] = mode
        return supervisor.worker_environment()

    with patch.dict(os.environ):
        os.environ.pop('BILLY_CONFIG_LOADED', None)
        yield start, notifications


@pytest.mark.parametrize('role', ['core', 'web'])
def test_normal_workers_reject_release_settings_before_marking_loaded(
    worker_input, role
):
    start, notifications = worker_input
    with pytest.raises(ConfigError, match='REALTIME_AI_PROVIDER at line 2'):
        start(role, 'OPENAI_API_KEY=secret\nREALTIME_AI_PROVIDER=unsupported-secret\n')
    assert 'BILLY_CONFIG_LOADED' not in os.environ
    notifications.assert_called_once_with(
        'config-error:Invalid value for REALTIME_AI_PROVIDER at line 2'
    )


def test_recovery_worker_does_not_load_application_schema(worker_input, monkeypatch):
    start, notifications = worker_input
    monkeypatch.setitem(sys.modules, 'billy_runtime.configuration_schema', None)
    content = 'FLASK_PORT=8080\nREALTIME_AI_PROVIDER=future-provider\n'
    assert start('recovery', content) == content.encode()
    assert os.environ['BILLY_APPLIED_REVISION'] == revision(content)
    assert notifications.call_args.args[0].startswith('environment:')


def test_backups_and_emergency_edit_remain_available_without_schema(
    tmp_path, monkeypatch
):
    content = 'FLASK_PORT=8080\nREALTIME_AI_PROVIDER=future-provider\n'
    (tmp_path / '.env').write_text(content)
    monkeypatch.setitem(sys.modules, 'billy_runtime.configuration_schema', None)
    store = Recovery(tmp_path)
    assert store.prepare() == (content.encode(), True)
    # Reports from BOTH fully validated release workers are still necessary.
    store.report('web', 'web-1', content.encode(), healthy=True, effective='same')
    assert not store.backups()
    promote(store, content.encode())
    assert store.backups()[0][2] == content.encode()
    (tmp_path / '.env').write_text('FLASK_PORT="corrupt')
    assert store.prepare() == (content.encode(), True)

    client = create_recovery_app(store).test_client()
    client.get('/repair/')
    with client.session_transaction() as session:
        csrf = session['csrf']
    updated = content + 'UPDATE_CHANNEL=future-channel\n'
    response = client.post(
        '/repair/',
        data={
            'action': 'save',
            'csrf': csrf,
            'file': '.env',
            'revision': revision(content),
            'content': updated,
        },
    )
    assert 'Saved.' in response.text
    assert 'selected release will check' in response.text
    assert store.env.read_text() == updated
    response = client.post('/repair/', data={'action': 'retry', 'csrf': csrf})
    assert 'Startup requested' in response.text
    assert store.read_state()['retry']


def test_regular_editor_and_patches_keep_full_release_validation(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    original = 'REALTIME_AI_PROVIDER=openai\n'
    path.write_text(original)
    monkeypatch.setitem(configuration_schema.ENUMS, 'REALTIME_AI_PROVIDER', {'openai'})
    with pytest.raises(ConfigError, match='REALTIME_AI_PROVIDER'):
        configuration.save_env(path, 'REALTIME_AI_PROVIDER=gpt-live\n')
    with pytest.raises(ConfigError, match='REALTIME_AI_PROVIDER'):
        configuration.patch_env(path, {'REALTIME_AI_PROVIDER': 'gpt-live'})
    assert path.read_text() == original


def test_release_configuration_failure_points_to_configuration_recovery(tmp_path):
    store = Recovery(tmp_path)
    store.initialize()
    (store.directory / 'runtime-error').write_text(
        'Configuration needs attention: Invalid value for REALTIME_AI_PROVIDER at line 2'
    )
    assert store.notice()['kind'] == 'configuration'
    assert 'REALTIME_AI_PROVIDER at line 2' in store.notice()['message']
