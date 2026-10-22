import json
import multiprocessing
import os
import sys
import time
from unittest.mock import patch

import pytest

from billy_runtime.configuration import (
    ConfigError,
    ConflictError,
    example_defaults,
    load_environment,
    patch_env,
    revision,
    save_env,
    validate,
)
from billy_runtime.recovery import Recovery
from billy_runtime.recovery_ui import create_recovery_app
from billy_runtime.storage import atomic_write, transaction


def writer(path, key):
    for i in range(10):
        patch_env(path, {key: i})


@pytest.fixture
def store(tmp_path):
    (tmp_path / '.env').write_text('FLASK_PORT=8080\nOPENAI_API_KEY=\n')
    result = Recovery(tmp_path)
    result.prepare()
    return result


@pytest.mark.parametrize(
    'text',
    [
        '',
        '# empty',
        'FLASK_PORT="oops',
        'FLASK_PORT=abc',
        'FLASK_PORT=0',
        'FLASK_PORT=99999',
        'AEC_ENABLED=yes',
        'CHUNK_MS=0',
        'MOUTH_BOOST=nan',
        'STATUS_LED_BRIGHTNESS=inf',
        'FOO',
        'A=1\nA=2',
        'DEFAULT_USER=../../etc',
        'CHUNK_MS=4.2',
        'MOCKFISH=whatever',
        'A=\x00',
    ],
)
def test_invalid_values_leave_live_untouched(store, text):
    before = store.env.read_bytes()
    with pytest.raises(ConfigError):
        save_env(store.env, text)
    assert store.env.read_bytes() == before


def test_unknown_keys_multiline_quoted_values_and_special_characters(store):
    content = (
        'FLASK_PORT=8080\nEXTENSION="line one\nline two"\nPASSWORD=\'hello # $x\'\n'
    )
    save_env(store.env, content)
    patch_env(store.env, {'PASSWORD': "new'\\secret\nmore", 'EXTENSION_2': 'yes'})
    values = validate(store.env.read_text())
    assert values['PASSWORD'] == "new'\\secret\nmore"
    assert values['EXTENSION'] == 'line one\nline two'
    assert values['EXTENSION_2'] == 'yes'


def test_concurrent_process_patches_preserve_each_other(store):
    context = multiprocessing.get_context('spawn')
    workers = [
        context.Process(target=writer, args=(store.env, key)) for key in ['A', 'B', 'C']
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    values = validate(store.env.read_text())
    assert [values[key] for key in ['A', 'B', 'C']] == ['9'] * 3


def test_atomic_failure_before_replace_keeps_original(store, monkeypatch):
    before = store.env.read_bytes()

    def fail(*args):
        raise OSError('full disk')

    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError):
        patch_env(store.env, {'FLASK_PORT': 8001})
    assert store.env.read_bytes() == before
    assert not list(store.root.glob('..env-*'))


def test_stale_editor_conflicts_and_permissions(store):
    old = revision(store.env.read_bytes())
    patch_env(store.env, {'FLASK_PORT': '8001'})
    with pytest.raises(ConflictError):
        save_env(store.env, 'FLASK_PORT=8002', expected=old)
    assert store.env.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700


def test_reload_removes_deleted_keys_but_restores_original_environment(
    store, monkeypatch
):
    import billy_runtime.configuration as module

    monkeypatch.setattr(module, '_original_environment', {'INHERITED': 'original'})
    monkeypatch.setattr(module, '_loaded_keys', set())
    monkeypatch.setenv('INHERITED', 'original')
    monkeypatch.delenv('ONLY_IN_FILE', raising=False)
    save_env(store.env, 'FLASK_PORT=8080\nINHERITED=fromfile\nONLY_IN_FILE=1')
    load_environment(store.env)
    assert os.environ['INHERITED'] == 'fromfile'
    save_env(store.env, 'FLASK_PORT=8080')
    load_environment(store.env)
    assert os.environ['INHERITED'] == 'original'
    assert 'ONLY_IN_FILE' not in os.environ


def promote(store, content):
    now = time.time()
    for role in ('core', 'web'):
        atomic_write(
            store.runtime / f'{role}.json',
            json.dumps({
                'identity': role + '-boot',
                'revision': revision(content),
                'healthy': True,
                'effective': 'same',
                'seen': now,
                'since': now - 31,
            }),
        )
    with transaction(store.env):
        store._promote(content, now)


