"""No hardware or apt mutations: exercise plans, interruption and recovery policy."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from billy_runtime import system_packages as packages


MACHINE = {'distribution': 'bookworm', 'architecture': 'arm64', 'python': '3.11'}


@pytest.fixture
def env(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    root = tmp_path / 'installed'
    (source / 'dependencies').mkdir(parents=True)
    root.mkdir()
    manifest = {
        'schema': 1,
        'platforms': [MACHINE],
        'runtime': ['hello'],
        'bluetooth': [],
        'onboarding': [],
        'build': [],
    }
    (source / 'dependencies/system-packages.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(packages, 'host', lambda: MACHINE)
    monkeypatch.setattr(packages, 'bluetooth_ready', lambda *_: True)
    monkeypatch.setattr(packages, 'configure_bluetooth', Mock())
    monkeypatch.setattr(
        packages.shutil, 'disk_usage', lambda _: SimpleNamespace(free=4 * 1024**3)
    )
    monkeypatch.setattr(
        packages.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=0))
    )
    installed = {'base': {'version': '1', 'status': 'installed'}}
    monkeypatch.setattr(packages, 'inventory', lambda: dict(installed))
    calls = []

    def run(args, **kw):
        calls.append((args, kw))
        if '--simulate' in args:
            return 'Inst hello (2 Debian:stable [arm64])\nConf hello (2 Debian:stable [arm64])\n'
        if '--no-download' in args:
            installed['hello'] = {'version': '2', 'status': 'installed'}
        return ''

    monkeypatch.setattr(packages, 'run', run)
    return SimpleNamespace(
        root=root, source=source, manifest=manifest, installed=installed, calls=calls
    )


def test_check_and_noop_never_start_apt(env):
    env.installed['hello'] = {'version': '2', 'status': 'installed'}
    assert packages.check(env.source, env.root)['state'] == 'ready'
    packages.ensure(env.source, env.root, Mock())
    assert env.calls == []


def test_missing_packages_and_bluetooth_config_are_reported(env, monkeypatch):
    monkeypatch.setattr(packages, 'bluetooth_ready', lambda *_: False)
    state = packages.check(env.source, env.root)
    assert state['missing'] == ['hello'] and state['configuration_needed']
    assert state['repairable'] and state['state'] == 'attention'


def test_full_plan_download_precedes_core_pause_and_keeps_receipt(env):
    packages.ensure(env.source, env.root, Mock())
    commands = [args for args, _ in env.calls]
    download = next(i for i, args in enumerate(commands) if '--download-only' in args)
    stop = commands.index(['systemctl', 'stop', 'billy.service'])
    apply = next(i for i, args in enumerate(commands) if '--no-download' in args)
    assert download < stop < apply
    assert commands[-1] == ['systemctl', 'start', '--no-block', 'billy.service']
    assert env.calls[apply][1]['timeout'] is None
    receipt = packages.pending_transaction(env.root)
    assert receipt['state'] == 'complete' and receipt['plan'] == {'hello': '2'}
    assert (
        'Pin-Priority: -1'
        in (env.root / 'state/software/packages/preferences').read_text()
    )


@pytest.mark.parametrize('output', ['Remv network-manager [1]', 'Inst hello invalid'])
def test_unsafe_or_unreadable_plan_rejected(output):
    with pytest.raises(RuntimeError):
        packages.parse_plan(output)


@pytest.mark.parametrize(
    'name',
    ['libc6', 'python3.11', 'libssl3', 'network-manager', 'systemd', 'libpython3.11'],
)
def test_foundation_upgrades_are_blocked_even_when_manifest_requests_them(name):
    with pytest.raises(RuntimeError, match='recovery or connectivity'):
        packages.validate_plan(
            {name: '2'}, {name: {'version': '1', 'status': 'installed'}}, {name: '2'}
        )


def test_unapproved_transitive_upgrade_is_blocked():
    with pytest.raises(RuntimeError, match='Unexpected upgrade'):
        packages.validate_plan(
            {'libcodec': '2'}, {'libcodec': {'version': '1', 'status': 'installed'}}, {}
        )


def test_approved_upgrade_still_rejects_downgrade(monkeypatch):
    monkeypatch.setattr(
        packages.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=0))
    )
    with pytest.raises(RuntimeError, match='downgrades'):
        packages.validate_plan(
            {'ffmpeg': '1'},
            {'ffmpeg': {'version': '2', 'status': 'installed'}},
            {'ffmpeg': '1'},
        )


def test_approved_non_foundation_upgrade_is_allowed(monkeypatch):
    monkeypatch.setattr(
        packages.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=1))
    )
    packages.validate_plan(
        {'ffmpeg': '2'},
        {'ffmpeg': {'version': '1', 'status': 'installed'}},
        {'ffmpeg': '2'},
    )


def test_new_network_service_not_installed_behind_existing_connection():
    with pytest.raises(RuntimeError, match='network migration'):
        packages.validate_plan({'network-manager': '1'}, {}, {})


def test_failed_download_does_not_stop_core(env, monkeypatch):
    original = packages.run

    def fail(args, **kwargs):
        if '--download-only' in args:
            raise RuntimeError('offline')
        return original(args, **kwargs)

    monkeypatch.setattr(packages, 'run', fail)
    with pytest.raises(RuntimeError, match='offline'):
        packages.ensure(env.source, env.root, Mock())
    assert not any(args[:2] == ['systemctl', 'stop'] for args, _ in env.calls)


def test_failed_apply_restarts_core_and_exposes_repair(env, monkeypatch):
    original = packages.run

    def fail(args, **kwargs):
        if '--no-download' in args:
            raise RuntimeError('dpkg failed')
        return original(args, **kwargs)

    monkeypatch.setattr(packages, 'run', fail)
    with pytest.raises(RuntimeError, match='dpkg failed'):
        packages.ensure(env.source, env.root, Mock())
    assert env.calls[-1][0] == ['systemctl', 'start', '--no-block', 'billy.service']
    assert packages.pending_transaction(env.root)['state'] == 'interrupted'
    assert packages.check(env.source, env.root)['repairable']


def test_interrupted_job_does_not_blindly_resume_apt(env):
    folder = env.root / 'state/software/packages'
    folder.mkdir(parents=True)
    (folder / 'transaction.json').write_text(
        json.dumps({'restart_core': True, 'state': 'applying'})
    )
    packages.interrupted(env.root)
    assert [args for args, _ in env.calls] == [
        ['systemctl', 'start', '--no-block', 'billy.service']
    ]


def test_unknown_half_configured_packages_block_automatic_repair(env):
    env.installed['unrelated'] = {'version': '1', 'status': 'half-configured'}
    with pytest.raises(RuntimeError, match='interrupted'):
        packages.ensure(env.source, env.root, Mock(), repair=True)
    assert not env.calls


def test_known_interrupted_packages_configured_explicitly(env, monkeypatch):
    env.installed['hello'] = {'version': '2', 'status': 'half-configured'}
    folder = env.root / 'state/software/packages'
    folder.mkdir(parents=True)
    (folder / 'transaction.json').write_text(
        json.dumps({'plan': {'hello': '2'}, 'state': 'interrupted'})
    )
    original = packages.run

    def repair(args, **kw):
        if args[:2] == ['dpkg', '--configure']:
            assert args == ['dpkg', '--configure', 'hello']
            env.installed['hello']['status'] = 'installed'
        return original(args, **kw)

    monkeypatch.setattr(packages, 'run', repair)
    packages.ensure(env.source, env.root, Mock(), repair=True)
    assert packages.pending_transaction(env.root)['state'] == 'resolved'


def test_changed_inventory_aborts_before_install(env, monkeypatch):
    initial = dict(env.installed)
    monkeypatch.setattr(packages, 'inventory', Mock(side_effect=[initial, {}]))
    with pytest.raises(RuntimeError, match='changed during preparation'):
        packages.ensure(env.source, env.root, Mock())
    assert not any('--no-download' in args for args, _ in env.calls)


def test_migrations_are_os_specific_and_require_rollback_compatibility(env):
    m = env.manifest
    m['migrations'] = [
        {
            'id': 'codec-v2',
            'distribution': 'bookworm',
            'packages': {'ffmpeg': '2'},
            'compatible_with_previous': True,
        }
    ]
    assert packages.targets(m, MACHINE) == {'ffmpeg': '2'}
    assert packages.targets(m, {**MACHINE, 'distribution': 'trixie'}) == {}
    m['migrations'][0]['compatible_with_previous'] = False
    with pytest.raises(ValueError, match='previous software'):
        packages.targets(m, MACHINE)


@pytest.mark.parametrize(
    'name', ['--allow-unauthenticated', 'foo;reboot', '../x', 'foo=1']
)
def test_manifest_rejects_command_injection(env, name):
    env.manifest['runtime'] = [name]
    (env.source / 'dependencies/system-packages.json').write_text(
        json.dumps(env.manifest)
    )
    with pytest.raises(ValueError):
        packages.ensure(env.source, env.root, Mock())
    assert not env.calls


def test_no_plan_for_unknown_platform(env):
    with pytest.raises(RuntimeError, match='No tested'):
        packages.requirements(env.manifest, {**MACHINE, 'python': '3.12'})


def test_mutation_commands_never_use_shell_or_timeout_dpkg(monkeypatch):
    completed = SimpleNamespace(stdout='done')
    mock = Mock(return_value=completed)
    monkeypatch.setattr(packages.subprocess, 'run', mock)
    packages.run(['apt-get', 'install', 'hello=2'], mutate=True, timeout=None)
    args, kw = mock.call_args
    assert args[0][:3] == ['sudo', '-n', 'env']
    assert 'DEBIAN_FRONTEND=noninteractive' in args[0]
    assert args[0][-3:] == ['apt-get', 'install', 'hello=2']
    assert kw['timeout'] is None and not kw.get('shell')
    assert kw['env']['DEBIAN_FRONTEND'] == 'noninteractive'


@pytest.mark.parametrize('state,version', [('complete', '2'), ('interrupted', '3')])
def test_repair_requires_own_interrupted_version(env, state, version):
    env.installed['hello'] = {'version': version, 'status': 'half-configured'}
    folder = env.root / 'state/software/packages'
    folder.mkdir(parents=True)
    (folder / 'transaction.json').write_text(
        json.dumps({'plan': {'hello': '2'}, 'state': state})
    )
    with pytest.raises(RuntimeError, match='interrupted'):
        packages.ensure(env.source, env.root, Mock(), repair=True)
    assert not env.calls


def test_check_thread_failure_does_not_break_http(env, monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError('cannot start thread')
    monkeypatch.setattr(packages.threading, 'Thread', Mock(return_value=thread))
    assert packages.status(env.root, env.source)['state'] == 'unavailable'


def test_external_repair_receipt_invalidates_web_cache(env, monkeypatch):
    class ImmediateThread:
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(packages.threading, 'Thread', ImmediateThread)
    assert packages.status(env.root, env.source)['state'] == 'attention'
    env.installed['hello'] = {'version': '2', 'status': 'installed'}
    packages.save_receipt(
        env.root / 'state/software/packages/transaction.json', {'state': 'complete'}
    )
    assert packages.status(env.root, env.source)['state'] == 'ready'
