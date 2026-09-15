"""Install OS integration after the existing updater restarts webconfig.

Python dependencies remain exclusively in requirements.txt. This module only
uses the standard library so a failed Bluetooth setup cannot prevent Flask
from starting. No migration hook is required in the previously installed code.
"""

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / 'state'
ASSETS = (
    'install-bluetooth.sh',
    '51-billy-bluetooth.conf',
    '51-billy-bluetooth.lua',
    'billy-bluetooth.service.in',
)
_lock = threading.Lock()
_thread = None
_status = {'state': 'pending', 'message': 'Preparing Bluetooth setup…'}


def setup_status():
    with _lock:
        return dict(_status)


def _set_status(state, message):
    with _lock:
        _status.update(state=state, message=message)


def fingerprint():
    digest = hashlib.sha256(str(ROOT).encode())
    digest.update(str(os.getuid()).encode())
    for name in ASSETS:
        digest.update((ROOT / 'setup/system' / name).read_bytes())
    return digest.hexdigest()


def _provision(force=False):
    try:
        STATE_DIR.mkdir(exist_ok=True)
        # Serialize both separate Flask processes and retries after a restart.
        with (STATE_DIR / 'bluetooth-setup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = STATE_DIR / 'bluetooth-setup.json'
            revision = fingerprint()
            try:
                installed = json.loads(marker.read_text())
            except (OSError, ValueError):
                installed = {}
            if (
                not force
                and installed.get('revision') == revision
                and Path('/etc/systemd/system/billy-bluetooth.service').exists()
            ):
                _set_status('ready', 'Bluetooth is installed.')
                return
            _set_status(
                'installing',
                'Installing Bluetooth support. Billy remains available; this can take a few minutes…',
            )
            # Keep output out of memory, and preserve details for remote diagnosis.
            # No timeout that could interrupt dpkg halfway through installation.
            with (STATE_DIR / 'bluetooth-setup.log').open('w') as log:
                result = subprocess.run(
                    ['bash', str(ROOT / 'setup/system/install-bluetooth.sh')],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=ROOT,
                )
            if result.returncode:
                lines = (
                    (STATE_DIR / 'bluetooth-setup.log')
                    .read_text(errors='replace')
                    .splitlines()
                )
                detail = '\n'.join(lines[-6:])[-1800:]
                raise RuntimeError(detail or 'System installation failed')
            temporary = marker.with_suffix('.tmp')
            temporary.write_text(json.dumps({'revision': revision}))
            temporary.replace(marker)
            _set_status('ready', 'Bluetooth is installed.')
    except Exception as exc:
        logging.getLogger(__name__).exception('Bluetooth setup failed')
        _set_status('error', f'Bluetooth setup failed: {exc}. You can retry here.')


def start_setup(force=False):
    global _thread
    if sys.platform != 'linux' or not Path('/run/systemd/system').exists():
        _set_status(
            'unsupported',
            'Automatic Bluetooth setup requires Raspberry Pi OS with systemd.',
        )
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _status.update(state='installing', message='Preparing Bluetooth setup…')
        _thread = threading.Thread(
            target=_provision, args=(force,), name='bluetooth-setup', daemon=True
        )
        _thread.start()