def test_snapshot_only_after_same_revision_probation(store):
    content = store.env.read_bytes()
    store.report('core', 'c1', content, healthy=True, effective='x')
    store.report('web', 'w1', content, healthy=True, effective='x')
    assert not store.backups()
    promote(store, content)
    assert store.backups()[0][2] == content
    assert store.read_state()['good'] == revision(content)


@pytest.mark.parametrize(
    'alteration',
    ['stale', 'unhealthy', 'other-revision', 'other-environment', 'new-process'],
)
def test_bad_readiness_cannot_promote(store, alteration):
    content = store.env.read_bytes()
    promote(store, content)
    # Remove snapshots so we can check that bad reports don't recreate one.
    for path in store.directory.glob('good-*'):
        path.unlink()
    store.state(good=None)
    path = store.runtime / 'core.json'
    report = json.loads(path.read_text())
    if alteration == 'stale':
        report['seen'] -= 60
    if alteration == 'unhealthy':
        report['healthy'] = False
    if alteration == 'other-revision':
        report['revision'] = 'old'
    if alteration == 'other-environment':
        report['effective'] = 'other'
    if alteration == 'new-process':
        report['since'] = time.time()
    atomic_write(path, json.dumps(report))
    store._promote(content, time.time())
    assert not store.backups()


def test_corruption_restores_good_snapshot_preserves_bad_original_and_profiles(store):
    good = store.env.read_bytes()
    promote(store, good)
    profile = store.root / 'profiles' / 'test.ini'
    atomic_write(profile, '[USER_INFO]\nname=test')
    bad = b'FLASK_PORT="broken\xff'
    store.env.write_bytes(bad)
    loaded, allowed = store.prepare()
    assert allowed and loaded == good
    state = store.read_state()
    assert (store.directory / state['original']).read_bytes() == bad
    assert profile.read_text() == '[USER_INFO]\nname=test'


def test_empty_example_missing_example_and_no_backup_are_recovery_only(tmp_path):
    store = Recovery(tmp_path)
    (tmp_path / '.env.example').write_text('#OPENAI_API_KEY=not-a-real-key\n')
    content, allowed = store.prepare()
    assert not allowed
    assert 'not-a-real-key' not in validate(content.decode()).values()
    assert store.read_state()['needs_setup']
    assert validate(example_defaults(tmp_path / 'missing'), complete=True)


def test_good_config_crash_never_rolls_back_and_pending_rolls_back_once(store):
    good = store.env.read_bytes()
    promote(store, good)
    assert not store.failed('core', good)
    assert not store.failed('core', good)
    save_env(store.env, 'FLASK_PORT=8081')
    pending = store.env.read_bytes()
    assert not store.failed('core', pending)
    assert store.failed('core', pending)
    assert store.env.read_bytes() == good
    save_env(store.env, 'FLASK_PORT=8082')
    other = store.env.read_bytes()
    assert not store.failed('core', other)
    assert not store.failed('core', other)


def test_late_failure_does_not_overwrite_newer_edit(store):
    good = store.env.read_bytes()
    promote(store, good)
    save_env(store.env, 'FLASK_PORT=8081')
    pending = store.env.read_bytes()
    store.failed('web', pending)
    save_env(store.env, 'FLASK_PORT=8082')
    assert not store.failed('web', pending)
    assert store.env.read_text() == 'FLASK_PORT=8082'


def test_recovery_ui_no_core_imports_no_login_csrf_and_conflict(store):
    before = set(sys.modules)
    client = create_recovery_app(store).test_client()
    assert not any(
        name == 'core' or name.startswith('core.') for name in set(sys.modules) - before
    )
    assert client.get('/').status_code == 200
    assert 'Unlock recovery' not in client.get('/').text
    assert client.post('/', data={'token': 'obsolete'}).status_code == 403
    with client.session_transaction() as session:
        csrf = session['csrf']
    assert 'OPENAI_API_KEY' in client.get('/').text
    assert client.get('/?file=/etc/passwd').status_code == 404
    assert client.post('/', data={'action': 'save'}).status_code == 403
    digest = revision(store.env.read_bytes())
    response = client.post(
        '/',
        data={
            'action': 'save',
            'csrf': csrf,
            'file': '.env',
            'revision': digest,
            'content': 'FLASK_PORT=broken',
        },
    )
    assert 'Invalid value' in response.text
    assert revision(store.env.read_bytes()) == digest
    patch_env(store.env, {'FLASK_PORT': 8082})
    response = client.post(
        '/',
        data={
            'action': 'save',
            'csrf': csrf,
            'file': '.env',
            'revision': digest,
            'content': 'FLASK_PORT=8083',
        },
    )
    assert 'Configuration changed' in response.text
    assert validate(store.env.read_text())['FLASK_PORT'] == '8082'
    assert response.headers['Cache-Control'] == 'no-store'


