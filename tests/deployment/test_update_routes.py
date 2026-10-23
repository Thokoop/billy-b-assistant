from unittest.mock import Mock

from conftest import definitions
from flask import Blueprint, Flask, jsonify, request

from billy_runtime import updates
from billy_runtime.recovery import Recovery
from billy_runtime.recovery_ui import create_recovery_app
from billy_runtime.release_policy import newest_release, release_version


def normal_app(root):
    app = Flask('test-update-routes')
    bp = Blueprint('software', __name__)
    functions = definitions(
        'webconfig/app/routes/system.py',
        [
            '_software_action',
            'software_status',
            'perform_update',
            'simulate_update',
            'software_rollback',
            'repair_system_packages',
        ],
        {
            'bp': bp,
            'jsonify': jsonify,
            'request': request,
            'PROJECT_ROOT': root,
            'Recovery': Recovery,
            'load_versions': lambda: {'version': {'latest': 'v2.0.0'}},
            'get_current_version': lambda: 'v1.0.0',
            'available_version': lambda current, stored: stored,
        },
    )
    app.register_blueprint(bp)
    return app


def test_update_mutations_require_browser_header_and_return_immediately(
    tmp_path, monkeypatch
):
    queue = Mock(return_value={'job': {'id': 'abc', 'state': 'queued'}})
    monkeypatch.setattr(updates, 'queue', queue)
    client = normal_app(tmp_path).test_client()
    for url in (
        '/update',
        '/update-simulate',
        '/software/rollback',
        '/software/packages',
    ):
        assert client.post(url).status_code == 403
    queue.assert_not_called()
    response = client.post('/update', headers={'X-Billy-Update': '1'})
    assert response.status_code == 202
    queue.assert_called_once_with('update', 'v2.0.0', root=tmp_path)
    assert 'Set-Cookie' not in response.headers


def test_fresh_browser_can_use_rescue_after_ui_update_without_access_cookie(
    tmp_path, monkeypatch
):
    (tmp_path / '.env').write_text('FLASK_PORT=8080\n')
    queue = Mock(return_value={'job': {'id': 'abc', 'state': 'queued'}})
    monkeypatch.setattr(updates, 'queue', queue)
    normal = normal_app(tmp_path).test_client()
    response = normal.post('/update-simulate', headers={'X-Billy-Update': '1'})
    assert response.status_code == 202
    rescue = create_recovery_app(Recovery(tmp_path)).test_client()
    response = rescue.get('/settings')
    assert b'Software recovery' in response.data
    assert b'FLASK_PORT=8080' in response.data
    queue.reset_mock()
    assert (
        rescue.post(
            '/settings', data={'action': 'rollback', 'csrf': 'wrong'}
        ).status_code
        == 403
    )
    queue.assert_not_called()
    with rescue.session_transaction() as session:
        csrf = session['csrf']
    response = rescue.post('/settings', data={'action': 'rollback', 'csrf': csrf})
    assert response.status_code == 200
    queue.assert_called_once_with('rollback', root=tmp_path)


def test_legacy_git_reset_is_rejected_before_data_changes():
    app = Flask('reset')
    functions = definitions(
        'webconfig/app/routes/system.py',
        ['factory_reset'],
        {'bp': Blueprint('unused', __name__), 'request': request, 'jsonify': jsonify},
    )
    with app.test_request_context(
        json={'confirm': True, 'options': {'git': True, 'env': True}}
    ):
        response, status = functions.factory_reset()
        assert status == 409 and 'Reinstall' in response.json['error']


def test_profile_settings_keep_using_shared_data_after_release_switch(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from billy_runtime import paths

    monkeypatch.setattr(paths, 'INSTALL_ROOT', tmp_path)
    functions = definitions(
        'webconfig/app/routes/profiles.py',
        ['get_profiles_dir', '_get_env_path'],
        {'Path': Path},
    )
    assert functions.get_profiles_dir() == tmp_path / 'profiles'
    assert functions._get_env_path() == tmp_path / '.env'


def test_recovery_package_repair_uses_same_job_and_csrf(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('FLASK_PORT=8080\n')
    queue = Mock(return_value={'job': {'state': 'queued'}})
    monkeypatch.setattr(updates, 'queue', queue)
    rescue = create_recovery_app(Recovery(tmp_path)).test_client()
    page = rescue.get('/repair/')
    assert (
        b'System components' in page.data and b'Repair system components' in page.data
    )
    assert rescue.post('/repair/', data={'action': 'packages'}).status_code == 403
    queue.assert_not_called()
    with rescue.session_transaction() as session:
        token = session['csrf']
    assert (
        rescue.post('/repair/', data={'action': 'packages', 'csrf': token}).status_code
        == 200
    )
    queue.assert_called_once_with('packages', root=tmp_path)


def test_update_endpoint_refilters_stale_metadata_and_never_queues_downgrade(
    tmp_path, monkeypatch
):
    import threading

    policy = definitions(
        'webconfig/app/state.py',
        ['available_version'],
        {
            'VERSION_STATE_LOCK': threading.RLock(),
            'KNOWN_RELEASE_TAGS': (),
            'newest_release': newest_release,
            'release_version': release_version,
        },
    )
    queue = Mock(return_value={'job': {'id': 'abc', 'state': 'queued'}})
    monkeypatch.setattr(updates, 'queue', queue)
    app = Flask('channel-endpoint')
    bp = Blueprint('channel-endpoint', __name__)
    namespace = {
        'bp': bp,
        'jsonify': jsonify,
        'request': request,
        'PROJECT_ROOT': tmp_path,
        'load_versions': lambda: {'version': {'latest': 'v2.8.0-beta.1'}},
        'get_current_version': lambda: 'v2.7.0-RC3',
        'available_version': policy.available_version,
    }
    definitions(
        'webconfig/app/routes/system.py',
        ['_software_action', 'perform_update'],
        namespace,
    )
    app.register_blueprint(bp)
    client = app.test_client()
    for channel in ('stable', 'rc'):
        monkeypatch.setenv('UPDATE_CHANNEL', channel)
        assert (
            client.post('/update', headers={'X-Billy-Update': '1'}).json['status']
            == 'up-to-date'
        )
        queue.assert_not_called()
    monkeypatch.setenv('UPDATE_CHANNEL', 'beta')
    assert client.post('/update', headers={'X-Billy-Update': '1'}).status_code == 202
    queue.assert_called_once_with('update', 'v2.8.0-beta.1', root=tmp_path)
    queue.reset_mock()
    monkeypatch.setenv('UPDATE_CHANNEL', 'stable')
    namespace['load_versions'] = lambda: {'version': {'latest': 'v2.6.0'}}
    assert (
        client.post('/update', headers={'X-Billy-Update': '1'}).json['status']
        == 'up-to-date'
    )
    queue.assert_not_called()
