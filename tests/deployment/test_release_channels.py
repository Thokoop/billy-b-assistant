"""Release eligibility, old configuration and stale update metadata."""

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions
from dotenv import dotenv_values
from flask import Blueprint, Flask, jsonify, request

from billy_runtime import release_policy as policy
from billy_runtime.configuration import ConfigError, patch_env, validate


@pytest.mark.parametrize(
    'values,channel',
    [
        ({}, 'stable'),
        ({'SHOW_RC_VERSIONS': 'False'}, 'stable'),
        ({'SHOW_RC_VERSIONS': 'True'}, 'rc'),
        ({'SHOW_RC_VERSIONS': ' TRUE '}, 'rc'),
        ({'UPDATE_CHANNEL': 'stable', 'SHOW_RC_VERSIONS': 'True'}, 'stable'),
        ({'UPDATE_CHANNEL': 'beta', 'SHOW_RC_VERSIONS': 'False'}, 'beta'),
        ({'UPDATE_CHANNEL': ' RC '}, 'rc'),
        ({'UPDATE_CHANNEL': 'invalid', 'SHOW_RC_VERSIONS': 'True'}, 'stable'),
    ],
)
def test_channel_migration_and_explicit_precedence(values, channel):
    before = dict(values)
    assert policy.update_channel(values) == channel
    assert values == before  # Reading a preference never rewrites configuration.


@pytest.mark.parametrize(
    'tag,channels',
    [
        ('v2.7.0', {'stable', 'rc', 'beta'}),
        ('2.7.0', {'stable', 'rc', 'beta'}),
        ('v2.7.0-RC3', {'rc', 'beta'}),
        ('v2.7.0-rc.10', {'rc', 'beta'}),
        ('v2.7.0-beta.1', {'beta'}),
        ('v2.7.0-BETA2', {'beta'}),
        ('v2.7.0-beta', {'beta'}),
        ('v2.7.0-alpha.1', set()),
        ('v2.7.0-dev.1', set()),
        ('v2.7.0.post1', set()),
        ('v2.7.0+local', set()),
        ('v02.7.0', set()),
        ('v2.7.0-RC03', set()),
        ('v2.7', set()),
        ('dev', set()),
        ('beta', set()),
        ('2!2.7.0', set()),
        (None, set()),
    ],
)
def test_only_supported_version_tags_are_eligible(tag, channels):
    for channel in policy.CHANNELS:
        assert policy.allows_release(tag, channel) == (channel in channels)


def test_semantic_order_and_stability_levels():
    tags = [
        'v2.7.0-RC2',
        'v2.7.0-RC10',
        'v2.7.0',
        'v2.8.0-beta.2',
        'v2.8.0-beta.10',
        'v3.0.0-alpha.1',
    ]
    assert policy.newest_release(tags, 'stable') == 'v2.7.0'
    assert policy.newest_release(tags, 'rc') == 'v2.7.0'
    assert policy.newest_release(tags, 'beta') == 'v2.8.0-beta.10'
    assert (
        policy.newest_release(['v2.7.0-beta.20', 'v2.7.0-RC1'], 'beta') == 'v2.7.0-RC1'
    )
    assert policy.newest_release(['v2.7.0-RC2', 'v2.7.0-RC10'], 'rc') == 'v2.7.0-RC10'
    assert policy.newest_release(['dev', 'v2.7.0-beta.1'], 'stable') is None


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    monkeypatch.delenv('UPDATE_CHANNEL', raising=False)
    monkeypatch.delenv('SHOW_RC_VERSIONS', raising=False)
    namespace = {
        'KNOWN_RELEASE_TAGS': (),
        'VERSION_STATE_LOCK': threading.RLock(),
        'newest_release': policy.newest_release,
        'release_version': policy.release_version,
        'update_channel': policy.update_channel,
        'logger': Mock(),
        '_fetch_github_json': Mock(return_value=[]),
        'get_current_version': lambda: 'v2.6.0',
        'load_versions': lambda: {'version': {'latest': 'v2.7.0-beta.1'}},
        'save_versions': Mock(),
        'fetch_release_note_for_tag': Mock(return_value=None),
        'RELEASE_NOTE': {},
        'time': time,
    }
    functions = definitions(
        'webconfig/app/state.py',
        [
            'fetch_latest_tag',
            'available_version',
            'bootstrap_versions_and_release_note',
        ],
        namespace,
    )
    return functions, namespace


def test_tag_fetch_reads_following_pages_and_refilters_without_network(
    metadata, monkeypatch
):
    functions, ns = metadata
    ns['_fetch_github_json'].side_effect = [
        [{'name': f'v2.8.0-beta.{i}'} for i in range(100)],
        [{'name': 'v2.7.0-RC2'}, {'name': 'v2.6.1'}, {'name': 'dev'}, {'other': True}],
    ]
    assert functions.fetch_latest_tag() == 'v2.6.1'
    assert ns['_fetch_github_json'].call_count == 2
    assert 'page=2' in ns['_fetch_github_json'].call_args.args[0]
    monkeypatch.setenv('UPDATE_CHANNEL', 'rc')
    assert functions.available_version('v2.6.0', 'v2.8.0-beta.99') == 'v2.7.0-RC2'
    monkeypatch.setenv('UPDATE_CHANNEL', 'beta')
    assert functions.available_version('v2.6.0', 'v2.6.1') == 'v2.8.0-beta.99'
    assert ns['_fetch_github_json'].call_count == 2


