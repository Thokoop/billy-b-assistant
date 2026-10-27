"""Shared Bluetooth telemetry location, independent of the user-session runtime."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from .paths import INSTALL_ROOT


SHARED_MEMORY = Path('/dev/shm')


def status_path(root=INSTALL_ROOT):
    """Choose a stable RAM-backed path even before /run/user/UID exists."""
    base = (
        SHARED_MEMORY
        if SHARED_MEMORY.is_dir() and os.access(SHARED_MEMORY, os.W_OK)
        else Path(tempfile.gettempdir())
    )
    identity = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
    return base / f'billy-bluetooth-{os.getuid()}-{identity}' / 'status.json'


def prepare_directory(directory):
    """Create only Billy's private directory; reject unsafe existing entries."""
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PermissionError('Bluetooth telemetry directory is not private')
