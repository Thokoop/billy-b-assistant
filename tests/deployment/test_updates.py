import hashlib
import io
import json
import subprocess
import tarfile
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from billy_runtime import updates
from billy_runtime.storage import runtime_directory


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def candidate(root, identity='a' * 32):
    folder = updates.directory(root) / 'releases' / identity
    source = folder / 'source'
    environment = folder / 'environments/test'
    (source / 'dependencies').mkdir(parents=True)
    lock = source / 'dependencies/test.lock'
    lock.write_text('locked')
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    (environment / 'bin').mkdir(parents=True)
    (environment / 'bin/python').write_text('interpreter')
    (environment / '.complete').write_text(digest)
    write_json(
        environment / 'manifest.json', {'lock': 'test.lock', 'lock_sha256': digest}
    )
    record = {
        'id': identity,
        'environment': str(environment),
        'version': 'v2',
        'commit': 'a' * 40,
    }
    write_json(folder / 'release.json', record)
    return record


@pytest.fixture
def root(tmp_path):
    (tmp_path / 'venv/bin').mkdir(parents=True)
    (tmp_path / 'venv/bin/python').write_text('original interpreter')
    (tmp_path / '.env').write_text('FLASK_PORT=8080\n')
    (tmp_path / 'persona.ini').write_text('original persona')
    (tmp_path / 'profiles').mkdir()
    (tmp_path / 'profiles/owner.ini').write_text('private memory')
    return tmp_path


def queued(root, action='update', **extras):
    work = {
        'id': 'job1',
        'state': 'queued',
        'action': action,
        'version': 'v2',
        **extras,
    }
    write_json(updates.directory(root) / 'job.json', work)
    return work