@pytest.mark.parametrize(
    'current,stored,channel,expected',
    [
        ('v2.6.0', 'v2.7.0-beta.1', 'stable', 'v2.6.0'),
        ('v2.7.0-RC3', 'v2.6.0', 'stable', 'v2.7.0-RC3'),
        ('v2.7.0-beta.10', 'v2.7.0-beta.2', 'beta', 'v2.7.0-beta.10'),
        ('v2.7.0-beta.1', 'v2.7.0-RC1', 'rc', 'v2.7.0-RC1'),
        ('v2.7.0-RC3', 'v2.7.0', 'stable', 'v2.7.0'),
        ('v2.7.0', 'v2.7.0-RC10', 'rc', 'v2.7.0'),
        ('unknown', 'v2.7.0', 'stable', 'unknown'),
        ('v2.6.0', 'dev', 'beta', 'v2.6.0'),
    ],
)
def test_cached_tags_never_bypass_channel_or_offer_downgrade(
    metadata, monkeypatch, current, stored, channel, expected
):
    functions, _ = metadata
    monkeypatch.setenv('UPDATE_CHANNEL', channel)
    assert functions.available_version(current, stored) == expected


def test_offline_refresh_does_not_restore_cached_beta_on_stable(metadata):
    functions, ns = metadata
    ns['_fetch_github_json'].side_effect = OSError('offline')
    functions.bootstrap_versions_and_release_note()
    ns['save_versions'].assert_called_once_with('v2.6.0', 'v2.6.0')


@pytest.mark.parametrize('channel', ['stable', 'rc', 'beta'])
def test_configuration_accepts_supported_channels(channel):
    assert validate(f'UPDATE_CHANNEL={channel}')['UPDATE_CHANNEL'] == channel


def test_configuration_rejects_unknown_channel():
    with pytest.raises(ConfigError):
        validate('UPDATE_CHANNEL=nightly')


@pytest.fixture
def settings_api(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    path.write_text('SHOW_RC_VERSIONS=True\nFLASK_PORT=80\nOPENAI_API_KEY=preserved\n')
    monkeypatch.delenv('UPDATE_CHANNEL', raising=False)
    monkeypatch.setenv('SHOW_RC_VERSIONS', 'True')
    monkeypatch.setenv('FLASK_PORT', '80')
    monkeypatch.setattr('importlib.reload', lambda module: module)

    def load_environment(*args, **kwargs):
        for key, value in dotenv_values(path).items():
            monkeypatch.setenv(key, value)

    bp = Blueprint('settings-test', __name__)
    thread = Mock()
    definitions(
        'webconfig/app/routes/system.py',
        ['save'],
        {
            'bp': bp,
            'request': request,
            'jsonify': jsonify,
            'os': os,
            'dotenv_values': dotenv_values,
            'ENV_PATH': str(path),
            'CONFIG_KEYS': ['UPDATE_CHANNEL', 'SHOW_RC_VERSIONS', 'FLASK_PORT'],
            'BOOLEAN_CONFIG_KEYS': set(),
            'update_channel': policy.update_channel,
            'patch_env': patch_env,
            'ConfigError': ConfigError,
            'load_environment': load_environment,
            'core_config': SimpleNamespace(),
            '_mark_core_config_fresh': Mock(),
            'threading': SimpleNamespace(Thread=thread),
            'bootstrap_versions_and_release_note': Mock(),
        },
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client(), path, thread


@pytest.mark.parametrize(
    'channel,legacy', [('stable', 'False'), ('rc', 'True'), ('beta', 'True')]
)
def test_settings_save_persists_channel_and_legacy_fallback_atomically(
    settings_api, channel, legacy
):
    client, path, thread = settings_api
    response = client.post(
        '/save', json={'UPDATE_CHANNEL': channel, 'SHOW_RC_VERSIONS': 'False'}
    )
    assert response.status_code == 200
    config = dotenv_values(path)
    assert config['UPDATE_CHANNEL'] == channel
    assert config['SHOW_RC_VERSIONS'] == legacy
    assert config['OPENAI_API_KEY'] == 'preserved'
    assert bool(response.json.get('update_channel_changed')) == (channel != 'rc')
    assert thread.call_count == int(channel != 'rc')


def test_invalid_channel_does_not_change_existing_config(settings_api):
    client, path, _ = settings_api
    before = path.read_bytes()
    assert client.post('/save', json={'UPDATE_CHANNEL': 'nightly'}).status_code == 400
    assert path.read_bytes() == before


def test_old_settings_payload_does_not_overwrite_explicit_beta(settings_api):
    client, path, _ = settings_api
    assert client.post('/save', json={'UPDATE_CHANNEL': 'beta'}).status_code == 200
    assert client.post('/save', json={'SHOW_RC_VERSIONS': 'False'}).status_code == 200
    assert policy.update_channel(dotenv_values(path)) == 'beta'
