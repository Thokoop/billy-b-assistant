"""Strict dotenv parsing and transactions without importing core or devices."""

import hashlib
import io
import math
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .paths import INSTALL_ROOT
from .storage import atomic_write, transaction


ROOT = INSTALL_ROOT
INTEGERS = {
    'FLASK_PORT': (1, 65535),
    'MQTT_PORT': (1, 65535),
    'MIC_TIMEOUT_SECONDS': (1, 3600),
    'CHUNK_MS': (1, 1000),
    'FOLLOW_UP_RETRY_LIMIT': (0, 100),
    'MOUTH_ARTICULATION': (0, 100),
    'STATUS_LED_PIN': (0, 53),
    'STATUS_LED_COUNT': (1, 1024),
    'STATUS_LED_DMA_CHANNEL': (0, 14),
    'STATUS_LED_PWM_CHANNEL': (0, 1),
    'CAMERA_DEVICE_INDEX': (0, 1024),
    'CAMERA_ROTATION': (0, 359),
    'CAMERA_CAPTURE_WIDTH': (1, 16384),
    'CAMERA_CAPTURE_HEIGHT': (1, 16384),
}
FLOATS = {
    'SILENCE_THRESHOLD': (0, 32768),
    'AEC_BARGE_IN_SNR_DB': (3, 20),
    'MOUTH_BOOST': (0, 10),
    'HEAD_RETRACT_DELAY_SECONDS': (0, 3600),
    'MOOD_INSTRUCTIONS_MIN_INTERVAL_SECONDS': (0, 86400),
    'WAKE_WORD_COOLDOWN_SECONDS': (0, 3600),
    'WAKE_WORD_PORCUPINE_SENSITIVITY': (0, 1),
    'WAKE_WORD_OPENWAKEWORD_THRESHOLD': (0, 1),
    'STATUS_LED_BRIGHTNESS': (0, 1),
    'NEWS_REQUEST_TIMEOUT_SECONDS': (0.01, 3600),
    'WEB_SEARCH_TIMEOUT_SECONDS': (0.01, 3600),
    'CAMERA_CAPTURE_TIMEOUT_SECONDS': (0.01, 3600),
}
BOOLEANS = set([
    'TEXT_ONLY_MODE',
    'MIC_TIMEOUT_TAIL_FLAP',
    'AEC_ENABLED',
    'BLUETOOTH_CALL_AEC',
    'BLUETOOTH_AUTO_SWITCH',
    'MOOD_INSTRUCTIONS_ENABLED',
    'WAKE_WORD_ENABLED',
    'STATUS_LED_ENABLED',
    'ALLOW_UPDATE_PERSONALITY_INI',
    'SHOW_SUPPORT',
    'FORCE_PASS_CHANGE',
    'SHOW_RC_VERSIONS',
    'FLAP_ON_BOOT',
    'SHOW_TOOLTIPS',
    'MOCKFISH',
    'WEB_SEARCH_ENABLED',
])
ENUMS = {
    'REALTIME_AI_PROVIDER': {'openai', 'xai'},
    'BILLY_MODEL': {'modern', 'classic'},
    'BILLY_PINS': {'new', 'legacy'},
    'TURN_EAGERNESS': {'low', 'medium', 'high'},
    'WAKE_WORD_BACKEND': {'porcupine', 'openwakeword'},
    'CAMERA_HARDWARE': {'none', 'rpi_camera', 'usb_webcam'},
    'WEB_SEARCH_CONTEXT_SIZE': {'low', 'medium', 'high'},
}


class ConfigError(ValueError):
    pass


class ConflictError(ConfigError):
    pass


def revision(content):
    return hashlib.sha256(
        content if isinstance(content, bytes) else content.encode()
    ).hexdigest()


def validate(text, *, complete=False):
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
    for key, value in values.items():
        label = f'{key} at line {seen[key]}'
        try:
            if key in INTEGERS or key in FLOATS:
                # Preserve existing optional empty SILENCE_THRESHOLD semantics.
                if key == 'SILENCE_THRESHOLD' and value == '':
                    continue
                number = int(value) if key in INTEGERS else float(value)
                low, high = (INTEGERS | FLOATS)[key]
                if not math.isfinite(number) or not low <= number <= high:
                    raise ValueError
            if key in BOOLEANS and value.lower() not in {'true', 'false'}:
                raise ValueError
            if key in ENUMS and value.lower() not in ENUMS[key]:
                raise ValueError
            if (
                key == 'MIC_GAIN'
                and value.lower() not in {'', 'max', 'maximum', 'default'}
                and (not math.isfinite(float(value)) or float(value) < 0)
            ):
                raise ValueError
            if (
                key in {'CURRENT_USER', 'DEFAULT_USER'}
                and value
                and (not re.fullmatch(r'[\w -]+', value) or value in {'.', '..'})
            ):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ConfigError(f'Invalid value for {label}') from None
    return values


_original_environment = dict(os.environ)
_loaded_keys = set()


def apply_environment(text, *, override=True):
    global _loaded_keys
    values = validate(text, complete=True)
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


def save_env(path, content, *, expected=None):
    path = Path(path).absolute()
    with transaction(path):
        old = path.read_bytes() if path.exists() else b''
        if expected is not None and revision(old) != expected:
            raise ConflictError('Configuration changed; reload before saving')
        validate(content, complete=True)
        atomic_write(path, content)
    return revision(content)


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
        values = validate(text)
        if values:
            return text
        return (
            text.rstrip()
            + "\nFLASK_PORT=80\nWAKE_WORD_ENABLED=false\nBLUETOOTH_AUTO_SWITCH=false\n"
        )
    except (OSError, UnicodeError, ConfigError):
        return "FLASK_PORT=80\nWAKE_WORD_ENABLED=false\nBLUETOOTH_AUTO_SWITCH=false\n"
