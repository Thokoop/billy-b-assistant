from __future__ import annotations

import asyncio
import configparser
import os

from dotenv import load_dotenv

from .persona import (
    PersonaProfile,
    load_traits_from_ini,
)


# === Paths ===
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT_DIR, ".env")
PERSONA_PATH = os.path.join(ROOT_DIR, "persona.ini")

# === Load .env ===
load_dotenv(dotenv_path=ENV_PATH)

# === Load traits.ini ===
traits = load_traits_from_ini(PERSONA_PATH)

# === Build Personality ===
PERSONALITY = PersonaProfile(**traits)

_config = configparser.ConfigParser()
_config.read(PERSONA_PATH)

# === Instructions for GPT ===
TOOL_INSTRUCTIONS = """
=== CRITICAL: EVERY RESPONSE MUST END WITH conversation_state ===
Speak first, then ALWAYS call conversation_state.
Set expects_follow_up=true if you asked a question or expect the user to continue; otherwise false.
Optionally set one mood_event only when the turn clearly affects Billy's temporary mood.
Do not report system-observed events as mood_event; the app handles those.
NEVER speak or print tool-call text. Tool calls are internal only.

=== TOOL ROUTING ===
Use tool schemas for exact arguments. Call tools when clearly useful.
For Home Assistant, call smart_home_command only for direct commands; if asked to ask/check/confirm first, just speak.
For news/headlines/weather/sports, briefly say "Checking." before get_news_digest.
For memory, only store volunteered user facts/preferences, not answers to your own questions.
For camera/vision or uploaded local files, prefer the matching tool over guessing.

=== RESPONSE FLOW ===
1. Optional normal tools.
2. Spoken answer.
3. conversation_state.
""".strip()

TOOL_INSTRUCTIONS_NO_CONVERSATION_STATE = """
Use tool schemas for exact arguments. Call tools when clearly useful.
For Home Assistant, call smart_home_command only for direct commands.
For news/headlines/weather/sports, briefly say "Checking." before get_news_digest.
For memory, only store volunteered user facts/preferences, not answers to your own questions.
Never speak or print internal tool-call text.
""".strip()

CUSTOM_INSTRUCTIONS = _config.get("META", "instructions")
if _config.has_section("BACKSTORY"):
    BACKSTORY = dict(_config.items("BACKSTORY"))
    BACKSTORY_FACTS = "\n".join([
        f"- {key}: {value}" for key, value in BACKSTORY.items()
    ])
else:
    BACKSTORY = {}
    BACKSTORY_FACTS = (
        "You are an enigma and nobody knows anything about you because the person "
        "talking to you hasn't configured your backstory. You might remind them to do "
        "that."
    )

INSTRUCTIONS = f"""
# Role & Objective
{CUSTOM_INSTRUCTIONS.strip()}
---
# Tools
{TOOL_INSTRUCTIONS.strip()}
---
# Personality & Tone
{PERSONALITY.generate_prompt()}
---
# Context (backstory)
Use your backstory to inspire jokes, metaphors, or occasional references in conversation, staying consistent with your personality.
{BACKSTORY_FACTS}
""".strip()

# === OpenAI Config ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-realtime-mini")
CONVERSATION_STATE_ENABLED_MODELS = {
    "gpt-realtime",
    "gpt-realtime-mini",
    "gpt-realtime-1.5",
    "gpt-realtime-2",
}


def _is_camera_vision_enabled() -> bool:
    hardware = os.getenv("CAMERA_HARDWARE", "none").strip().lower()
    return hardware in {"rpi_camera", "usb_webcam"}


def _filter_vision_instruction_line(text: str) -> str:
    if _is_camera_vision_enabled():
        return text
    lines = text.splitlines()
    filtered = [line for line in lines if not line.strip().startswith("VISION:")]
    return "\n".join(filtered)


def is_conversation_state_enabled(model: str | None = None) -> bool:
    """Whether conversation_state tool/instructions should be enabled."""
    m = (model or os.getenv("OPENAI_MODEL", OPENAI_MODEL) or "").strip()
    return m in CONVERSATION_STATE_ENABLED_MODELS or any(
        m.startswith(f"{enabled}-") for enabled in CONVERSATION_STATE_ENABLED_MODELS
    )


def get_tool_instructions(model: str | None = None) -> str:
    """Return tool instructions appropriate for the selected model."""
    instructions = (
        TOOL_INSTRUCTIONS
        if is_conversation_state_enabled(model)
        else TOOL_INSTRUCTIONS_NO_CONVERSATION_STATE
    )
    return _filter_vision_instruction_line(instructions)


