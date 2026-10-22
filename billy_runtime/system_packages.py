"""Shared OS prerequisite checks and constrained, resumable APT transactions.

No core/Flask imports. The stable updater owns the job lock and executes this
module as the Billy user; only package-manager commands use sudo. Never upgrade
unrelated installed packages, change repositories or downgrade on app rollback.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .storage import atomic_write


GROUPS = ('runtime', 'bluetooth', 'onboarding', 'build')
NAME = re.compile(r'[a-z0-9][a-z0-9+.-]*(?::(?:arm64|all))?\Z')
VERSION = re.compile(r'[0-9][A-Za-z0-9.+:~_-]*\Z')
# These packages underpin the original recovery runtime or its network access.
# Their upgrades require an OS/image migration, not a live application update.
FOUNDATION = (
    'python3',
    'libpython3',
    'libc6',
    'libc-bin',
    'systemd',
    'libsystemd',
    'libssl',
    'openssl',
    'libffi',
    'libgcc',
    'libstdc++',
    'sudo',
    'dpkg',
    'apt',
    'network-manager',
    'wpasupplicant',
    'dhcpcd',
    'avahi',
    'hostapd',
    'dnsmasq',
    'linux-image',
    'raspberrypi-kernel',
    'dbus',
)
ASSETS = ('install-bluetooth.sh', '51-billy-bluetooth.conf', '51-billy-bluetooth.lua')
_cache = {}
_guard = threading.Lock()


def run(args, *, mutate=False, timeout=60):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(('APT', 'DPKG', 'PYTHON', 'BILLY_'))
    }
    env.update(
        LC_ALL='C',
        LANG='C',
        DEBIAN_FRONTEND='noninteractive',
        NEEDRESTART_MODE='l',
        PATH='/usr/sbin:/usr/bin:/sbin:/bin',
    )
    try:
        result = subprocess.run(
            [
                str(v)
                for v in (
                    [
                        'sudo',
                        '-n',
                        'env',
                        'DEBIAN_FRONTEND=noninteractive',
                        'NEEDRESTART_MODE=l',
                        'LC_ALL=C',
                        'LANG=C',
                        'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                        *args,
                    ]
                    if mutate
                    else args
                )
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        print((exc.stdout or '')[-12000:], flush=True)
        raise RuntimeError(
            'System-component command failed; details are in the updater log.'
        ) from exc
    if mutate and result.stdout:
        print(result.stdout[-12000:], flush=True)
    return result.stdout


def load_manifest(source):
    manifest = json.loads(
        (Path(source) / 'dependencies/system-packages.json').read_text()
    )
    if manifest.get('schema') != 1:
        raise ValueError(
            'Unsupported system-component manifest; update the installer first.'
        )
    modes = manifest.get('onboarding_modes', {})
    if not isinstance(modes, dict) or set(modes) - {'legacy', 'unified'}:
        raise ValueError('Invalid Wi-Fi system-component modes')
    for names in [*(manifest.get(group) for group in GROUPS), *modes.values()]:
        if not isinstance(names, list) or any(
            not isinstance(n, str) or not NAME.fullmatch(n) for n in names
        ):
            raise ValueError('Invalid system-component package list')
    if not isinstance(manifest.get('platforms'), list):
        raise ValueError('Missing supported platforms')
    return manifest


def host():
    if platform.system() != 'Linux':
        raise RuntimeError('System components require Raspberry Pi OS.')
    release = platform.freedesktop_os_release()
    arch = run(['dpkg', '--print-architecture']).strip()
    return {
        'distribution': release.get('VERSION_CODENAME'),
        'architecture': arch,
        'python': f'{os.sys.version_info.major}.{os.sys.version_info.minor}',
    }


def inventory():
    raw = run([
        'dpkg-query',
        '-W',
        '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n',
    ])
    result = {}
    for line in raw.splitlines():
        name, version, status = line.split('\t')
        if status not in {'not-installed', 'config-files'}:
            if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
                raise ValueError('Invalid package inventory')
            result[name] = {'version': version, 'status': status}
    return result


def lookup(installed, name):
    return (
        installed.get(name)
        or installed.get(name + ':arm64')
        or installed.get(name + ':all')
    )


def onboarding_mode(root):
    """Match setup/wifi_check.sh using the stable install's config, not a release.

    Keep this independent of dotenv and the application's Python environment so
    prerequisite recovery still works when application dependencies are broken.
    Missing/unknown modes retain the legacy default used by the Wi-Fi watchdog.
    """
    try:
        content = (Path(root) / '.env').read_text(encoding='utf-8')
    except FileNotFoundError:
        return 'legacy'
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            'Could not read the Wi-Fi mode from the configuration.'
        ) from exc
    value = 'legacy'
    for line in content.splitlines():
        if line.startswith('WIFI_ONBOARDING_MODE='):
            value = line.split('=', 1)[1].replace('"', '').replace("'", '').lower()
    return value if value in {'legacy', 'unified'} else 'legacy'


def requirements(manifest, machine, *, mode='legacy'):
    if machine not in manifest['platforms']:
        raise RuntimeError(
            'No tested system-component plan for this OS, architecture and Python version.'
        )
    onboarding = manifest.get('onboarding_modes', {}).get(mode, manifest['onboarding'])
    return sorted({
        name
        for group in GROUPS
        for name in (onboarding if group == 'onboarding' else manifest[group])
    })


def targets(manifest, machine):
    """Only explicit per-OS versions may upgrade existing non-foundation packages."""
    result = {}
    ids = set()
    for migration in manifest.get('migrations', []):
        identity = migration.get('id', '')
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', identity) or identity in ids:
            raise ValueError('Invalid or duplicate system migration')
        ids.add(identity)
        if migration.get('distribution') != machine['distribution']:
            continue
        if migration.get('compatible_with_previous') is not True:
            raise ValueError(
                'System migration must support the previous software and recovery runtime.'
            )
        for name, version in migration.get('packages', {}).items():
            if (
                not NAME.fullmatch(name)
                or not isinstance(version, str)
                or not VERSION.fullmatch(version)
            ):
                raise ValueError('Invalid migration package/version')
            if name in result and result[name] != version:
                raise ValueError('Conflicting system migrations')
            result[name] = version
    return result


def bluetooth_revision(source, root):
    digest = hashlib.sha256(str(Path(root).resolve()).encode())
    digest.update(str(os.getuid()).encode())
    for name in ASSETS:
        digest.update((Path(source) / 'setup/system' / name).read_bytes())
    return digest.hexdigest()


def bluetooth_ready(source, root):
    try:
        marker = json.loads((Path(root) / 'state/bluetooth-setup.json').read_text())
        return (
            marker.get('revision') == bluetooth_revision(source, root)
            and Path('/etc/dbus-1/system.d/billy-bluetooth.conf').exists()
        )
    except (OSError, ValueError):
        return False


def pending_transaction(root):
    try:
        return json.loads(
            (Path(root) / 'state/software/packages/transaction.json').read_text()
        )
    except FileNotFoundError:
        return {}


def interrupted(root):
    receipt = pending_transaction(root)
    if receipt.get('restart_core'):
        run(['systemctl', 'start', '--no-block', 'billy.service'], mutate=True)
    invalidate(root)


def check(source, root):
    manifest = load_manifest(source)
    machine = host()
    names = requirements(manifest, machine, mode=onboarding_mode(root))
    installed = inventory()
    missing = [name for name in names if not lookup(installed, name)]
    damaged = [
        name for name, info in installed.items() if info['status'] != 'installed'
    ]
    requested = targets(manifest, machine)
    outdated = [
        name
        for name, version in requested.items()
        if not lookup(installed, name) or lookup(installed, name)['version'] != version
    ]
    configure = not bluetooth_ready(source, root)
    pending = pending_transaction(root)
    unfinished = pending.get('state') in {'applying', 'interrupted'}
    needs = bool(missing or damaged or outdated or configure or unfinished)
    return {
        'state': 'attention' if needs else 'ready',
        'missing': missing,
        'damaged': damaged,
        'updates': outdated,
        'configuration_needed': configure,
        'message': (
            'System components need attention.'
            if needs
            else 'All system components are installed.'
        ),
        'repairable': needs,
        'platform': machine,
    }


def invalidate(root):
    with _guard:
        _cache.pop(str(Path(root).resolve()), None)


def status(root, source=None):
    """Bounded background probe; no apt or slow dpkg calls on an HTTP worker."""
    root = Path(root).resolve()
    if source is None:
        from .updates import selection

        source = selection(root)[1]
    key = str(root)
    # Updater and web run in different processes. Receipt changes must invalidate
    # a cached warning immediately, including after a successful repair.
    stamps = []
    for path in (
        root / 'state/software/packages/transaction.json',
        root / 'state/bluetooth-setup.json',
        root / '.env',
    ):
        try:
            info = path.stat()
            stamps.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stamps.append(None)
    with _guard:
        entry = _cache.get(key)
        if (
            entry
            and entry['source'] == str(source)
            and entry['stamps'] == stamps
            and (entry['running'] or time.monotonic() - entry['at'] < 30)
        ):
            return dict(entry['value'])
        entry = {
            'source': str(source),
            'stamps': stamps,
            'at': time.monotonic(),
            'running': True,
            'value': {
                'state': 'checking',
                'message': 'Checking system components…',
                'repairable': False,
            },
        }
        _cache[key] = entry

    def probe():
        try:
            value = check(source, root)
        except Exception as exc:
            value = {'state': 'unavailable', 'message': str(exc), 'repairable': False}
        with _guard:
            entry.update(value=value, running=False, at=time.monotonic())

    try:
        threading.Thread(
            target=probe, daemon=True, name='system-components-check'
        ).start()
    except RuntimeError:
        with _guard:
            entry.update(
                running=False,
                value={
                    'state': 'unavailable',
                    'message': 'Could not start component check. Try again shortly.',
                    'repairable': False,
                },
            )
    return dict(entry['value'])


def parse_plan(output):
    result = {}
    for line in output.splitlines():
        if line.startswith('Remv '):
            raise RuntimeError('Package removal is not allowed during a Billy update.')
        if line.startswith('Inst '):
            match = re.match(r'Inst (\S+)(?: \[[^]]+\])? \((\S+)', line)
            if (
                not match
                or not NAME.fullmatch(match[1])
                or not VERSION.fullmatch(match[2])
            ):
                raise RuntimeError(
                    'Unrecognized package-manager plan; nothing installed.'
                )
            result[match[1]] = match[2]
    return result


def validate_plan(plan, installed, approved):
    for name, version in plan.items():
        before = lookup(installed, name)
        if not before and name.split(':')[0].startswith((
            'network-manager',
            'wpasupplicant',
            'dhcpcd',
            'avahi',
            'hostapd',
            'dnsmasq',
        )):
            raise RuntimeError(
                f'Installing {name} needs a tested network migration; current connection preserved.'
            )
        if before and before['version'] != version:
            if name.split(':')[0].startswith(FOUNDATION):
                raise RuntimeError(
                    f'{name} supports recovery or connectivity; an OS migration is required.'
                )
            if approved.get(name) != version:
                raise RuntimeError(
                    f'Unexpected upgrade of {name}; current installation retained.'
                )
            result = subprocess.run(
                ['dpkg', '--compare-versions', version, 'lt', before['version']],
                check=False,
            )
            if result.returncode == 0:
                raise RuntimeError('System-package downgrades are not supported.')
            if result.returncode != 1:
                raise RuntimeError('Could not compare package versions.')


def apt_args(preferences=None):
    args = [
        'apt-get',
        '-o',
        'DPkg::Lock::Timeout=120',
        '-o',
        'Dpkg::Options::=--force-confold',
    ]
    if preferences:
        args += [
            '-o',
            f'Dir::Etc::preferences={preferences}',
            '-o',
            f'Dir::Etc::preferencesparts={Path(preferences).parent / "empty-preferences"}',
        ]
    return args


def pins(installed, plan):
    """Freeze the solver: no unreviewed dependency may appear during apply."""
    allowed = {name: info['version'] for name, info in installed.items()}
    allowed.update(plan)
    # More specific records precede the default deny rule.
    return (
        ''.join(
            f'Package: {name}\nPin: version {version}\nPin-Priority: 1001\n\n'
            for name, version in sorted(allowed.items())
        )
        + 'Package: *\nPin: release *\nPin-Priority: -1\n'
    )


def save_receipt(path, value):
    atomic_write(path, json.dumps(value, indent=2) + '\n')


def configure_bluetooth(source, root):
    if bluetooth_ready(source, root):
        return
    # The existing script owns only Billy-specific integration, never apt.
    run(['bash', str(Path(source) / 'setup/system/install-bluetooth.sh')], timeout=None)
    atomic_write(
        Path(root) / 'state/bluetooth-setup.json',
        json.dumps({
            'revision': bluetooth_revision(source, root),
        }),
    )


def ensure(source, root, progress, *, repair=False):
    """Run only under updates.operation.lock. Existing app selection is untouched."""
    source, root = Path(source).resolve(), Path(root).resolve()
    manifest = load_manifest(source)
    machine = host()
    names = requirements(manifest, machine, mode=onboarding_mode(root))
    approved = targets(manifest, machine)
    folder = root / 'state/software/packages'
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / 'transaction.json'
    previous = json.loads(receipt.read_text()) if receipt.exists() else {}
    installed = inventory()
    damaged = [
        name for name, info in installed.items() if info['status'] != 'installed'
    ]
    if damaged:
        known = previous.get('plan', {})
        if (
            not repair
            or previous.get('state') not in {'applying', 'interrupted'}
            or not known
            or any(known.get(name) != installed[name]['version'] for name in damaged)
        ):
            raise RuntimeError(
                'An interrupted package installation needs repair before updating.'
            )
        # Only our own interrupted packages, never a blanket dpkg --configure -a.
        progress('Repairing an interrupted component installation…')
        was_active = subprocess.run(
            ['systemctl', 'is-active', '--quiet', 'billy.service'], check=False
        ).returncode == 0 or bool(previous.get('restart_core'))
        previous.update(
            state='interrupted',
            restart_core=was_active or previous.get('restart_core', False),
        )
        save_receipt(receipt, previous)
        try:
            if was_active:
                run(['systemctl', 'stop', 'billy.service'], mutate=True)
            run(['dpkg', '--configure', *damaged], mutate=True, timeout=None)
        finally:
            if was_active:
                run(['systemctl', 'start', '--no-block', 'billy.service'], mutate=True)
        installed = inventory()
        if any(info['status'] != 'installed' for info in installed.values()):
            raise RuntimeError(
                'Package repair did not finish; the web interface remains available.'
            )
    requested = [name for name in names if not lookup(installed, name)]
    for name, version in approved.items():
        before = lookup(installed, name)
        if not before or before['version'] != version:
            requested.append(f'{name}={version}')
    needs_config = not bluetooth_ready(source, root)
    if not requested and not needs_config:
        if previous:
            previous['state'] = 'resolved'
            save_receipt(receipt, previous)
        invalidate(root)
        return
    if shutil.disk_usage(root).free < 512 * 1024**2:
        raise RuntimeError(
            'At least 512 MiB free space is required for system components.'
        )
    run(['true'], mutate=True)
    plan = {}
    if requested:
        progress('Checking and downloading required system components…')
        run([*apt_args(), 'update'], mutate=True, timeout=180)
        simulated = run(
            [
                *apt_args(),
                '--simulate',
                '--no-remove',
                '--no-install-recommends',
                'install',
                *requested,
            ],
            mutate=True,
        )
        plan = parse_plan(simulated)
        validate_plan(plan, installed, approved)
        if not plan:
            raise RuntimeError('Required packages could not be resolved.')
    record = {
        'schema': 1,
        'platform': machine,
        'before': installed,
        'plan': plan,
        'state': 'planned',
        'source': str(source),
        'created': time.time(),
    }
    save_receipt(receipt, record)
    (folder / 'empty-preferences').mkdir(exist_ok=True)
    pref = folder / 'preferences'
    atomic_write(pref, pins(installed, plan))
    packages = [f'{name}={version}' for name, version in sorted(plan.items())]
    args = [*apt_args(pref), '--yes', '--no-remove', '--no-install-recommends']
    if packages:
        run([*args, '--download-only', 'install', *packages], mutate=True, timeout=600)
        fresh = inventory()
        if fresh != installed:
            raise RuntimeError(
                'System packages changed during preparation; retry the operation.'
            )
        actual = parse_plan(
            run([*args, '--simulate', 'install', *packages], mutate=True)
        )
        if actual != plan:
            raise RuntimeError(
                'Package plan changed during download; nothing installed.'
            )
    # Pause the core only for application. The web supervisor and recovery keep
    # running. Downloads and plan failures never stop the working core.
    active = subprocess.run(
        ['systemctl', 'is-active', '--quiet', 'billy.service'], check=False
    ).returncode == 0 or (
        previous.get('state') in {'applying', 'interrupted'}
        and bool(previous.get('restart_core'))
    )
    record.update(state='applying', restart_core=active)
    save_receipt(receipt, record)
    try:
        progress(
            'Installing system components; Billy pauses briefly. Keep the power connected…'
        )
        if active:
            run(['systemctl', 'stop', 'billy.service'], mutate=True)
        if packages:
            # No timeout may kill dpkg midway through unpacking/configuration.
            run(
                [*args, '--no-download', 'install', *packages],
                mutate=True,
                timeout=None,
            )
        after = inventory()
        if any(
            not lookup(after, name)
            or lookup(after, name)['status'] != 'installed'
            or lookup(after, name)['version'] != version
            for name, version in plan.items()
        ):
            raise RuntimeError('System-component installation is incomplete.')
        configure_bluetooth(source, root)
        record.update(state='complete', after=after, finished=time.time())
        save_receipt(receipt, record)
    except BaseException:
        record['state'] = 'interrupted'
        save_receipt(receipt, record)
        raise
    finally:
        invalidate(root)
        if active:
            run(['systemctl', 'start', '--no-block', 'billy.service'], mutate=True)