def test_recovery_ui_can_repair_profile_without_loading_it(store):
    path = store.root / 'profiles' / 'test.ini'
    atomic_write(path, 'corrupt')
    client = create_recovery_app(store).test_client()
    client.get('/repair/')
    with client.session_transaction() as session:
        csrf = session['csrf']
    response = client.post(
        '/',
        data={
            'action': 'save',
            'csrf': csrf,
            'file': 'profiles/test.ini',
            'revision': revision(path.read_bytes()),
            'content': '[USER_INFO]\nname=test',
        },
    )
    assert 'Saved.' in response.text
    assert path.read_text().startswith('[USER_INFO]')


def test_unwritable_storage_does_not_prevent_opening_recovery(store, monkeypatch):
    def fail():
        raise PermissionError('read-only storage')

    monkeypatch.setattr(store, 'initialize', fail)
    # Old corrupt access codes must not lock out a browser either.
    (store.directory / 'access-token').write_text('corrupt')
    client = create_recovery_app(store).test_client()
    response = client.get('/settings')
    assert response.status_code == 200
    assert 'OPENAI_API_KEY' in response.text
    assert 'Unlock recovery' not in response.text
    with client.session_transaction() as session:
        csrf = session['csrf']
    response = client.post('/', data={'action': 'retry', 'csrf': csrf})
    assert response.status_code == 200
    assert 'Operation failed (PermissionError)' in response.text


@pytest.mark.parametrize('url', ['/', '/settings', '/repair/', '/recovery'])
def test_fresh_browser_has_recovery_without_code_or_saved_cookie(store, url):
    client = create_recovery_app(store).test_client()
    response = client.get(url)
    assert response.status_code == 200
    assert 'Software recovery' in response.text
    assert 'OPENAI_API_KEY' in response.text
    assert 'Unlock recovery' not in response.text
    assert not (store.directory / 'access-token').exists()
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert 'SameSite=Strict' in response.headers['Set-Cookie']
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Frame-Options'] == 'DENY'


@pytest.mark.parametrize('token', ['', 'invalid', 'wrong', '🌍'])
def test_recovery_actions_reject_missing_or_incorrect_csrf(store, token):
    client = create_recovery_app(store).test_client()
    before = store.read_state()
    assert client.post('/', data={'action': 'retry', 'csrf': token}).status_code == 403
    client.get('/')
    assert client.post('/', data={'action': 'retry', 'csrf': token}).status_code == 403
    assert store.read_state() == before


@pytest.mark.parametrize(
    'headers',
    [
        {'Origin': 'http://other.example'},
        {'Origin': 'null'},
        {'Origin': 'http://localhost:invalid'},
        {'Sec-Fetch-Site': 'cross-site'},
    ],
)
def test_recovery_rejects_cross_site_mutations_even_with_csrf(store, headers):
    client = create_recovery_app(store).test_client()
    client.get('/')
    with client.session_transaction() as session:
        csrf = session['csrf']
    before = store.read_state()
    assert (
        client.post(
            '/', data={'action': 'retry', 'csrf': csrf}, headers=headers
        ).status_code
        == 403
    )
    assert store.read_state() == before


def test_recovery_allows_documentation_link_and_same_origin_save(store):
    client = create_recovery_app(store).test_client()
    response = client.get(
        '/repair/',
        headers={'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate'},
    )
    assert response.status_code == 200
    with client.session_transaction() as session:
        csrf = session['csrf']
    response = client.post(
        '/repair/',
        headers={'Origin': 'http://localhost:80'},
        data={
            'action': 'save',
            'csrf': csrf,
            'file': '.env',
            'revision': revision(store.env.read_bytes()),
            'content': 'FLASK_PORT=8081\n',
        },
    )
    assert 'Saved.' in response.text
    assert store.env.read_text() == 'FLASK_PORT=8081\n'


