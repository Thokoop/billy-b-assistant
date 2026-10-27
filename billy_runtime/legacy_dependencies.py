"""Repair the first upgrade from pre-workaround installers, without core imports."""

import importlib.metadata
import subprocess
import sys
import tempfile
from pathlib import Path

from .environments import clean_environment, lock_profile, locked_entries
from .storage import atomic_write, transaction


class LegacyDependencyError(RuntimeError):
    """The baseline dependency migration did not complete."""


def ensure_openwakeword(root):
    """Only the baseline core supervisor calls this, before launching a worker.

    Old updaters can select 0.4.x on Python 3.13. Do not uninstall first: pip
    downloads and validates the replacement before replacing the installed
    package. Dependencies already come from the legacy requirements install;
    --no-deps avoids upstream's unavailable Python 3.13 TFLite requirement.
    Recovery and the normal web interface remain independent of this repair.
    """
    root = Path(root)
    pending = root / 'state/software/legacy-openwakeword-pending'
    with transaction(root / 'state/software/legacy-dependencies'):
        try:
            installed = importlib.metadata.version('openwakeword')
        except importlib.metadata.PackageNotFoundError:
            # An absent optional backend is not evidence of this old-updater
            # bug; do not install packages in partial/recovery environments.
            if not pending.exists():
                return
            installed = 'incomplete installation'
        if installed == '0.6.0' and not pending.exists():
            return
        try:
            lock = root / 'dependencies' / lock_profile()
            entry = dict(locked_entries(lock.read_text()))['openwakeword']
            if not entry.startswith('openwakeword==0.6.0 '):
                raise ValueError('Unexpected openWakeWord migration target')
            atomic_write(pending, '0.6.0' + chr(10))
            print(f'Migrating legacy openWakeWord {installed} to 0.6.0…', flush=True)
            with tempfile.TemporaryDirectory(prefix='billy-wakeword-') as directory:
                requirements = Path(directory) / 'openwakeword.lock'
                requirements.write_text(entry + '\n')
                subprocess.run(
                    [
                        sys.executable,
                        '-m',
                        'pip',
                        '--isolated',
                        'install',
                        '--no-cache-dir',
                        '--no-deps',
                        '--require-hashes',
                        '--force-reinstall',
                        '--only-binary=:all:',
                        '--index-url',
                        'https://pypi.org/simple',
                        '--timeout',
                        '15',
                        '--retries',
                        '1',
                        '-r',
                        str(requirements),
                    ],
                    check=True,
                    timeout=120,
                    env=clean_environment(),
                    cwd=directory,
                )
                # Run in a fresh interpreter: never reuse an already imported
                # package, and do not open a microphone or instantiate models.
                subprocess.run(
                    [
                        sys.executable,
                        '-c',
                        "from importlib.metadata import version; "
                        "assert version('openwakeword') == '0.6.0'; "
                        "from openwakeword.model import Model",
                    ],
                    check=True,
                    timeout=30,
                    env=clean_environment(),
                    cwd=directory,
                )
            pending.unlink()
        except (
            OSError,
            ValueError,
            KeyError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as exc:
            raise LegacyDependencyError(
                'Could not migrate openWakeWord to 0.6.0. Check the internet '
                'connection and free storage, then restart Billy to retry. '
                'The web interface and software recovery remain available.'
            ) from exc
        print('Legacy openWakeWord migration completed (0.6.0).', flush=True)


def ensure_for_worker(root, role, *, managed):
    """Never mutate a selected release, a web worker or independent recovery."""
    if not managed or role != 'core':
        return
    from . import updates

    record = updates.state(root).get('current') or updates.baseline(root)
    if record.get('id') == 'baseline':
        ensure_openwakeword(root)
