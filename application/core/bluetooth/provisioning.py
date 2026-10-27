"""Observe Bluetooth setup without running a second package installer.

The central updater installs prerequisites and writes the integration marker.
Only standard-library checks run here, so failed setup cannot prevent Flask.
"""

import hashlib
import os
import sys
import threading
from pathlib import Path

from billy_runtime.paths import CODE_ROOT, INSTALL_ROOT


ROOT = CODE_ROOT
STATE_DIR = INSTALL_ROOT / 'state'
ASSETS = (
    'install-bluetooth.sh',
    '51-billy-bluetooth.conf',
    '51-billy-bluetooth.lua',
)
_lock = threading.Lock()
_thread = None
_status = {'state': 'pending', 'message': 'Preparing Bluetooth setup…'}


def setup_status():
    # A repair completes in the updater process; observe its marker without
    # restarting webconfig or taking over its package transaction.
    if _status['state'] != 'unsupported':
        _provision()
    with _lock:
        return dict(_status)


def _set_status(state, message):
    with _lock:
        _status.update(state=state, message=message)


def fingerprint():
    digest = hashlib.sha256(str(INSTALL_ROOT).encode())
    digest.update(str(os.getuid()).encode())
    for name in ASSETS:
        digest.update((ROOT / 'setup/system' / name).read_bytes())
    return digest.hexdigest()


def _provision(force=False):
    from billy_runtime import system_packages

    if system_packages.bluetooth_ready(ROOT, INSTALL_ROOT):
        _set_status('ready', 'Bluetooth is installed.')
    else:
        _set_status(
            'error',
            'Bluetooth is not ready yet. Finish setup in System components under Advanced Settings.',
        )


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
