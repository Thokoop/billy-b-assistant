"""Regression coverage for unattended upgrades from the old updater."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'provisioning_test', ROOT / 'core/bluetooth/provisioning.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'STATE_DIR', tmp_path)
    return module


def test_missing_integration_routes_to_central_repair(setup, monkeypatch):
    from billy_runtime import system_packages

    monkeypatch.setattr(system_packages, 'bluetooth_ready', lambda *_: False)
    setup._provision()
    assert setup.setup_status()['state'] == 'error'
    assert 'System components' in setup.setup_status()['message']
    assert not (setup.STATE_DIR / 'bluetooth-setup.json').exists()


def test_successful_central_repair_is_seen_without_web_restart(setup, monkeypatch):
    from billy_runtime import system_packages

    ready = [False]
    monkeypatch.setattr(system_packages, 'bluetooth_ready', lambda *_: ready[0])
    assert setup.setup_status()['state'] == 'error'
    ready[0] = True
    assert setup.setup_status()['state'] == 'ready'


def test_setup_runs_in_background_on_existing_service_start(setup, monkeypatch):
    monkeypatch.setattr(setup.sys, 'platform', 'linux')
    monkeypatch.setattr(Path, 'exists', lambda _: True)
    thread = Mock()
    monkeypatch.setattr(setup.threading, 'Thread', Mock(return_value=thread))
    setup.start_setup()
    thread.start.assert_called_once()
    assert setup.threading.Thread.call_args.kwargs['target'] == setup._provision
    assert setup.threading.Thread.call_args.kwargs['daemon'] is True
    # Repeated requests must not start another package manager.
    thread.is_alive.return_value = True
    setup.start_setup(force=True)
    setup.threading.Thread.assert_called_once()


@pytest.mark.parametrize('version', ['0.4.17', '0.5.8'])
def test_installer_supports_both_configs_without_restarting_billy(tmp_path, version):
    import os
    import pwd
    import subprocess

    commands = tmp_path / 'bin'
    commands.mkdir()
    log = tmp_path / 'commands.log'
    user = pwd.getpwuid(os.getuid()).pw_name
    scripts = {
        'sudo': 'printf "%s\\n" "$*" >> "$COMMAND_LOG"\nexit 0\n',
        'systemctl': 'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [ "$1" = show ]; then printf "%s\\n" "$TEST_USER"; fi\n',
        'wireplumber': 'printf "wireplumber %s\\n" "$TEST_WP_VERSION"\n',
        'dpkg': 'exit 0\n',
    }
    for name, content in scripts.items():
        path = commands / name
        path.write_text('#!/bin/sh\n' + content)
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f'{commands}:{os.environ["PATH"]}',
        COMMAND_LOG=str(log),
        TEST_USER=user,
        TEST_WP_VERSION=version,
        XDG_CONFIG_HOME=str(tmp_path / 'config'),
    )
    for _ in range(2):
        subprocess.run(
            ['bash', str(ROOT / 'setup/system/install-bluetooth.sh')],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    recorded = log.read_text()
    assert 'restart billy-webconfig.service' not in recorded
    assert 'restart billy.service' not in recorded
    assert 'start billy-bluetooth.service' not in recorded
    # The prototype's unit and its sudoers grant are removed, not installed:
    # without Conflicts= it would no longer stop billy.service, so starting it
    # would put two processes on the same microphone and motor pins.
    assert 'rm -f /etc/systemd/system/billy-bluetooth.service' in recorded
    # Nothing grants sudo for that unit any more; 440 was the sudoers file.
    assert 'install -m 440' not in recorded
    assert 'enable bluetooth.service' in recorded
    assert 'restart bluetooth.service' in recorded
    assert 'apt-get' not in recorded  # Package mutations belong to the central updater.
    config = tmp_path / 'config/wireplumber'
    assert (
        'monitor.alsa = disabled'
        in (config / 'wireplumber.conf.d/51-billy-bluetooth.conf').read_text()
    )
    assert (
        config / 'main.lua.d/51-billy-alsa.lua'
    ).read_text() == 'alsa_monitor.enabled = false\n'
    assert (config / 'bluetooth.lua.d/51-billy-bluetooth.lua').read_text().count(
        'table.insert'
    ) == 1


def test_managed_release_repair_never_launches_a_second_installer(setup, monkeypatch):
    from billy_runtime import system_packages

    monkeypatch.setenv('BILLY_RELEASE_ID', 'a' * 32)
    monkeypatch.setattr(system_packages, 'bluetooth_ready', lambda *_: False)
    setup._provision(force=True)
    assert setup.setup_status()['state'] == 'error'
    assert 'System components' in setup.setup_status()['message']


def test_managed_release_reuses_original_bluetooth_setup_fingerprint(
    setup, tmp_path, monkeypatch
):
    import shutil

    original = setup.fingerprint()
    release = tmp_path / 'release'
    shutil.copytree(ROOT / 'setup/system', release / 'setup/system')
    monkeypatch.setattr(setup, 'ROOT', release)
    assert setup.fingerprint() == original