def test_recovery_get_cannot_execute_action(store):
    client = create_recovery_app(store).test_client()
    before = store.read_state()
    assert client.get('/', data={'action': 'retry'}).status_code == 200
    assert store.read_state() == before


def test_recovery_restart_requires_only_page_reload_not_access_code(store):
    first = create_recovery_app(store).test_client()
    first.get('/')
    with first.session_transaction() as session:
        csrf = session['csrf']
    cookie = first.get_cookie('billy_recovery').value
    second = create_recovery_app(store).test_client()
    second.set_cookie('billy_recovery', cookie)
    assert second.post('/', data={'action': 'retry', 'csrf': csrf}).status_code == 403
    assert second.get('/repair/').status_code == 200
    with second.session_transaction() as session:
        csrf = session['csrf']
    response = second.post('/', data={'action': 'retry', 'csrf': csrf})
    assert 'Startup requested.' in response.text


@pytest.mark.parametrize(
    'ip,port', [('10.42.0.1', 80), ('192.168.4.1', 80), ('10.42.0.1', 8081)]
)
def test_hotspot_probes_redirect_to_recovery_on_actual_interface(
    store, monkeypatch, ip, port
):
    from types import SimpleNamespace

    import billy_runtime.recovery_ui as recovery_ui

    (store.root / 'setup').mkdir(exist_ok=True)
    (store.root / 'setup/.wifi_onboarding_active').touch()
    store.state(listen_port=port)
    calls = []

    def address_lookup(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps([{'addr_info': [{'local': ip}]}]))

    monkeypatch.setattr(recovery_ui.subprocess, 'run', address_lookup)
    client = create_recovery_app(store).test_client()
    authority = ip if port == 80 else f'{ip}:{port}'
    for path in [
        '/generate_204',
        '/hotspot-detect.html',
        '/connecttest.txt',
        '/unknown-probe',
    ]:
        response = client.get(path, base_url='http://probe.example')
        assert response.status_code == 302
        assert response.headers['Location'] == f'http://{authority}/repair/'
    assert all(
        command == ['ip', '-4', '-j', 'address', 'show', 'dev', 'wlan0']
        for command in calls
    )
    response = client.get('/repair/', base_url=f'http://{authority}')
    assert response.status_code == 200
    assert 'Software recovery' in response.text
    assert client.get('/health').json['recovery'] is True
    assert client.get('/static/missing.js').status_code == 404
    assert client.get('/api/missing').status_code == 404


def test_no_hotspot_does_not_hijack_connectivity_probes(store):
    client = create_recovery_app(store).test_client()
    assert client.get('/generate_204').status_code == 404
    assert client.get('/connecttest.txt').status_code == 404


def test_mid_write_failure_and_fsync_failure_never_leave_partial_file(
    store, monkeypatch
):
    before = store.env.read_bytes()

    def fail(*args):
        raise OSError('disk full during sync')

    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError):
        save_env(store.env, 'FLASK_PORT=8081\n')
    assert store.env.read_bytes() == before
    assert not list(store.root.glob('..env-*'))


def test_corrupt_newest_backup_falls_back_to_older_valid_generation(store):
    first = store.env.read_bytes()
    promote(store, first)
    save_env(store.env, 'FLASK_PORT=8081\n')
    second = store.env.read_bytes()
    promote(store, second)
    (store.directory / f'good-{revision(second)}.env').write_bytes(b'corrupt')
    store.env.write_bytes(b'FLASK_PORT="broken')
    content, allowed = store.prepare()
    assert allowed and content == first


def test_only_five_good_generations_retained_and_newer_edit_not_promoted(store):
    for port in range(8081, 8088):
        save_env(store.env, f'FLASK_PORT={port}\n')
        promote(store, store.env.read_bytes())
    assert len(store.backups()) == 5
    previous = store.env.read_bytes()
    save_env(store.env, 'FLASK_PORT=9000\n')
    store._promote(previous, time.time())
    assert store.read_state()['good'] == revision(previous)
    assert not any(b[1] == revision(store.env.read_bytes()) for b in store.backups())


def test_fallback_keeps_last_known_web_port_without_good_core_snapshot(store):
    store.state(listen_port=12345)
    store.env.write_bytes(b'FLASK_PORT=bad')
    content, allowed = store.prepare()
    assert not allowed and validate(content.decode())['FLASK_PORT'] == '12345'


