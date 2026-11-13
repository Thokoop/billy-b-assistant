"""Names and images of this Billy: the fish by default, a device can override.

An extension device may ship ``theme/theme.json`` (and images) in its folder,
``<install root>/private/<name>/theme/``. Keys it leaves out keep the fish
defaults below. Plain JSON on purpose: the setup hotspot and the recovery page
read it too, and they must work when the device's Python code does not.

    {"product_name": "…", "name": "…", "hotspot_ssid": "…", "character": "…",
     "persona_icon": "<Material icon name>"}

Persona presets: ``theme/personas/<id>/persona.ini`` replace the fish's
``persona_presets/`` (persona_presets()).

Images: ``theme/images/<file>`` replaces ``webconfig/static/images/<file>``
(and ``theme/favicon.ico`` the favicon) wherever the web interface asks for
it through ``asset()`` / the /theme/ route.

Command line (shell scripts): ``python3 -m billy_runtime.theme hotspot_ssid``.
"""

import json
import re
import sys
from pathlib import Path

from billy_runtime.paths import CODE_ROOT, DEVICE_NAME, INSTALL_ROOT, devices_root


DEFAULTS = {
    # The product: page titles, recovery page, Home Assistant model.
    "product_name": "Billy Bassistant",
    # The character, where the hostname cannot be used (e.g. no hostname yet).
    "name": "Billy",
    # The open Wi-Fi network of the setup mode.
    "hotspot_ssid": "Billy_Bassistant",
    # What the character is, for prompts that generate lines for it.
    "character": "a Big Mouth Billy Bass animatronic fish",
    # Material icon for the character's personas (navigation, persona list).
    "persona_icon": "set_meal",
}

# An SSID is at most 32 bytes; keep it to characters every phone shows well.
_SSID = re.compile(r"[A-Za-z0-9 _.\-]{1,32}")
_LIMITS = {"product_name": 60, "name": 30, "character": 120, "persona_icon": 40}
_ICON = re.compile(r"[a-z0-9_]{1,40}")


def _device_name(root) -> str:
    from billy_runtime.system_packages import configured_device

    try:
        value = configured_device(root)
    except RuntimeError:
        return "fish"
    # "phone", "private.phone" or "private.phone.device:PhoneDevice".
    module = value.split(":", 1)[0].strip()
    parts = module.split(".")
    name = parts[1] if parts[0] == "private" and len(parts) > 1 else parts[0]
    return name if DEVICE_NAME.fullmatch(name) else "fish"


def theme_dir(root=None) -> Path | None:
    """The active device's theme folder, or None for the fish."""
    root = Path(root or INSTALL_ROOT)
    name = _device_name(root)
    if name == "fish":
        return None
    for folder in (
        devices_root(root) / name,
        CODE_ROOT / "application" / "private" / name,
    ):
        if (folder / "theme").is_dir():
            return folder / "theme"
    return None


def _valid(key: str, value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if key == "hotspot_ssid":
        return bool(_SSID.fullmatch(value))
    if key == "persona_icon":
        return bool(_ICON.fullmatch(value))
    return len(value) <= _LIMITS.get(key, 60) and "\n" not in value


def load(root=None) -> dict:
    """The defaults with the device's valid overrides; never raises."""
    theme = dict(DEFAULTS)
    folder = theme_dir(root)
    if folder is None:
        return theme
    try:
        data = json.loads((folder / "theme.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return theme
    if isinstance(data, dict):
        for key in DEFAULTS:
            if _valid(key, data.get(key)):
                theme[key] = data[key].strip()
    return theme


def asset(relative: str, root=None) -> Path | None:
    """The device's replacement for a web interface image, if it has one."""
    folder = theme_dir(root)
    if folder is None or ".." in Path(relative).parts:
        return None
    path = (folder / relative).resolve()
    if folder.resolve() not in path.parents or not path.is_file():
        return None
    return path


def persona_presets(root=None) -> Path | None:
    """The device's own persona presets (theme/personas/<id>/persona.ini).

    They replace the fish's presets, which are written for a singing fish.
    """
    folder = theme_dir(root)
    if folder is None or not (folder / "personas").is_dir():
        return None
    return folder / "personas"


def display_name(current: dict | None = None, hostname: str | None = None) -> str:
    """The name the interface calls this Billy by: the hostname as the header
    shows it ("billy" → "Billy"), so a renamed device reads right. A Pi without
    a hostname of its own falls back to the theme's name."""
    if hostname is None:
        import os

        hostname = os.uname().nodename
    hostname = (hostname or "").split(".", 1)[0]
    if not hostname or hostname in {"localhost", "raspberrypi"}:
        return (current or load())["name"]
    return hostname[:1].upper() + hostname[1:]


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["display_name"]:
        print(display_name())
        return 0
    if len(args) != 1 or args[0] not in DEFAULTS:
        print(
            f"usage: python3 -m billy_runtime.theme {{{','.join(DEFAULTS)}}}",
            file=sys.stderr,
        )
        return 2
    print(load()[args[0]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