# === XAI Config ===
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-voice-latest").strip()

# === Provider Config ===
REALTIME_AI_PROVIDER = os.getenv("REALTIME_AI_PROVIDER", "openai").strip().lower()

# === Modes ===
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
TEXT_ONLY_MODE = os.getenv("TEXT_ONLY_MODE", "false").lower() == "true"
RUN_MODE = os.getenv("RUN_MODE", "normal").lower()

# === Billy Hardware ===
BILLY_MODEL = os.getenv("BILLY_MODEL", "modern").strip().lower()
BILLY_PINS = os.getenv("BILLY_PINS", "new").strip().lower()

# === Audio Config ===
SPEAKER_PREFERENCE = os.getenv("SPEAKER_PREFERENCE")
MIC_PREFERENCE = os.getenv("MIC_PREFERENCE")
MIC_TIMEOUT_SECONDS = int(os.getenv("MIC_TIMEOUT_SECONDS", "5"))
MIC_GAIN = os.getenv("MIC_GAIN", "max").strip()


def _resolve_silence_threshold():
    raw_value = os.getenv("SILENCE_THRESHOLD")
    if raw_value in (None, ""):
        return 2000.0

    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return 2000.0


SILENCE_THRESHOLD = _resolve_silence_threshold()
CHUNK_MS = int(os.getenv("CHUNK_MS", "40"))
AEC_ENABLED = os.getenv("AEC_ENABLED", "false").lower() == "true"
try:
    AEC_BARGE_IN_SNR_DB = max(
        3.0, min(20.0, float(os.getenv("AEC_BARGE_IN_SNR_DB", "9")))
    )
except (TypeError, ValueError):
    AEC_BARGE_IN_SNR_DB = 9.0
FOLLOW_UP_RETRY_LIMIT = int(os.getenv("FOLLOW_UP_RETRY_LIMIT", "2"))
PLAYBACK_VOLUME = 1
MOUTH_ARTICULATION = int(os.getenv("MOUTH_ARTICULATION", "5"))
TURN_EAGERNESS = os.getenv("TURN_EAGERNESS", "high").strip().lower()
HEAD_RETRACT_DELAY_SECONDS = float(os.getenv("HEAD_RETRACT_DELAY_SECONDS", "1.5"))
WAKE_WORD_ENABLED = os.getenv("WAKE_WORD_ENABLED", "false").lower() == "true"
WAKE_WORD_BACKEND = os.getenv("WAKE_WORD_BACKEND", "porcupine").strip().lower()
WAKE_WORD_COOLDOWN_SECONDS = float(os.getenv("WAKE_WORD_COOLDOWN_SECONDS", "4.0"))
PORCUPINE_ACCESS_KEY = os.getenv("PORCUPINE_ACCESS_KEY", "").strip()
WAKE_WORD_PORCUPINE_KEYWORD_PATH = os.getenv(
    "WAKE_WORD_PORCUPINE_KEYWORD_PATH", "hey-billy.ppn"
).strip()
WAKE_WORD_PORCUPINE_SENSITIVITY = float(
    os.getenv("WAKE_WORD_PORCUPINE_SENSITIVITY", "0.20")
)
WAKE_WORD_OPENWAKEWORD_MODEL_PATH = os.getenv(
    "WAKE_WORD_OPENWAKEWORD_MODEL_PATH", "hey_billy.onnx"
).strip()
WAKE_WORD_OPENWAKEWORD_THRESHOLD = float(
    os.getenv("WAKE_WORD_OPENWAKEWORD_THRESHOLD", "0.50")
)
WAKE_WORD_OPENWAKEWORD_MELSPEC_MODEL_PATH = os.getenv(
    "WAKE_WORD_OPENWAKEWORD_MELSPEC_MODEL_PATH", ""
).strip()
WAKE_WORD_OPENWAKEWORD_EMBEDDING_MODEL_PATH = os.getenv(
    "WAKE_WORD_OPENWAKEWORD_EMBEDDING_MODEL_PATH", ""
).strip()
if TURN_EAGERNESS not in {"low", "medium", "high"}:
    TURN_EAGERNESS = "medium"

# Server VAD parameters based on eagerness.
# Eagerness should mainly control how long Billy waits before ending a turn.
# Keep speech sensitivity consistent so "low" does not become "hard to hear".
SERVER_VAD_PARAMS = {
    "low": {
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 1500,
    },
    "medium": {
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 1000,
    },
    "high": {
        "threshold": 0.7,
        "prefix_padding_ms": 150,
        "silence_duration_ms": 250,
    },
}

