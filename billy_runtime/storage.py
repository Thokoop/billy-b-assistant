"""Cross-process transactions and durable, private file replacement."""

import configparser
import contextlib
import io
import os
import tempfile
import threading
import time
from pathlib import Path


_locks = {}
_guard = threading.Lock()
_local = threading.local()


@contextlib.contextmanager
def transaction(path):
    import fcntl

    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    if not lock.acquire(timeout=10):
        raise TimeoutError('Configuration writer is busy; retry later')
    try:
        held = getattr(_local, 'held', set())
        if key in held:
            yield
            return
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            'Configuration writer is busy; retry later'
                        ) from None
                    time.sleep(0.02)
            _local.held = held | {key}
            yield
        finally:
            _local.held = held
            os.close(fd)
    finally:
        lock.release()


def atomic_write(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing to replace a symbolic link')
    if isinstance(data, str):
        data = data.encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def write_ini(path, config):
    stream = io.StringIO()
    config.write(stream)
    # Validate before replacing the only live copy.
    check = configparser.ConfigParser(interpolation=None)
    check.read_string(stream.getvalue())
    atomic_write(path, stream.getvalue())


def runtime_directory(root):
    """Put frequent health metadata in RAM on the Pi, not on the SD card."""
    import hashlib

    candidates = [Path('/run/user') / str(os.getuid()), Path('/dev/shm')]
    base = next(
        (p for p in candidates if p.is_dir() and os.access(p, os.W_OK)),
        Path(tempfile.gettempdir()),
    )
    digest = hashlib.sha256(str(Path(root).absolute()).encode()).hexdigest()[:16]
    return base / f'billy-health-{os.getuid()}-{digest}'
