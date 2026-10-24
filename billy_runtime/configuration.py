"""Stable dotenv parsing and transactions, without importing core or devices.

Bootstrap and emergency recovery use validate_bootstrap: only file structure
and the listen port belong to the fixed installation. Normal settings saves
and workers use validate, which loads the selected release's application schema.
The configuration file and recovery snapshots remain shared across releases.
"""

import hashlib
import io
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .paths import INSTALL_ROOT
from .storage import atomic_write, transaction


ROOT = INSTALL_ROOT
BOOTSTRAP_KEYS = {'FLASK_PORT'}


class ConfigError(ValueError):
    pass


class ConflictError(ConfigError):
    pass


def revision(content):
    return hashlib.sha256(
        content if isinstance(content, bytes) else content.encode()
    ).hexdigest()


def _parse(text, *, complete=False):
    if not isinstance(text, str) or '\x00' in text:
        raise ConfigError('Configuration must be UTF-8 text without NUL characters')
    bindings = list(parse_stream(io.StringIO(text)))
    seen = {}
    for binding in bindings:
        line = binding.original.line
        if binding.error:
            raise ConfigError(f'Invalid configuration syntax at line {line}')
        if binding.key is None:
            continue
        key = binding.key
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or binding.value is None:
            raise ConfigError(f'Invalid assignment at line {line}')
        if key in seen:
            raise ConfigError(f'Duplicate key {key} at line {line}')
        seen[key] = line
    if complete and not seen:
        raise ConfigError('Configuration is empty or contains no settings')
    values = dict(dotenv_values(stream=io.StringIO(text)))
    return values, seen


def _validate_bootstrap_values(values, lines):
    # Only the stable listen socket needs a setting before a release is loaded.
    # Providers, GPIO/audio choices and other application settings belong to it.
    if 'FLASK_PORT' in values:
        try:
            if not 1 <= int(values['FLASK_PORT']) <= 65535:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ConfigError(
                f"Invalid value for FLASK_PORT at line {lines['FLASK_PORT']}"
            ) from None


def validate_bootstrap(text, *, complete=False):
    """Check file structure and the listen port, independent of release features."""
    values, lines = _parse(text, complete=complete)
    _validate_bootstrap_values(values, lines)
    return values


def validate(text, *, complete=False):
    """Full validation for normal workers and settings saves in this release."""
    values, lines = _parse(text, complete=complete)
    _validate_bootstrap_values(values, lines)
    # Lazy on purpose: a missing/broken release schema must not break rescue.
    from .configuration_schema import validate_values

    validate_values(values, lines)
    return values


_original_environment = dict(os.environ)
_loaded_keys = set()


def apply_environment(text, *, override=True, validator=None):
    global _loaded_keys
    values = (validator or validate)(text, complete=True)
    for key in _loaded_keys - values.keys():
        if key in _original_environment:
            os.environ[key] = _original_environment[key]
        else:
            os.environ.pop(key, None)
    for key, value in values.items():
        if override or key not in _original_environment:
            os.environ[key] = value
    _loaded_keys = set(values)
    os.environ["BILLY_APPLIED_REVISION"] = revision(text)
    return values


def load_environment(path, *, override=True):
    return apply_environment(Path(path).read_text(encoding='utf-8'), override=override)


def save_env(path, content, *, expected=None, validator=None):
    path = Path(path).absolute()
    with transaction(path):
        old = path.read_bytes() if path.exists() else b''
        if expected is not None and revision(old) != expected:
            raise ConflictError('Configuration changed; reload before saving')
        (validator or validate)(content, complete=True)
        atomic_write(path, content)
    return revision(content)


def save_bootstrap_env(path, content, *, expected=None):
    """Emergency edits are fully checked by the selected worker on retry."""
    return save_env(path, content, expected=expected, validator=validate_bootstrap)


def patch_env(path, changes):
    path = Path(path).absolute()
    with transaction(path):
        text = path.read_text(encoding='utf-8')
        # Use original binding spans to preserve comments, multiline values and unknown keys.
        bindings = list(parse_stream(io.StringIO(text)))
        if any(b.error for b in bindings):
            validate(text)
        remaining = dict(changes)
        result = []
        for binding in bindings:
            if binding.key not in changes:
                result.append(binding.original.string)
                continue
            key = binding.key
            if key not in remaining:
                continue
            value = str(remaining.pop(key)).replace('\\', '\\\\').replace("'", "\\'")
            result.append(f"{key}='{value}'\n")
        for key, value in remaining.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ConfigError('Invalid setting name')
            value = str(value).replace('\\', '\\\\').replace("'", "\\'")
            result.append(f"\n{key}='{value}'\n")
        updated = ''.join(result)
        validate(updated)
        atomic_write(path, updated)
    return revision(updated)


def example_defaults(path):
    try:
        text = Path(path).read_text(encoding='utf-8')
        values = validate_bootstrap(text)
        if values:
            return text
        return (
            text.rstrip()
            + "\nFLASK_PORT=80\nWAKE_WORD_ENABLED=false\nBLUETOOTH_AUTO_SWITCH=false\n"
        )
    except (OSError, UnicodeError, ConfigError):
        return "FLASK_PORT=80\nWAKE_WORD_ENABLED=false\nBLUETOOTH_AUTO_SWITCH=false\n"