# === GPIO Config ===
BUTTON_PIN = 27 if BILLY_PINS == "legacy" else 24  # legacy=pin 13, new=pin 18
STATUS_LED_ENABLED = os.getenv("STATUS_LED_ENABLED", "false").lower() == "true"
STATUS_LED_BACKEND = os.getenv("STATUS_LED_BACKEND", "auto").strip().lower()
STATUS_LED_PIN = int(os.getenv("STATUS_LED_PIN", "18"))
STATUS_LED_COUNT = max(1, int(os.getenv("STATUS_LED_COUNT", "1")))
STATUS_LED_BRIGHTNESS = float(os.getenv("STATUS_LED_BRIGHTNESS", "0.2"))
STATUS_LED_DMA_CHANNEL = int(os.getenv("STATUS_LED_DMA_CHANNEL", "10"))
STATUS_LED_PWM_CHANNEL = int(os.getenv("STATUS_LED_PWM_CHANNEL", "0"))

# === MQTT Config ===
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "0"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")

# === Home Assistant Config ===
HA_HOST = os.getenv("HA_HOST")
HA_TOKEN = os.getenv("HA_TOKEN")
HA_LANG = os.getenv("HA_LANG", "en")

# === Personality Config ===
ALLOW_UPDATE_PERSONALITY_INI = (
    os.getenv("ALLOW_UPDATE_PERSONALITY_INI", "true").lower() == "true"
)

# === Software Config ===
FLASK_PORT = int(os.getenv("FLASK_PORT", "80"))
SHOW_SUPPORT = os.getenv("SHOW_SUPPORT", True)
FORCE_PASS_CHANGE = os.getenv("FORCE_PASS_CHANGE", "false").lower() == "true"
SHOW_RC_VERSIONS = os.getenv("SHOW_RC_VERSIONS", "False")
FLAP_ON_BOOT = os.getenv("FLAP_ON_BOOT", "false").lower() == "true"
MOCKFISH = os.getenv("MOCKFISH", "false").lower() == "true"
WIFI_COUNTRY = (os.getenv("WIFI_COUNTRY", "US") or "").strip().upper() or "US"
WIFI_ONBOARDING_MODE = os.getenv("WIFI_ONBOARDING_MODE", "legacy").strip().lower()
if WIFI_ONBOARDING_MODE not in {"legacy", "unified"}:
    WIFI_ONBOARDING_MODE = "legacy"

# === News Digest Config ===
NEWS_REQUEST_TIMEOUT_SECONDS = float(os.getenv("NEWS_REQUEST_TIMEOUT_SECONDS", "6"))

# === Camera Vision Config ===
CAMERA_HARDWARE = os.getenv("CAMERA_HARDWARE", "none").strip().lower()
if CAMERA_HARDWARE not in {"none", "rpi_camera", "usb_webcam"}:
    CAMERA_HARDWARE = "none"
# Kept under its original setting name for compatibility. Runtime camera handling
# falls back between the current rpicam-still and legacy libcamera-still commands.
LIBCAMERA_STILL_BIN = os.getenv("LIBCAMERA_STILL_BIN", "rpicam-still").strip()
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg").strip()
CAMERA_DEVICE_INDEX = int(os.getenv("CAMERA_DEVICE_INDEX", "0"))
CAMERA_ROTATION = int(os.getenv("CAMERA_ROTATION", "0"))
if CAMERA_ROTATION not in {0, 90, 180, 270}:
    CAMERA_ROTATION = 0
CAMERA_CAPTURE_WIDTH = int(os.getenv("CAMERA_CAPTURE_WIDTH", "1280"))
CAMERA_CAPTURE_HEIGHT = int(os.getenv("CAMERA_CAPTURE_HEIGHT", "720"))
CAMERA_CAPTURE_TIMEOUT_SECONDS = float(os.getenv("CAMERA_CAPTURE_TIMEOUT_SECONDS", "8"))

# === User Profile Config ===
DEFAULT_USER = os.getenv("DEFAULT_USER", "guest").strip()
CURRENT_USER = os.getenv("CURRENT_USER", "").strip()


def is_classic_billy():
    return os.getenv("BILLY_MODEL", "modern").strip().lower() == "classic"


try:
    MAIN_LOOP = asyncio.get_event_loop()
except RuntimeError:
    MAIN_LOOP = None
