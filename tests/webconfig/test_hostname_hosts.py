"""Hostname changes keep /etc/hosts in step, so sudo stops warning and waiting."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions


SYSTEM = 'application/webconfig/app/routes/system.py'


@pytest.fixture
def hosts(tmp_path):
    file = tmp_path / 'hosts'
    writes = []

    def run(args, input=None, **_):
        assert args == ['sudo', '-n', 'tee', '/etc/hosts']
        writes.append(input)
        file.write_text(input)
        return subprocess.CompletedProcess(args, 0, input, '')

    routes = definitions(
        SYSTEM,
        ['_update_hosts_file', 'repair_hosts_file'],
        {
            'Path': lambda name: file if name == '/etc/hosts' else Path(name),
            'subprocess': SimpleNamespace(run=run),
            'logger': Mock(),
            'os': SimpleNamespace(uname=lambda: SimpleNamespace(nodename='billydev')),
        },
    )
    return SimpleNamespace(file=file, writes=writes, routes=routes)


def test_renaming_points_127_0_1_1_at_the_new_name(hosts):
    hosts.file.write_text('127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\tbilly\n')
    hosts.routes._update_hosts_file('billydev')
    assert hosts.file.read_text() == (
        '127.0.0.1\tlocalhost\n::1\tlocalhost\n127.0.1.1\tbillydev\n'
    )


def test_startup_repairs_an_earlier_rename_and_otherwise_writes_nothing(hosts):
    hosts.file.write_text('127.0.0.1\tlocalhost\n127.0.1.1\tbilly\n')
    hosts.routes.repair_hosts_file()
    assert '127.0.1.1\tbillydev' in hosts.file.read_text()
    hosts.routes.repair_hosts_file()
    assert len(hosts.writes) == 1
    # Not a Raspberry Pi OS hosts file: leave it alone.
    hosts.file.write_text('127.0.0.1\tlocalhost\n')
    hosts.routes.repair_hosts_file()
    assert len(hosts.writes) == 1


def test_sudo_hostname_warning_does_not_replace_the_real_error(monkeypatch):
    def run(args, **_):
        return subprocess.CompletedProcess(
            args,
            4,
            '',
            'sudo: unable to resolve host billydev: Temporary failure in name '
            'resolution\nError: Connection activation failed: Secrets were required',
        )

    routes = definitions(
        SYSTEM,
        ['_run_wifi_command'],
        {'subprocess': SimpleNamespace(run=run)},
    )
    result = routes._run_wifi_command(['sudo', 'nmcli', 'connection', 'up', 'x'])
    assert result.stderr == 'Error: Connection activation failed: Secrets were required'


def test_restoring_setup_leaves_a_root_owned_flag_alone():
    class RootOwnedFlag:
        """wifi_check.sh creates the flag as root: the web user cannot touch it."""

        def exists(self):
            return True

        def touch(self, **_):
            raise PermissionError(13, 'Permission denied')

    logger = Mock()
    routes = definitions(
        SYSTEM,
        ['_set_wifi_onboarding_active'],
        {'WIFI_ONBOARDING_FLAG': RootOwnedFlag(), 'logger': logger},
    )
    routes._set_wifi_onboarding_active(True)
    logger.warning.assert_not_called()