def test_queue_is_serialized_and_does_not_accept_shell_refs(root, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(updates, 'dispatch', dispatch)
    with pytest.raises(ValueError):
        updates.queue('update', '--help', root)
    with pytest.raises(ValueError):
        updates.queue('update', 'v1;bad', root)
    result = updates.queue('update', 'v1.2.0-rc.1', root)
    assert result['job']['state'] == 'queued'
    with pytest.raises(RuntimeError):
        updates.queue('reinstall', root=root)
    assert dispatch.call_count == 1


def test_dispatch_failure_keeps_current_software(root, monkeypatch):
    monkeypatch.setattr(updates, 'dispatch', Mock(side_effect=OSError('no systemd')))
    with pytest.raises(RuntimeError):
        updates.queue('reinstall', root=root)
    assert updates.job(root)['state'] == 'failed'
    assert updates.selection(root)[0]['id'] == 'baseline'


def test_new_release_activates_only_after_preparation_and_health(root, monkeypatch):
    record = candidate(root)
    queued(root)

    def prepare(*_):
        assert updates.selection(root)[0]['id'] == 'baseline'
        return record

    monkeypatch.setattr(updates, 'prepare_candidate', prepare)
    monkeypatch.setattr(updates, 'command', Mock())

    def healthy(*args):
        assert updates.state(root)['trial']
        assert updates.state(root)['current'] == record
        return True

    monkeypatch.setattr(updates, 'wait_healthy', healthy)
    updates.execute(root, 'job1')
    assert updates.job(root)['state'] == 'complete'
    assert updates.state(root)['previous']['id'] == 'baseline'
    assert not updates.state(root)['trial']
    assert (root / '.env').read_text() == 'FLASK_PORT=8080\n'
    assert (root / 'profiles/owner.ini').read_text() == 'private memory'
    assert (root / 'venv/bin/python').read_text() == 'original interpreter'


@pytest.mark.parametrize(
    'error',
    [
        OSError('disk full'),
        ValueError('bad hash'),
        subprocess.CalledProcessError(1, 'pip'),
    ],
)
def test_failed_preparation_does_not_switch_or_restart(root, monkeypatch, error):
    queued(root)
    monkeypatch.setattr(updates, 'prepare_candidate', Mock(side_effect=error))
    start = Mock()
    monkeypatch.setattr(updates, 'command', start)
    updates.execute(root, 'job1')
    assert updates.job(root)['state'] == 'failed'
    assert updates.selection(root)[0]['id'] == 'baseline'
    start.assert_not_called()


def test_failed_startup_rolls_back_without_config_rollback(root, monkeypatch):
    record = candidate(root)
    queued(root)
    monkeypatch.setattr(updates, 'prepare_candidate', lambda *_: record)
    monkeypatch.setattr(updates, 'command', Mock())
    monkeypatch.setattr(updates, 'wait_healthy', lambda *_: False)
    updates.execute(root, 'job1')
    assert updates.job(root)['state'] == 'rolled_back'
    assert updates.state(root)['current']['id'] == 'baseline'
    assert (root / 'persona.ini').read_text() == 'original persona'


@pytest.mark.parametrize('phase', ['activating', 'validating', 'rolling_back'])
def test_power_loss_during_switch_restores_previous_idempotently(root, phase):
    record = candidate(root)
    before = updates.baseline(root)
    updates.activate(root, record, before, trial=True)
    queued(root, state=phase, before=before, old_previous=None, candidate=record)
    updates.execute(root, 'job1')
    selected = updates.state(root)
    assert selected['current'] == before
    assert updates.job(root)['state'] == 'rolled_back'
    updates.execute(root, 'job1')
    assert updates.state(root) == selected


def test_wrong_job_id_cannot_execute_another_operation(root, monkeypatch):
    queued(root)
    prepare = Mock()
    monkeypatch.setattr(updates, 'prepare_candidate', prepare)
    updates.execute(root, 'old-job')
    prepare.assert_not_called()


def test_manual_rollback_keeps_newer_release_as_reverse_target(root, monkeypatch):
    record = candidate(root)
    before = updates.baseline(root)
    updates.activate(root, record, before, trial=False)
    queued(root, action='rollback')
    monkeypatch.setattr(updates, 'command', Mock())
    monkeypatch.setattr(updates, 'wait_healthy', lambda *_: True)
    updates.execute(root, 'job1')
    assert updates.state(root)['current'] == before
    assert updates.state(root)['previous'] == record


def test_health_requires_current_software_and_fresh_both_services(root):
    expected = 'generation:release'
    now = time.time()
    report = {
        'software': expected,
        'healthy': True,
        'revision': hashlib.sha256((root / '.env').read_bytes()).hexdigest(),
        'effective': 'same',
        'seen': now,
        'since': now - 31,
    }
    for role in ('core', 'web'):
        write_json(runtime_directory(root) / f'{role}.json', report)
    assert updates.healthy(root, expected, now=now)
    for change in (
        {'software': 'old'},
        {'seen': now - 9},
        {'healthy': False},
        {'since': now - 1},
        {'effective': 'different'},
    ):
        write_json(runtime_directory(root) / 'web.json', report | change)
        assert not updates.healthy(root, expected, now=now)


def test_damaged_candidate_falls_back_to_original_for_status(root):
    record = candidate(root)
    updates.activate(root, record, None, trial=True)
    (Path(record['environment']) / '.complete').unlink()
    assert updates.selection(root)[0]['id'] == 'baseline'
    with pytest.raises(OSError):
        updates.resolve(record, root)


def test_pruning_retains_active_previous_and_never_follows_shared_data(root):
    first = candidate(root, 'a' * 32)
    second = candidate(root, 'b' * 32)
    unused = candidate(root, 'c' * 32)
    updates.share_data(updates.resolve(unused, root)[0], root)
    updates.activate(root, first, second, trial=False)
    updates.prune(root)
    assert updates.resolve(first, root) and updates.resolve(second, root)
    assert not (updates.directory(root) / 'releases' / unused['id']).exists()
    assert (root / 'profiles/owner.ini').read_text() == 'private memory'


@pytest.mark.parametrize(
    'name,kind', [('../escape', 'file'), ('/tmp/escape', 'file'), ('safe', 'symlink')]
)
def test_archives_cannot_escape_the_candidate(root, name, kind):
    archive = root / 'archive.tar'
    with tarfile.open(archive, 'w') as bundle:
        member = tarfile.TarInfo(name)
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = str(root)
        else:
            member.size = 1
        bundle.addfile(member, io.BytesIO(b'x') if kind == 'file' else None)
    destination = root / 'extracted'
    destination.mkdir()
    with pytest.raises(ValueError):
        updates.extract(archive, destination)


def test_systemd_job_runs_unprivileged_in_separate_unit(root, monkeypatch):
    run = Mock()
    monkeypatch.setattr(updates.subprocess, 'run', run)
    updates.dispatch(root, 'a' * 32)
    args = run.call_args.args[0]
    assert args[:3] == ['sudo', '-n', 'systemd-run']
    assert any(arg.startswith('--uid=') for arg in args)
    assert '--property=Restart=on-failure' in args
    assert str(root / 'venv/bin/python') in args
    assert 'pip' not in args and 'checkout' not in args


def test_dispatch_timeout_does_not_cancel_an_accepted_job(root, monkeypatch):
    monkeypatch.setattr(
        updates,
        'dispatch',
        Mock(side_effect=subprocess.TimeoutExpired('systemd-run', 15)),
    )
    assert updates.queue('reinstall', root=root)['job']['state'] == 'queued'


def test_damaged_rollback_target_selects_original_installation(root):
    record = candidate(root)
    updates.activate(root, record, None, trial=True)
    queued(root, state='validating', before=record)
    (Path(record['environment']) / '.complete').unlink()
    updates.execute(root, 'job1')
    assert updates.state(root)['current']['id'] == 'baseline'
    assert updates.job(root)['state'] == 'rolled_back'


@pytest.fixture
def archived_release(root, monkeypatch):
    from types import SimpleNamespace

    from billy_runtime import environments

    files = {
        'dependencies/deployment.json': json.dumps({
            'supervisor_protocol': 1,
            'shared_data_schema': 1,
        }),
        'dependencies/system-packages.json': '{"schema":1}',
        'dependencies/test.lock': 'pinned',
        'profiles/owner.ini': 'shipped default',
        'profiles/new.ini': 'new profile',
        'main.py': 'new source',
    }
    for name in (
        'install-bluetooth.sh',
        '51-billy-bluetooth.conf',
        '51-billy-bluetooth.lua',
    ):
        path = 'setup/system/' + name
        files[path] = 'same OS asset'
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(files[path])
    calls = []

    def command(args, cwd, **kwargs):
        calls.append(args)
        if 'rev-parse' in args:
            return 'a' * 40 + '\n'
        if 'archive' in args:
            with tarfile.open(args[4], 'w') as archive:
                for name, content in files.items():
                    data = content.encode()
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
        return None

    def prepare(release, *, source, directory):
        assert not updates.state(root)
        assert (source / 'main.py').read_text() == 'new source'
        environment = directory / 'new'
        (environment / 'bin').mkdir(parents=True)
        (environment / 'bin/python').write_text('new interpreter')
        digest = hashlib.sha256(
            (source / 'dependencies/test.lock').read_bytes()
        ).hexdigest()
        (environment / '.complete').write_text(digest)
        write_json(
            environment / 'manifest.json', {'lock': 'test.lock', 'lock_sha256': digest}
        )
        return environment

    from billy_runtime import system_packages

    def install(source, *args, **kwargs):
        if (
            json.loads((source / 'dependencies/system-packages.json').read_text()).get(
                'schema'
            )
            != 1
        ):
            raise ValueError('Unsupported system manifest')

    monkeypatch.setattr(system_packages, 'ensure', install)
    monkeypatch.setattr(updates, 'command', command)
    monkeypatch.setattr(
        updates.shutil, 'disk_usage', lambda _: SimpleNamespace(free=20 * 1024**3)
    )
    monkeypatch.setattr(environments, 'prepare', prepare)
    return files, calls


def test_prepare_combines_archived_code_and_isolated_environment_preserving_data(
    root, archived_release
):
    files, calls = archived_release
    record = updates.prepare_candidate(root, {'action': 'update', 'version': 'v2'})
    source, python = updates.resolve(record, root)
    assert python.read_text() == 'new interpreter'
    assert (source / 'profiles/owner.ini').read_text() == 'private memory'
    assert (root / 'profiles/new.ini').read_text() == 'new profile'
    assert not updates.state(root)
    assert (root / 'venv/bin/python').read_text() == 'original interpreter'
    assert ['git', 'fetch', 'origin', '--tags'] in calls
    assert all('checkout' not in c and 'reset' not in c for c in calls)


@pytest.mark.parametrize(
    'change', ['protocol', 'os-assets', 'system-packages', 'dependency-failure']
)
def test_incompatible_or_failed_candidate_is_removed_without_switch(
    root, archived_release, monkeypatch, change
):
    from billy_runtime import environments

    files, _ = archived_release
    if change == 'protocol':
        files['dependencies/deployment.json'] = '{}'
    elif change == 'os-assets':
        files['setup/system/install-bluetooth.sh'] = 'new system changes'
    elif change == 'system-packages':
        files['dependencies/system-packages.json'] = '{"schema":2}'
    else:
        monkeypatch.setattr(
            environments, 'prepare', Mock(side_effect=RuntimeError('failed build'))
        )
    with pytest.raises((ValueError, RuntimeError)):
        updates.prepare_candidate(root, {'action': 'update', 'version': 'v2'})
    assert not list((updates.directory(root) / 'releases').glob('*'))
    assert not updates.state(root)
    assert (root / 'profiles/owner.ini').read_text() == 'private memory'


@pytest.mark.parametrize('failure', ['offline', 'dirty'])
def test_fetch_and_dirty_checkout_fail_before_allocating_candidate(
    root, archived_release, monkeypatch, failure
):
    previous = updates.command

    def command(args, cwd, **kw):
        if ('fetch' if failure == 'offline' else 'diff') in args:
            raise subprocess.CalledProcessError(1, args)
        return previous(args, cwd, **kw)

    from billy_runtime import system_packages

    def install(source, *args, **kwargs):
        if (
            json.loads((source / 'dependencies/system-packages.json').read_text()).get(
                'schema'
            )
            != 1
        ):
            raise ValueError('Unsupported system manifest')

    monkeypatch.setattr(system_packages, 'ensure', install)
    monkeypatch.setattr(updates, 'command', command)
    with pytest.raises(subprocess.CalledProcessError):
        updates.prepare_candidate(root, {'action': 'update', 'version': 'v2'})
    assert not (updates.directory(root) / 'releases').exists()


def test_failed_atomic_selection_write_preserves_previous_pointer(root, monkeypatch):
    before = updates.baseline(root)
    updates.activate(root, before, None, trial=False)
    original = updates.state(root)
    record = candidate(root)
    monkeypatch.setattr(updates, 'atomic_write', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        updates.activate(root, record, before, trial=True)
    assert updates.state(root) == original


@pytest.mark.parametrize(
    'record', [{'id': None}, {'id': 12}, {'id': 'a' * 32, 'environment': {}}, ['wrong']]
)
def test_corrupt_selection_fields_do_not_break_status_or_recovery(root, record):
    write_json(updates.directory(root) / 'selection.json', {'current': record})
    assert updates.status(root)['current'] == 'original installation'
    assert isinstance(updates.generation(root), str)


def test_status_names_both_versions_including_original_installation(root, monkeypatch):
    monkeypatch.setattr(updates, 'original_version', lambda _: 'v2.5.1')
    assert updates.status(root)['current'] == 'v2.5.1'
    assert updates.status(root)['previous'] is None
    record = candidate(root)
    updates.activate(root, record, updates.baseline(root), trial=False)
    result = updates.status(root)
    assert result['current'] == 'v2'
    assert result['previous'] == 'v2.5.1'
    assert result['rollback_available']


def test_original_version_uses_commit_when_no_exact_tag(root, monkeypatch):
    updates.original_version.cache_clear()
    check = Mock(side_effect=[subprocess.CalledProcessError(1, 'git'), 'abc1234\n'])
    monkeypatch.setattr(updates.subprocess, 'check_output', check)
    assert updates.original_version(str(root)) == 'abc1234'
    assert updates.original_version(str(root)) == 'abc1234'
    assert check.call_count == 2
    updates.original_version.cache_clear()


def test_package_repair_uses_existing_selection_without_activation(root, monkeypatch):
    queued(root, action='packages')
    install = Mock()
    monkeypatch.setattr(updates, 'install_components', install)
    activate = Mock()
    monkeypatch.setattr(updates, 'activate', activate)
    updates.execute(root, 'job1')
    install.assert_called_once_with(root, root, repair=True)
    activate.assert_not_called()
    assert updates.job(root)['state'] == 'complete'
    assert (root / '.env').read_text() == 'FLASK_PORT=8080\n'


def test_reboot_during_package_apply_requires_repair_without_activating(
    root, monkeypatch
):
    from billy_runtime import system_packages

    queued(root, state='installing_packages')
    recover = Mock()
    monkeypatch.setattr(system_packages, 'interrupted', recover)
    activate = Mock()
    monkeypatch.setattr(updates, 'activate', activate)
    updates.execute(root, 'job1')
    recover.assert_called_once_with(root)
    activate.assert_not_called()
    assert updates.job(root)['state'] == 'failed'


def test_old_managed_bootstrap_refuses_new_package_action(root, monkeypatch):
    dispatch = Mock()
    monkeypatch.setattr(updates, 'dispatch', dispatch)
    with pytest.raises(RuntimeError, match='compatibility release'):
        updates.queue('packages', root=root)
    dispatch.assert_not_called()
