"""Preparation cannot mutate the running software, even on partial failure."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from billy_runtime import environments as envs
from billy_runtime.environment_probe import dependency_errors


ROOT = Path(__file__).resolve().parents[2]
HASH = 'a' * 64


@pytest.mark.parametrize('version,name', [((3, 11), 'py311'), ((3, 13), 'py313')])
def test_profile_supports_existing_and_new_images(version, name):
    assert name in envs.lock_profile(system='Linux', machine='aarch64', version=version)


@pytest.mark.parametrize(
    'system,machine,version',
    [
        ('Linux', 'armv7l', (3, 11)),
        ('Darwin', 'arm64', (3, 13)),
        ('Linux', 'aarch64', (3, 12)),
        ('Linux', 'aarch64', (3, 14)),
    ],
)
def test_unknown_platform_fails_before_install(system, machine, version):
    with pytest.raises(RuntimeError):
        envs.lock_profile(system=system, machine=machine, version=version)


@pytest.mark.parametrize(
    'content',
    [
        '',
        'numpy',
        'numpy>=2',
        'numpy==2',
        '-r other.txt',
        '--index-url https://example.com',
        f'numpy==2 --hash=sha256:short',
        f'numpy==2 --hash=sha256:{HASH}\nnumpy==3 --hash=sha256:{HASH}',
    ],
)
def test_lock_rejects_resolution_and_unsafe_directives(content):
    with pytest.raises(ValueError):
        envs.locked_entries(content)


def test_production_locks_cover_legacy_requirements_and_installation_tools():
    import re

    expected = {
        re.sub(r'[-_.]+', '-', re.match(r'[\w.-]+', line)[0]).lower()
        for line in (ROOT / 'requirements.txt').read_text().splitlines()
        if line.strip() and not line.startswith('#')
    }
    for path in (ROOT / 'dependencies').glob('linux-*.lock'):
        names = {name for name, _ in envs.locked_entries(path.read_text())}
        assert expected | envs.BOOTSTRAP_PACKAGES <= names
        assert ('tflite-runtime' in names) == ('py311' in path.name)


def test_only_known_upstream_tflite_metadata_exception_is_allowed():
    message = 'openwakeword 0.6.0 requires tflite-runtime, which is not installed.'
    assert dependency_errors(message, (3, 13)) == []
    assert dependency_errors(message, (3, 11)) == [message]
    unexpected = 'openai 3.16.2 requires httpx, which is not installed.'
    assert dependency_errors(message + '\n' + unexpected, (3, 13)) == [unexpected]
    assert dependency_errors(message.replace('0.6.0', '0.7.0'), (3, 13))


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    (source / 'dependencies').mkdir(parents=True)
    (source / 'venv').mkdir()
    (source / 'venv' / 'keep').write_text('working installation')
    lock = source / 'dependencies' / 'linux-aarch64-py311.lock'
    lock.write_text(
        '\n'.join(
            f'{name}==1 --hash=sha256:{HASH}'
            for name in sorted(envs.BOOTSTRAP_PACKAGES)
        )
    )
    monkeypatch.setattr(envs, 'lock_profile', lambda: lock.name)
    monkeypatch.setattr(
        envs.shutil, 'disk_usage', lambda _: SimpleNamespace(free=10**10)
    )
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        'read_text',
        lambda path, *a, **kw: 'ID=debian\n'
        if str(path) == '/etc/os-release'
        else original_read(path, *a, **kw),
    )
    calls = []

    def run(command, **kwargs):
        calls.append([str(v) for v in command])
        return '[]' if '--format=json' in command else 'pipewire\t1.2.7\tarm64\n'

    monkeypatch.setattr(envs, 'run', run)
    return source, calls


def test_success_publishes_candidate_only_and_records_exact_inventory(candidate):
    source, calls = candidate
    prepared = envs.prepare('v1.2.3', source=source)
    assert prepared.parent == source / 'state' / 'environments'
    assert (prepared / '.complete').is_file()
    manifest = json.loads((prepared / 'manifest.json').read_text())
    assert manifest['release'] == 'v1.2.3'
    assert len(manifest['lock_sha256']) == 64
    assert 'pipewire\t1.2.7' in (prepared / 'system-packages.tsv').read_text()
    assert (source / 'venv' / 'keep').read_text() == 'working installation'
    assert all('systemctl' not in c and 'apt-get' not in c for c in calls)
    installs = [c for c in calls if 'install' in c]
    assert len(installs) == 2
    assert all('--require-hashes' in c and '--no-deps' in c for c in installs)
    assert '--no-build-isolation' in installs[-1]
    # Retrying a reinstall prepares separately; it must not mutate a good env.
    second = envs.prepare('v1.2.3', source=source)
    assert second != prepared and (prepared / '.complete').is_file()


@pytest.mark.parametrize('failure_at', range(1, 7))
def test_partial_failure_removes_only_its_candidate(candidate, monkeypatch, failure_at):
    source, calls = candidate
    original = envs.run

    def fail(command, **kwargs):
        if len(calls) + 1 == failure_at:
            raise RuntimeError('simulated interrupted preparation')
        return original(command, **kwargs)

    monkeypatch.setattr(envs, 'run', fail)
    with pytest.raises(RuntimeError):
        envs.prepare('v2', source=source)
    assert not list((source / 'state' / 'environments').iterdir())
    assert (source / 'venv' / 'keep').read_text() == 'working installation'


def test_disk_full_keeps_current_environment_and_never_runs_pip(candidate, monkeypatch):
    source, calls = candidate
    monkeypatch.setattr(envs.shutil, 'disk_usage', lambda _: SimpleNamespace(free=100))
    with pytest.raises(RuntimeError, match='free space'):
        envs.prepare('v2', source=source)
    assert calls == []
    assert (source / 'venv' / 'keep').is_file()


def test_unsafe_destination_and_release_are_rejected(candidate):
    source, calls = candidate
    link = source / 'link'
    link.symlink_to(source / 'venv', target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        envs.prepare('v2', source=source, directory=link)
    with pytest.raises(ValueError, match='identifier'):
        envs.prepare('../venv', source=source)
    assert calls == []


def test_environment_does_not_inherit_pip_or_python_overrides(monkeypatch):
    monkeypatch.setenv('PIP_INDEX_URL', 'https://example.invalid')
    monkeypatch.setenv('PYTHONPATH', '/unrelated/environment')
    clean = envs.clean_environment()
    assert 'PIP_INDEX_URL' not in clean and 'PYTHONPATH' not in clean
    assert clean['PYTHONNOUSERSITE'] == '1'


@pytest.mark.parametrize(
    'version,returncode,stdout,stderr,accepted',
    [
        ((3, 11), 0, 'No broken requirements found.\n', '', True),
        (
            (3, 13),
            1,
            'openwakeword 0.6.0 requires tflite-runtime, which is not installed.\n',
            '',
            True,
        ),
        (
            (3, 11),
            1,
            'openwakeword 0.6.0 requires tflite-runtime, which is not installed.\n',
            '',
            False,
        ),
        (
            (3, 13),
            1,
            'openai 3.16.2 requires httpx, which is not installed.\n',
            '',
            False,
        ),
        ((3, 11), 2, '', 'pip failed to start', False),
        ((3, 11), 1, '', '', False),
        ((3, 11), 0, 'No broken requirements found.\n', 'Unexpected diagnostic', False),
    ],
)
def test_dependency_probe_disables_cache_without_hiding_errors(
    monkeypatch, version, returncode, stdout, stderr, accepted
):
    from billy_runtime import environment_probe as probe

    calls = []

    def check(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(probe.subprocess, 'run', check)
    monkeypatch.setattr(probe.sys, 'version_info', version)
    if accepted:
        probe.verify_dependencies()
    else:
        with pytest.raises(RuntimeError, match='Dependency validation'):
            probe.verify_dependencies()
    assert calls == [
        [probe.sys.executable, '-m', 'pip', '--isolated', '--no-cache-dir', 'check']
    ]
