from pathlib import Path

import pytest
from conftest import definitions
from flask import Blueprint, Flask, jsonify, request

from billy_runtime.configuration import ConfigError, ConflictError, revision, save_env
from billy_runtime.recovery import Recovery


@pytest.fixture
def editor(tmp_path):
    path = tmp_path / '.env'
    path.write_text('FLASK_PORT=8080\n')
    store = Recovery(tmp_path)
    store.prepare()
    original = store._quarantine(b'FLASK_PORT=8081\n')
    bp = Blueprint('env_editor_test', __name__)
    definitions(
        'application/webconfig/app/routes/system.py',
        ['get_env', 'env_history', 'save_env'],
        {
            'bp': bp,
            'Path': Path,
            'Recovery': Recovery,
            'ENV_PATH': str(path),
            'request': request,
            'jsonify': jsonify,
            'persist_env': save_env,
            'ConfigError': ConfigError,
            'ConflictError': ConflictError,
        },
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client(), store, original


def test_open_original_then_save_updates_only_active_file(editor):
    client, store, original = editor
    before = store.env.read_bytes()
    response = client.get('/get-env', query_string={'source': original})
    assert response.status_code == 200
    assert response.data == b'FLASK_PORT=8081\n'
    assert response.headers['X-Config-Revision'] == revision(before)
    assert response.headers['Cache-Control'] == 'no-store'
    assert store.env.read_bytes() == before
    saved = client.post(
        '/save-env',
        json={
            'content': response.text,
            'revision': response.headers['X-Config-Revision'],
        },
    )
    assert saved.status_code == 200
    assert store.env.read_bytes() == response.data
    assert store.read_state()['mode'] == 'probation'
    assert (store.directory / original).exists()


def test_stale_historical_editor_does_not_overwrite_other_save(editor):
    client, store, original = editor
    response = client.get('/get-env', query_string={'source': original})
    save_env(store.env, 'FLASK_PORT=8082')
    saved = client.post(
        '/save-env',
        json={
            'content': response.text,
            'revision': response.headers['X-Config-Revision'],
        },
    )
    assert saved.status_code == 409
    assert store.env.read_text() == 'FLASK_PORT=8082'


def test_invalid_save_and_unknown_history_preserve_config(editor):
    client, store, _ = editor
    before = store.env.read_bytes()
    response = client.post(
        '/save-env', json={'content': 'FLASK_PORT=bad', 'revision': revision(before)}
    )
    assert response.status_code == 400
    assert store.env.read_bytes() == before
    assert client.get('/get-env?source=../../.env').status_code == 404
    assert client.post('/save-env', json=['bad']).status_code == 400


def test_history_metadata_is_uncached_without_contents(editor):
    client, store, _ = editor
    response = client.get('/env-history')
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'FLASK_PORT' not in response.text


def test_normal_web_routes_repair_to_software_settings(editor, monkeypatch):
    import os
    import sys
    from types import ModuleType, SimpleNamespace

    _, store, _ = editor
    app = Flask('normal-recovery-test')
    monkeypatch.setitem(
        sys.modules, 'webconfig.app', SimpleNamespace(create_app=lambda: app)
    )
    monkeypatch.setitem(sys.modules, 'core', ModuleType('core'))
    monkeypatch.setitem(
        sys.modules,
        'core.providers',
        SimpleNamespace(initialize_providers=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        'core.profile_manager',
        SimpleNamespace(user_manager=SimpleNamespace(load_default_user=lambda: None)),
    )
    normal = definitions(
        'application/webconfig/startup.py',
        ['normal_app'],
        {'ROOT': store.root, 'os': os},
    ).normal_app()
    response = normal.test_client().get('/repair/')
    assert response.status_code == 302
    assert response.location == '/settings#section-software-version'
    assert normal.test_client().get('/__runtime_health').status_code == 200
    assert any(
        fn().get('recovery_notice') == store.notice()
        for fn in normal.template_context_processors[None]
    )


def test_live_settings_with_quoted_values_and_blank_lines_survive_save(editor):
    client, store, _ = editor
    opened = client.get('/get-env')
    content = (
        "FLASK_PORT=8080\nREALTIME_AI_PROVIDER='gpt-live'\n"
        "OPENAI_LIVE_MODEL='gpt-live-1'\n"
        "OPENAI_LIVE_BACKEND_MODEL='gpt-6-luna'\n\n"
        "WEB_SEARCH_ENABLED='true'\nHA_AGENT_ID=''\n\n"
        "UPDATE_CHANNEL='beta'\n"
    )
    saved = client.post(
        '/save-env',
        json={
            'content': content,
            'revision': opened.headers['X-Config-Revision'],
        },
    )
    assert saved.status_code == 200
    loaded, allowed = store.prepare()
    assert allowed
    assert loaded.decode() == content
    assert store.read_state()['mode'] == 'probation'
    assert client.get('/get-env').text == content