def test_removed_boot_setting_does_not_linger_after_first_reload(store, monkeypatch):
    import billy_runtime.configuration as module

    monkeypatch.setattr(module, '_original_environment', {})
    monkeypatch.setattr(module, '_loaded_keys', set())
    monkeypatch.delenv('BOOT_ONLY', raising=False)
    module.apply_environment('FLASK_PORT=8080\nBOOT_ONLY=1')
    load_environment(store.env)
    assert 'BOOT_ONLY' not in os.environ


def test_failed_edit_keeps_user_input_in_recovery_form(store):
    client = create_recovery_app(store).test_client()
    client.get('/repair/')
    with client.session_transaction() as session:
        csrf = session['csrf']
    response = client.post(
        '/',
        data={
            'action': 'save',
            'csrf': csrf,
            'file': '.env',
            'revision': revision(store.env.read_bytes()),
            'content': 'FLASK_PORT=bad\n# keep my notes',
        },
    )
    assert '# keep my notes' in response.text
    assert 'FLASK_PORT=8080' in store.env.read_text()


def test_bad_backup_metadata_does_not_hide_valid_older_backup(store):
    first = store.env.read_bytes()
    promote(store, first)
    save_env(store.env, 'FLASK_PORT=8081\n')
    second = store.env.read_bytes()
    promote(store, second)
    metadata = store.directory / f'good-{revision(second)}.json'
    metadata.write_text(
        json.dumps({'revision': revision(second), 'validated_at': {'bad': 'type'}})
    )
    assert store.backups()[0][2] == first


def test_lost_pointer_does_not_rollback_previously_validated_revision(store):
    first = store.env.read_bytes()
    promote(store, first)
    save_env(store.env, 'FLASK_PORT=8081\n')
    second = store.env.read_bytes()
    promote(store, second)
    (store.directory / 'state.json').write_text('corrupt metadata')
    assert not store.failed('core', second)
    assert not store.failed('core', second)
    assert store.env.read_bytes() == second


def interrupted_writer(path, ready, after_replace):
    original = os.replace

    def pause(source, target):
        if after_replace:
            original(source, target)
        ready.set()
        time.sleep(60)

    os.replace = pause
    patch_env(path, {'FLASK_PORT': '8081'})


@pytest.mark.parametrize('after_replace', [False, True])
def test_killed_writer_leaves_whole_file_and_releases_lock(store, after_replace):
    before = store.env.read_bytes()
    context = multiprocessing.get_context('spawn')
    ready = context.Event()
    process = context.Process(
        target=interrupted_writer, args=(store.env, ready, after_replace)
    )
    process.start()
    try:
        assert ready.wait(5)
        process.kill()
        process.join(5)
        current = store.env.read_bytes()
        assert validate(current.decode())['FLASK_PORT'] == (
            '8081' if after_replace else '8080'
        )
        if not after_replace:
            assert current == before
        patch_env(store.env, {'FLASK_PORT': '8082'})
        assert validate(store.env.read_text())['FLASK_PORT'] == '8082'
        assert not store.backups()
    finally:
        if process.is_alive():
            process.kill()
            process.join()


def test_healthy_recovery_allows_later_independent_update_rollback(store):
    good = store.env.read_bytes()
    promote(store, good)
    save_env(store.env, 'FLASK_PORT=8081')
    first = store.env.read_bytes()
    store.failed('core', first)
    assert store.failed('core', first)
    promote(store, good)
    assert not store.read_state()['rollback_used']
    save_env(store.env, 'FLASK_PORT=8082')
    second = store.env.read_bytes()
    store.failed('core', second)
    assert store.failed('core', second)


@pytest.mark.parametrize('mode', ['restored', 'probation'])
def test_same_good_revision_finishes_recovery_without_rewriting_backup(store, mode):
    content = store.env.read_bytes()
    promote(store, content)
    backup = store.directory / f'good-{revision(content)}.json'
    before = backup.read_bytes()
    original = store._quarantine(b'FLASK_PORT=bad')
    store.state(
        mode=mode,
        reason='Invalid configuration (ConfigError); original preserved',
        original=original,
        failures={'core:test': 1},
    )
    promote(store, content)
    state = store.read_state()
    assert state['mode'] == 'healthy'
    assert state['failures'] == {}
    assert state['original'] == original
    assert (store.directory / original).read_bytes() == b'FLASK_PORT=bad'
    assert backup.read_bytes() == before
    with patch.object(store, 'state', wraps=store.state) as write_state:
        promote(store, content)
        write_state.assert_not_called()


