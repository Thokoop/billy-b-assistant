"""First upgrades use the pinned wake-word API without touching managed releases."""

import importlib.metadata
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from billy_runtime import legacy_dependencies as migration
from billy_runtime import updates


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    state = SimpleNamespace(version='0.4.0', calls=[])
    (tmp_path / 'dependencies').mkdir()
    lock = tmp_path / 'dependencies/test.lock'
    state.entry = 'openwakeword==0.6.0 --hash=sha256:' + 'a' * 64
    lock.write_text(state.entry + '\n')
    monkeypatch.setattr(migration, 'lock_profile', lambda: 'test.lock')
    monkeypatch.setattr(
        migration.importlib.metadata, 'version', lambda _: state.version
    )

    def run(command, **kwargs):
        state.calls.append((command, kwargs))
        if 'install' in command:
            state.requirements = Path(command[-1]).read_text()
            state.version = '0.6.0'
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(migration.subprocess, 'run', run)
    state.root = tmp_path
    return state


def test_old_version_installs_hashed_wheel_without_resolving_or_uninstalling_first(
    legacy,
):
    (legacy.root / '.env').write_text('FLASK_PORT=80\n')
    migration.ensure_openwakeword(legacy.root)
    assert legacy.requirements == legacy.entry + '\n'
    assert len(legacy.calls) == 2
    install, options = legacy.calls[0]
    for flag in ['--no-deps', '--require-hashes', '--only-binary=:all:', '--isolated']:
        assert flag in install
    assert 'uninstall' not in install
    assert options['timeout'] == 120
    verify, options = legacy.calls[1]
    assert "version('openwakeword') == '0.6.0'" in verify[-1]
    assert 'from openwakeword.model import Model' in verify[-1]
    assert (legacy.root / '.env').read_text() == 'FLASK_PORT=80\n'
    assert not Path(install[-1]).exists()
    # A subsequent boot is entirely offline and does not run pip again.
    migration.ensure_openwakeword(legacy.root)
    assert len(legacy.calls) == 2


def test_correct_version_and_absent_optional_package_do_not_run_pip(
    legacy, monkeypatch
):
    legacy.version = '0.6.0'
    migration.ensure_openwakeword(legacy.root)

    def absent(_):
        raise importlib.metadata.PackageNotFoundError('openwakeword')

    monkeypatch.setattr(migration.importlib.metadata, 'version', absent)
    migration.ensure_openwakeword(legacy.root)
    assert legacy.calls == []


@pytest.mark.parametrize('failure_at', [1, 2])
def test_failed_install_or_verification_is_actionable_and_can_retry(
    legacy, monkeypatch, failure_at
):
    run = migration.subprocess.run
    count = 0

    def fail(command, **kwargs):
        nonlocal count
        count += 1
        if count == failure_at:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return run(command, **kwargs)

    monkeypatch.setattr(migration.subprocess, 'run', fail)
    with pytest.raises(migration.LegacyDependencyError, match='restart Billy to retry'):
        migration.ensure_openwakeword(legacy.root)
    # No success marker or selected-release change masks a failed repair.
    assert not (legacy.root / 'state/software/selection.json').exists()
    assert (legacy.root / 'state/software/legacy-openwakeword-pending').exists()
    monkeypatch.setattr(migration.subprocess, 'run', run)
    migration.ensure_openwakeword(legacy.root)
    assert legacy.version == '0.6.0'
    assert not (legacy.root / 'state/software/legacy-openwakeword-pending').exists()


@pytest.mark.parametrize(
    'role,managed,record,expected',
    [
        ('core', True, {}, True),
        ('core', True, {'current': {'id': 'baseline'}}, True),
        ('core', True, {'current': {'id': 'managed-release'}}, False),
        ('web', True, {}, False),
        ('recovery', True, {}, False),
        ('core', False, {}, False),
    ],
)
def test_only_original_assistant_environment_is_migrated(
    monkeypatch, tmp_path, role, managed, record, expected
):
    repair = Mock()
    monkeypatch.setattr(migration, 'ensure_openwakeword', repair)
    monkeypatch.setattr(updates, 'state', lambda _: record)
    migration.ensure_for_worker(tmp_path, role, managed=managed)
    assert repair.called is expected
