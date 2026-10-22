"""Device preferences shared by the web UI and core, across software releases."""

import json
import logging
import re

from .paths import INSTALL_ROOT
from .storage import atomic_write, transaction


PATH = INSTALL_ROOT / "state" / "bluetooth-preferences.json"


def _read():
    try:
        data = json.loads(PATH.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(data, list) or any(
        not isinstance(address, str)
        or not re.fullmatch(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", address)
        for address in data
    ):
        raise ValueError("Invalid Bluetooth preferences")
    return data


def selected():
    try:
        return _read()
    except (OSError, ValueError):
        logging.getLogger(__name__).warning(
            "Cannot read Bluetooth auto-connect preferences"
        )
        return []


def set_enabled(address, enabled):
    if not isinstance(enabled, bool):
        raise ValueError("Auto-connect must be true or false")
    if not isinstance(address, str) or not re.fullmatch(
        r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", address
    ):
        raise ValueError("Invalid Bluetooth address")
    with transaction(PATH):
        addresses = (
            _read()
        )  # Preserve a damaged file rather than silently replacing it.
        if enabled and address not in addresses:
            addresses.append(address)
        elif not enabled and address in addresses:
            addresses.remove(address)
        else:
            return
        atomic_write(PATH, json.dumps(addresses) + "\n")
