"""Settings supported by this release; never imported by bootstrap/recovery.

Keep application-specific checks here so future releases can add providers,
models and settings without updating the independent service supervisor.
"""

import math
import re

from .configuration import ConfigError


INTEGERS = {
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
    'UPDATE_CHANNEL': {'stable', 'rc', 'beta'},
    'REALTIME_AI_PROVIDER': {'openai', 'xai', 'gpt-live'},
    'BILLY_MODEL': {'modern', 'classic'},
    'BILLY_PINS': {'new', 'legacy'},
    'TURN_EAGERNESS': {'low', 'medium', 'high'},
    'WAKE_WORD_BACKEND': {'porcupine', 'openwakeword'},
    'CAMERA_HARDWARE': {'none', 'rpi_camera', 'usb_webcam'},
    'WEB_SEARCH_CONTEXT_SIZE': {'low', 'medium', 'high'},
}

ENVIRONMENT_KEYS = (
    set(INTEGERS)
    | set(FLOATS)
    | BOOLEANS
    | set(ENUMS)
    | {'OPENAI_API_KEY', 'XAI_API_KEY', 'MIC_PREFERENCE', 'SPEAKER_PREFERENCE'}
)


def validate_values(values, lines):
    """Validate application settings without loading core or touching hardware."""
    for key, value in values.items():
        label = f'{key} at line {lines[key]}'
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
