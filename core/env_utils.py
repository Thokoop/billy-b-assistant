"""Helpers for updating Billy's .env file without duplicate assignments."""

from __future__ import annotations

import re
from pathlib import Path


def set_env_key(env_path, key: str, value) -> None:
    """Set one key, replacing commented/active copies and removing duplicates."""
    path = Path(env_path)
    if not path.exists():
        raise FileNotFoundError(f".env file does not exist: {path}")

    key_pattern = re.compile(rf"^\s*(?:#\s*)?(?:export\s+)?{re.escape(str(key))}\s*=")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    replacement = f"{key}={value}\n"
    updated = []
    replaced = False

    for line in lines:
        if key_pattern.match(line):
            if not replaced:
                updated.append(replacement)
                replaced = True
            continue
        updated.append(line)

    if not replaced:
        if updated and not updated[-1].endswith(("\n", "\r")):
            updated[-1] += "\n"
        updated.append(replacement)

    path.write_text("".join(updated), encoding="utf-8")
