"""Prepare an isolated, verified Python environment without switching services.

This is deliberately independent of core, dotenv and the web application's
imports. The caller must activate code and environment together, only after
prepare() succeeds. A failed preparation never changes the legacy venv.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
MINIMUM_FREE_BYTES = 3 * 1024**3
BOOTSTRAP_PACKAGES = {'pip', 'setuptools', 'wheel', 'packaging'}


def lock_profile(*, system=None, machine=None, version=None):
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    version = sys.version_info[:2] if version is None else version
    if system != 'Linux' or machine not in {'aarch64', 'arm64'}:
        raise RuntimeError('Managed environments require 64-bit Raspberry Pi OS')
    if tuple(version) not in {(3, 11), (3, 13)}:
        raise RuntimeError('No tested dependency lock for this Python version')
    return f'linux-aarch64-py{version[0]}{version[1]}.lock'


def locked_entries(text):
    """Reject loose requirements, includes and indexes before running pip."""
    entries = []
    names = set()
    for entry in text.replace('\\\n', ' ').splitlines():
        entry = entry.strip()
        if not entry or entry.startswith('#'):
            continue
        requirement, *hashes = entry.split(' --hash=sha256:')
        match = re.fullmatch(
            r'([a-zA-Z0-9_.-]+)(?:==[^\s;]+| @ https://[^\s;]+)',
            requirement.strip(),
        )
        if (
            not match
            or not hashes
            or any(
                not re.fullmatch(r'[a-f0-9]{64}', digest.strip()) for digest in hashes
            )
        ):
            raise ValueError('Lock contains an unpinned or unhashed requirement')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in names:
            raise ValueError('Lock contains a duplicate distribution')
        names.add(name)
        entries.append((name, entry))
    if not entries:
        raise ValueError('Dependency lock is empty')
    return entries


def clean_environment():
    # A user's pip config or PYTHONPATH must not contaminate a new release.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(('PIP_', 'PYTHON'))
    } | {'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': os.devnull}


def run(command, *, cwd=None, capture=False):
    return subprocess.run(
        [str(value) for value in command],
        check=True,
        cwd=cwd,
        env=clean_environment(),
        timeout=1200,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def prepare(release, *, source=SOURCE, directory=None):
    """Create at its final path: relocating a venv breaks console-script paths."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', release):
        raise ValueError('Invalid release identifier')
    source = Path(source).resolve()
    profile = lock_profile()
    lock = source / 'dependencies' / profile
    content = lock.read_text()
    entries = locked_entries(content)
    if BOOTSTRAP_PACKAGES - {name for name, _ in entries}:
        raise ValueError('Lock is missing pinned installation tools')
    digest = hashlib.sha256(content.encode()).hexdigest()
    directory = Path(directory or source / 'state' / 'environments').absolute()
    # Do not follow an operator-provided final symlink into an active venv.
    if directory.is_symlink():
        raise ValueError('Environment directory must not be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(directory).free < MINIMUM_FREE_BYTES:
        raise RuntimeError(
            'At least 3 GiB free space is required; current software retained'
        )
    candidate = (
        directory / f'{release}-{profile[:-5]}-{digest[:12]}-{uuid.uuid4().hex[:8]}'
    )
    candidate.mkdir(mode=0o700)
    try:
        run([sys.executable, '-m', 'venv', candidate])
        python = candidate / 'bin' / 'python'
        bootstrap = candidate / 'bootstrap.lock'
        bootstrap.write_text(
            '\n'.join(line for name, line in entries if name in BOOTSTRAP_PACKAGES)
            + '\n'
        )
        # Build backends are pinned as well. No isolated build environment may
        # silently resolve newer build dependencies while installing an sdist.
        base = [
            python,
            '-m',
            'pip',
            '--isolated',
            'install',
            '--no-cache-dir',
            '--require-hashes',
            '--no-deps',
            '--index-url',
            'https://pypi.org/simple',
        ]
        run([*base, '--only-binary=:all:', '-r', bootstrap])
        run([*base, '--no-build-isolation', '-r', lock])
        # lgpio creates a notification FIFO on import. Keep that disposable
        # probe state away from the running app's working directory.
        with tempfile.TemporaryDirectory(prefix='probe-', dir=candidate) as probe:
            run([python, source / 'billy_runtime' / 'environment_probe.py'], cwd=probe)
        inventory = json.loads(
            run(
                [python, '-m', 'pip', '--isolated', 'list', '--format=json'],
                capture=True,
            )
        )
        packages = run(
            ['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Architecture}\n'],
            capture=True,
        )
        manifest = {
            'schema': 1,
            'release': release,
            'lock': profile,
            'lock_sha256': digest,
            'python': sys.version,
            'machine': platform.machine(),
            'os_release': Path('/etc/os-release').read_text(),
            'packages': inventory,
        }
        (candidate / 'system-packages.tsv').write_text(packages)
        (candidate / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        # Only this marker makes a directory eligible for a later activation.
        # No shared pointer, existing environment or service is changed here.
        with (candidate / '.complete').open('x') as stream:
            stream.write(digest + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return candidate
    except BaseException:
        shutil.rmtree(candidate)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', required=True)
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--source', type=Path, default=SOURCE)
    args = parser.parse_args()
    prepared = prepare(args.release, source=args.source, directory=args.directory)
    print(f'Prepared (not activated): {prepared}')


if __name__ == '__main__':
    main()
