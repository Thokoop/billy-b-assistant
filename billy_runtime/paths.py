"""Stable writable installation data, separate from selected release code."""

import os
import re
from pathlib import Path


CODE_ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path(os.environ.get('BILLY_INSTALL_ROOT', CODE_ROOT)).resolve()


# Extension devices installed outside the release code: <install root>/private/
# <name>/ (a Python package). Software updates replace the release, never this.
DEVICE_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def devices_root(root=None):
    return Path(root or INSTALL_ROOT) / 'private'


def installed_devices(root=None):
    """Names of the extension devices installed under ``devices_root()``."""
    try:
        entries = list(devices_root(root).iterdir())
    except OSError:
        return []
    return sorted(
        entry.name
        for entry in entries
        if DEVICE_NAME.fullmatch(entry.name) and (entry / '__init__.py').is_file()
    )


def default_device(root=None):
    """The device when BILLY_DEVICE is not set: the only installed one, else the fish."""
    devices = installed_devices(root)
    return devices[0] if len(devices) == 1 else 'fish'
