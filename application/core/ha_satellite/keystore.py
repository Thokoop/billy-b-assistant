"""The satellite's ESPHome API encryption key, kept in the installation.

Home Assistant normally provisions the key itself: Billy reports that it
supports encryption and accepts a key, Home Assistant generates one, sends it
over a short-lived Noise session with the well-known all-zero key, and then
reconnects encrypted. From then on Billy refuses unencrypted connections.

The key is stored base64-encoded in ``state/ha-satellite/noise.key`` with
owner-only permissions. It is not a ``.env`` setting on purpose: the settings
page saves every field at once, and a draft opened before Home Assistant set
the key would otherwise erase it. The web UI can show or reset it through
dedicated endpoints.
"""

import contextlib
import os
from pathlib import Path

from .noise import decode_psk, encode_psk


# Stands in for a damaged key file; random per process, so no client has it.
_UNUSABLE_KEY = os.urandom(32)


def default_path() -> Path:
    from billy_runtime.paths import INSTALL_ROOT

    return INSTALL_ROOT / 'state' / 'ha-satellite' / 'noise.key'


class KeyStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()

    def get(self) -> bytes | None:
        """The raw 32-byte key, or None while no key is set.

        A key file that exists but cannot be read or decoded fails closed:
        it counts as a key nobody has, so a damaged file never turns
        encryption off. Resetting the key removes it.
        """
        try:
            text = self.path.read_text(encoding='ascii').strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return _UNUSABLE_KEY
        try:
            return decode_psk(text)
        except ValueError:
            return _UNUSABLE_KEY

    @property
    def damaged(self) -> bool:
        return self.get() is _UNUSABLE_KEY

    def get_text(self) -> str | None:
        key = self.get()
        if key is None or key is _UNUSABLE_KEY:
            return None
        return encode_psk(key)

    def set(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError('key must be 32 bytes')
        from billy_runtime.storage import atomic_write

        atomic_write(self.path, encode_psk(key) + '\n')

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    def version(self) -> tuple[int, int, int] | None:
        """Changes when the key file is replaced or removed."""
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)
