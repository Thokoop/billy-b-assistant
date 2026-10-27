"""Durable software jobs; the original installation remains the rescue runtime.

No live git checkout, pip upgrade, or service-unit replacement.
OS prerequisites are installed by the constrained system-component runner.
The stable supervisors switch workers while retaining their listening socket.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .paths import INSTALL_ROOT
from .storage import atomic_write, runtime_directory, transaction


BUSY = {
    'queued',
    'preparing',
    'installing_packages',
    'activating',
    'validating',
    'rolling_back',
}
PROTOCOL = 1
HEALTH_WINDOW = 30
VALIDATION_TIMEOUT = 180
SHARED = (
    'profiles',
    'personas',
    'knowledge',
    'custom_songs',
    'models',
    'wakewords',
    'state',
    'sounds/wake-up/custom',
    'sounds/response-history',
)


def directory(root):
    return Path(root) / 'state/software'


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def state(root=INSTALL_ROOT):
    return read_json(directory(root) / 'selection.json')


def baseline(root):
    return {'id': 'baseline', 'version': 'original installation'}


def resolve(record, root):
    root = Path(root).resolve()
    if not isinstance(record, dict):
        raise ValueError('Invalid release record')
    if record.get('id') == 'baseline':
        return root, root / 'venv/bin/python'
    identity = record.get('id', '')
    if not isinstance(identity, str) or not re.fullmatch('[a-f0-9]{32}', identity):
        raise ValueError('Invalid release record')
    folder = directory(root) / 'releases' / identity
    source = folder / 'source'
    if not isinstance(record.get('environment'), str):
        raise ValueError('Invalid environment path')
    if not isinstance(record.get('version'), str):
        raise ValueError('Invalid release version')
    environment = Path(record['environment'])
    if not environment.is_absolute() or not environment.resolve().is_relative_to(
        (folder / 'environments').resolve()
    ):
        raise ValueError('Invalid environment path')
    manifest = read_json(environment / 'manifest.json')
    digest = (environment / '.complete').read_text().strip()
    lock_name = manifest.get('lock', '')
    if (
        not isinstance(lock_name, str)
        or Path(lock_name).name != lock_name
        or not lock_name.endswith('.lock')
    ):
        raise ValueError('Invalid lock path')
    lock = source / 'dependencies' / lock_name
    if (
        not re.fullmatch('[a-f0-9]{64}', digest)
        or digest != manifest.get('lock_sha256')
        or hashlib.sha256(lock.read_bytes()).hexdigest() != digest
    ):
        raise ValueError('Incomplete release environment')
    if not (environment / 'bin/python').is_file():
        raise ValueError('Release interpreter missing')
    return source, environment / 'bin/python'


def selection(root=INSTALL_ROOT):
    current = state(root)
    record = current.get('current') or baseline(root)
    try:
        source, python = resolve(record, root)
    except (OSError, ValueError):
        # A damaged candidate must never remove the independent rescue surface.
        record = baseline(root)
        source, python = resolve(record, root)
    return record, source, python, current.get('generation', 'baseline')


def sync_source_link(root=INSTALL_ROOT):
    """Best-effort navigation link; selection.json remains authoritative.

    Read and publish under the selection lock so concurrent supervisors cannot
    overwrite a newer target. Never replace a user's regular file/directory.
    """
    root = Path(root).resolve()
    link = root / 'current-source'
    temporary = root / f'.current-source-{uuid.uuid4().hex}'
    try:
        with transaction(directory(root) / 'selection.json'):
            source = selection(root)[1]
            target = os.path.relpath(source, root)
            if link.is_symlink():
                if os.readlink(link) == target:
                    return True
            elif link.exists():
                raise FileExistsError('current-source is not a symbolic link')
            try:
                temporary.symlink_to(target, target_is_directory=True)
                os.replace(temporary, link)
            finally:
                temporary.unlink(missing_ok=True)
        return True
    except OSError as exc:
        # Navigation must not prevent startup, activation or recovery.
        print(f'Current-source link unavailable ({type(exc).__name__})', flush=True)
        return False


def generation(root=INSTALL_ROOT):
    selected = state(root)
    record = selected.get('current') or baseline(root)
    identity = record.get('id', 'invalid') if isinstance(record, dict) else 'invalid'
    return f"{selected.get('generation', 'baseline')}:{identity}"


def job(root=INSTALL_ROOT):
    return read_json(directory(root) / 'job.json')


def save_job(root, **changes):
    path = directory(root) / 'job.json'
    with transaction(path):
        value = read_json(path)
        value.update(changes, updated=time.time())
        atomic_write(path, json.dumps(value))
        return value


@lru_cache(maxsize=8)
def original_version(root):
    """The bootstrap checkout is fixed across managed releases."""
    for args in (
        ['describe', '--tags', '--exact-match', 'HEAD'],
        ['rev-parse', '--short', 'HEAD'],
    ):
        try:
            value = subprocess.check_output(
                ['git', *args],
                cwd=root,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
            if value:
                return value
        except (OSError, subprocess.SubprocessError):
            pass
    return 'original installation'


def version_label(record, root):
    if not isinstance(record, dict):
        return None
    if record.get('id') == 'baseline':
        return original_version(str(Path(root).resolve()))
    return record.get('version') or 'Version unknown'


def status(root=INSTALL_ROOT):
    current = state(root)
    work = job(root)
    from . import system_packages

    selected = selection(root)[0]
    return {
        'current': version_label(selected, root),
        'previous': version_label(current.get('previous'), root),
        'rollback_available': bool(current.get('previous')),
        'packages': system_packages.status(root),
        'job': {
            key: work[key]
            for key in ('id', 'action', 'state', 'message')
            if key in work
        },
    }


def unit_name(root):
    suffix = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]
    return f'billy-software-{suffix}'


def dispatch(root, identity):
    """Separate cgroup survives web/core restarts; a reboot is resumed from disk."""
    root = Path(root).resolve()
    user = pwd.getpwuid(os.getuid()).pw_name
    command = [
        'sudo',
        '-n',
        'systemd-run',
        '--quiet',
        '--collect',
        f'--unit={unit_name(root)}',
        '--property=Type=exec',
        '--property=Restart=on-failure',
        '--property=RestartSec=5',
        f'--uid={user}',
        f'--working-directory={root}',
        str(root / 'venv/bin/python'),
        '-m',
        'billy_runtime.updates',
        '--root',
        str(root),
        '--run',
        identity,
    ]
    subprocess.run(command, check=True, capture_output=True, timeout=15)


def queue(action, version=None, root=INSTALL_ROOT):
    root = Path(root).resolve()
    if action not in {'update', 'reinstall', 'rollback', 'packages'}:
        raise ValueError('Unknown software action')
    if action == 'update' and not re.fullmatch(r'v?\d+[A-Za-z0-9._+-]*', version or ''):
        raise ValueError('No valid release selected')
    if action == 'rollback' and not state(root).get('previous'):
        raise ValueError('No previous installation is available')
    if (
        action == 'packages'
        and not (root / 'billy_runtime/system_packages.py').is_file()
    ):
        raise RuntimeError(
            'Install the system-component compatibility release in the original installation first.'
        )
    path = directory(root) / 'job.json'
    with transaction(path):
        if job(root).get('state') in BUSY:
            raise RuntimeError('A software operation is already running')
        identity = uuid.uuid4().hex
        atomic_write(
            path,
            json.dumps({
                'id': identity,
                'action': action,
                'version': version,
                'state': 'queued',
                'message': 'Preparing software operation…',
                'updated': time.time(),
            }),
        )
        # Serialize queuing with dispatch, so another request cannot replace it.
        try:
            dispatch(root, identity)
        except subprocess.TimeoutExpired:
            # systemd may already have accepted the request. Leave it queued;
            # the supervisor resumes it and the operation lock prevents duplicates.
            pass
        except (OSError, subprocess.SubprocessError):
            save_job(
                root,
                state='failed',
                message='Could not start the updater. Current software retained.',
            )
            raise RuntimeError(
                'Could not start the updater; check service permissions'
            ) from None
    return status(root)


def resume(root=INSTALL_ROOT):
    """Called periodically by the stable web supervisor, never by a worker."""
    work = job(root)
    if work.get('state') not in BUSY:
        return
    active = subprocess.run(
        ['systemctl', 'is-active', '--quiet', unit_name(root)],
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if active.returncode != 0:
        dispatch(root, work['id'])


def command(args, root, *, capture=False):
    return subprocess.run(
        [str(value) for value in args],
        cwd=root,
        check=True,
        timeout=1200,
        stdout=subprocess.PIPE if capture else None,
        text=capture,
        env={
            k: v
            for k, v in os.environ.items()
            if not k.startswith(('BILLY_', 'PYTHON'))
        },
    ).stdout


def extract(archive, destination):
    """Git archives contain only code/assets; reject links and traversal."""
    total = 0
    with tarfile.open(archive) as bundle:
        for member in bundle:
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()) or not (
                member.isdir() or member.isfile()
            ):
                raise ValueError('Unsafe software archive')
            total += member.size
            if total > 512 * 1024**2:
                raise ValueError('Software archive is too large')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def share_data(source, root):
    for relative in SHARED:
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        local = source / relative
        if local.exists():
            # Ship missing default models/assets without replacing user data.
            shutil.copytree(
                local,
                target,
                dirs_exist_ok=True,
                copy_function=lambda a, b: (
                    b if Path(b).exists() else shutil.copy2(a, b)
                ),
            )
            shutil.rmtree(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.symlink_to(target, target_is_directory=True)


def install_components(source, root, *, repair=False):
    from .system_packages import ensure

    def progress(message):
        save_job(root, state='installing_packages', message=message)

    ensure(source, root, progress, repair=repair)
    save_job(
        root,
        state='preparing',
        message='System components checked. Preparing software…',
    )


def prepare_candidate(root, work):
    from .environments import MINIMUM_FREE_BYTES, prepare

    # Never snapshot a developer's modified checkout as a customer release.
    command(['git', 'diff', '--quiet', 'HEAD', '--'], root)
    if shutil.disk_usage(root).free < MINIMUM_FREE_BYTES + 512 * 1024**2:
        raise RuntimeError('Insufficient space; current software retained')
    current = state(root).get('current') or baseline(root)
    if work['action'] == 'update':
        command(['git', 'fetch', 'origin', '--tags'], root)
        ref = 'refs/tags/' + work['version']
        commit = command(
            ['git', 'rev-parse', '--verify', ref + '^{commit}'], root, capture=True
        ).strip()
        version = work['version']
    else:
        commit = (
            current.get('commit')
            or command(['git', 'rev-parse', 'HEAD'], root, capture=True).strip()
        )
        if current['id'] == 'baseline':
            try:
                version = command(
                    ['git', 'describe', '--tags', '--exact-match', commit],
                    root,
                    capture=True,
                ).strip()
            except subprocess.CalledProcessError:
                version = commit[:12]
        else:
            version = current['version']
    if not re.fullmatch('[a-f0-9]{40,64}', commit):
        raise ValueError('Invalid release revision')
    folder = directory(root) / 'releases' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    try:
        source = folder / 'source'
        source.mkdir()
        with tempfile.NamedTemporaryFile(dir=folder) as archive:
            command(
                ['git', 'archive', '--format=tar', '-o', archive.name, commit], root
            )
            extract(archive.name, source)
        contract = read_json(source / 'dependencies/deployment.json')
        if contract != {'supervisor_protocol': PROTOCOL, 'shared_data_schema': 1}:
            raise ValueError(
                'Release requires a different migration; current software retained'
            )
        for name in (
            'install-bluetooth.sh',
            '51-billy-bluetooth.conf',
            '51-billy-bluetooth.lua',
        ):
            asset = Path('setup/system') / name
            if (source / asset).read_bytes() != (root / asset).read_bytes():
                raise ValueError(
                    'This release requires a separate Bluetooth system migration'
                )
        install_components(source, root)
        environment = prepare(commit, source=source, directory=folder / 'environments')
        share_data(source, root)
        record = {
            'id': folder.name,
            'version': version,
            'commit': commit,
            'environment': str(environment),
        }
        resolve(record, root)
        atomic_write(folder / 'release.json', json.dumps(record))
        return record
    except BaseException:
        shutil.rmtree(folder)
        raise


def activate(root, record, previous, *, trial):
    resolve(record, root)
    path = directory(root) / 'selection.json'
    with transaction(path):
        atomic_write(
            path,
            json.dumps({
                'current': record,
                'previous': previous,
                'generation': uuid.uuid4().hex,
                'trial': trial,
            }),
        )
    sync_source_link(root)
    return generation(root)


def healthy(root, expected, *, now=None, window=HEALTH_WINDOW):
    now = time.time() if now is None else now
    try:
        reports = [
            read_json(runtime_directory(root) / f'{role}.json')
            for role in ('core', 'web')
        ]
        digest = hashlib.sha256((Path(root) / '.env').read_bytes()).hexdigest()
        return bool(
            all(
                r.get('software') == expected
                and r.get('healthy')
                and r.get('revision') == digest
                and 0 <= now - r['seen'] < 8
                and now - r['since'] >= window
                for r in reports
            )
            and reports[0].get('effective')
            and reports[0].get('effective') == reports[1].get('effective')
        )
    except (OSError, KeyError, TypeError):
        return False


def wait_healthy(root, expected):
    deadline = time.monotonic() + VALIDATION_TIMEOUT
    while time.monotonic() < deadline:
        if healthy(root, expected):
            return True
        time.sleep(1)
    return False


def rollback_job(root, work, message):
    # Persist the intent before touching the pointer. Retrying after power loss
    # repeats the same rollback rather than swapping the versions back again.
    save_job(root, state='rolling_back', message='Restoring the previous installation…')
    before = work.get('before') or baseline(root)
    try:
        resolve(before, root)
    except (OSError, ValueError):
        before = baseline(root)
        message = 'Previous release is damaged. Original installation selected; recovery remains available.'
    activate(root, before, work.get('old_previous'), trial=False)
    save_job(root, state='rolled_back', message=message)


def execute(root, identity):
    root = Path(root).resolve()
    directory(root).mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory(root) / 'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        work = job(root)
        if work.get('id') != identity or work.get('state') not in BUSY:
            return
        if work['state'] == 'installing_packages':
            from .system_packages import interrupted

            interrupted(root)
            save_job(
                root,
                state='failed',
                message='System-component installation was interrupted. Use Repair system components to continue; current software retained.',
            )
            return
        # A reboot/crash during activation invalidates probation. Boot the
        # previous release; never silently bless an interrupted update.
        if work['state'] in {'activating', 'validating', 'rolling_back'}:
            rollback_job(
                root,
                work,
                'Interrupted update selected the previous installation for restart.',
            )
            return
        try:
            selected = state(root)
            keep = {
                (selected.get(key) or {}).get('id') for key in ('current', 'previous')
            }
            for orphan in (directory(root) / 'releases').glob('*'):
                if (
                    re.fullmatch('[a-f0-9]{32}', orphan.name)
                    and orphan.name not in keep
                    and not orphan.is_symlink()
                    and not (orphan / 'release.json').exists()
                ):
                    shutil.rmtree(orphan)
            before = selected.get('current') or baseline(root)
            old_previous = state(root).get('previous')
            save_job(
                root,
                state='preparing',
                before=before,
                old_previous=old_previous,
                message='Preparing and checking software; Billy remains available…',
            )
            work = job(root)
            if work['action'] == 'packages':
                install_components(selection(root)[1], root, repair=True)
                save_job(
                    root,
                    state='complete',
                    message='System components are ready. Settings and software version preserved.',
                )
                return
            candidate = (
                old_previous
                if work['action'] == 'rollback'
                else prepare_candidate(root, work)
            )
            resolve(candidate, root)
            save_job(
                root,
                state='activating',
                candidate=candidate,
                message='Starting the selected software…',
            )
            expected = activate(root, candidate, before, trial=True)
            command(
                ['sudo', '-n', 'systemctl', 'start', '--no-block', 'billy.service'],
                root,
            )
            save_job(
                root,
                state='validating',
                message='Checking Billy and the web interface…',
            )
            if not wait_healthy(root, expected):
                rollback_job(
                    root,
                    job(root),
                    'Startup checks failed. Previous installation selected for restart; configuration preserved.',
                )
                return
            path = directory(root) / 'selection.json'
            with transaction(path):
                value = state(root)
                if generation(root) != expected:
                    raise RuntimeError('Software selection changed during validation')
                value['trial'] = False
                atomic_write(path, json.dumps(value))
            save_job(
                root,
                state='complete',
                message='Software is ready. Both services passed startup checks.',
            )
            try:
                prune(root)
            except OSError as exc:
                print(f'Old release cleanup deferred: {type(exc).__name__}', flush=True)
        except Exception as exc:
            print(f'Software operation failed: {type(exc).__name__}: {exc}', flush=True)
            if job(root).get('state') in {'activating', 'validating', 'rolling_back'}:
                rollback_job(
                    root,
                    job(root),
                    'Update failed. Previous installation selected for restart; configuration preserved.',
                )
            else:
                save_job(
                    root,
                    state='failed',
                    message=f'Preparation failed: {str(exc)[:350]}. Current software retained.',
                )


def prune(root):
    """Retain active+previous only, and only after a successful health window."""
    value = state(root)
    keep = {(value.get(key) or {}).get('id') for key in ('current', 'previous')}
    for folder in (directory(root) / 'releases').glob('*'):
        if (
            re.fullmatch('[a-f0-9]{32}', folder.name)
            and folder.name not in keep
            and folder.is_dir()
            and not folder.is_symlink()
        ):
            shutil.rmtree(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    execute(args.root, args.run)


if __name__ == '__main__':
    main()