@pytest.mark.parametrize(
    'problem',
    ['unhealthy', 'probation', 'stale', 'different-config', 'different-environment'],
)
def test_same_good_recovery_notice_remains_until_both_services_are_healthy(
    store, problem
):
    content = store.env.read_bytes()
    promote(store, content)
    store.state(mode='restored')
    path = store.runtime / 'core.json'
    report = json.loads(path.read_text())
    if problem == 'unhealthy':
        report['healthy'] = False
    if problem == 'probation':
        report['since'] = time.time()
    if problem == 'stale':
        report['seen'] = time.time() - 20
    if problem == 'different-config':
        report['revision'] = 'other'
    if problem == 'different-environment':
        report['effective'] = 'other'
    atomic_write(path, json.dumps(report))
    store._promote(content, time.time())
    assert store.read_state()['mode'] == 'restored'


def test_corrupt_config_restored_to_same_good_clears_notice_after_health_checks(store):
    content = store.env.read_bytes()
    promote(store, content)
    store.env.write_text('FLASK_PORT=bad')
    restored, allowed = store.prepare()
    assert restored == content and allowed
    assert store.read_state()['mode'] == 'restored'
    promote(store, restored)
    assert store.read_state()['mode'] == 'healthy'


def test_live_notice_clears_only_after_joint_health_and_contains_no_configuration(
    store,
):
    content = store.env.read_bytes()
    store.state(mode='restored', reason='Configuration restored')
    assert store.notice() == {
        'active': True,
        'kind': 'configuration',
        'message': 'Configuration restored',
    }
    promote(store, content)
    assert store.notice() == {'active': False, 'kind': None, 'message': ''}
    atomic_write(store.directory / 'runtime-error', 'Core stopped responding')
    assert store.notice()['active'] is True
    assert 'OPENAI_API_KEY' not in json.dumps(store.notice())
    promote(store, content)
    assert store.notice()['active'] is False


def test_env_history_inspection_preserves_active_file_and_uses_active_revision(store):
    old = store.env.read_bytes()
    promote(store, old)
    save_env(store.env, 'FLASK_PORT=8081\n')
    current = store.env.read_bytes()
    original = store._quarantine(b'FLASK_PORT=broken')
    files = store.environment_files()
    assert [f['kind'] for f in files] == ['current', 'backup', 'original']
    selected, digest = store.environment_content(files[1]['name'])
    assert selected == old and digest == revision(current)
    assert store.environment_content(original)[0] == b'FLASK_PORT=broken'
    assert store.env.read_bytes() == current
    assert 'OPENAI_API_KEY' not in json.dumps(files)


@pytest.mark.parametrize(
    'source', ['../.env', '/etc/passwd', 'access-token', 'good-missing.env']
)
def test_history_rejects_unlisted_paths(store, source):
    with pytest.raises(ValueError):
        store.environment_content(source)


def test_history_excludes_symlink_sources(store, tmp_path):
    outside = tmp_path / 'private'
    outside.write_text('FLASK_PORT=8082')
    (store.directory / 'corrupt-link.env').symlink_to(outside)
    assert not any(f['name'] == 'corrupt-link.env' for f in store.environment_files())
    with pytest.raises(ValueError):
        store.environment_content('corrupt-link.env')


def test_editor_can_create_missing_current_config(store):
    store.env.unlink()
    assert store.environment_content() == (b'', revision(b''))
    save_env(store.env, 'FLASK_PORT=8080', expected=revision(b''))
    assert store.env.exists()


def test_runtime_failure_does_not_point_to_configuration_editor(store):
    store.state(mode='restored', reason='Configuration restored')
    atomic_write(
        store.directory / 'runtime-error', 'core: Worker exited before becoming healthy'
    )
    notice = store.notice()
    assert notice['active'] and notice['kind'] == 'runtime'
    assert 'Worker exited' in notice['message']


def test_pending_startup_has_no_configuration_repair_action(store):
    store.state(mode='probation', reason='Old configuration error')
    assert store.notice() == {
        'active': True,
        'kind': 'startup',
        'message': 'Checking Billy and the web interface after startup.',
    }


def test_invalid_configuration_has_configuration_action(store):
    store.env.write_text('FLASK_PORT=broken')
    store.prepare()
    assert store.notice()['kind'] == 'configuration'
