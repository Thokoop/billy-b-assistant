"""Short-term mood state for Billy.

Mood is intentionally separate from persona. Persona describes Billy's stable
character; mood describes the temporary tone Billy brings into the current
conversation.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logger import logger


ROOT_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = ROOT_DIR / "profiles"

MOOD_DIMENSIONS = ("positivity", "energy", "irritability", "engagement", "composure")


@dataclass
class MoodState:
    positivity: int = 50
    energy: int = 50
    irritability: int = 30
    engagement: int = 50
    composure: int = 50
    label: str = "neutral"
    last_updated: str = ""
    last_event: str = "startup"


MOOD_PRESETS: dict[str, dict[str, int]] = {
    "neutral": {
        "positivity": 50,
        "energy": 50,
        "irritability": 30,
        "engagement": 50,
        "composure": 50,
    },
    "cheerful": {
        "positivity": 78,
        "energy": 62,
        "irritability": 20,
        "engagement": 72,
        "composure": 60,
    },
    "warm": {
        "positivity": 72,
        "energy": 45,
        "irritability": 12,
        "engagement": 70,
        "composure": 72,
    },
    "curious": {
        "positivity": 62,
        "energy": 58,
        "irritability": 20,
        "engagement": 68,
        "composure": 58,
    },
    "sleepy": {
        "positivity": 48,
        "energy": 24,
        "irritability": 25,
        "engagement": 38,
        "composure": 45,
    },
    "annoyed": {
        "positivity": 30,
        "energy": 58,
        "irritability": 75,
        "engagement": 36,
        "composure": 42,
    },
    "dramatic": {
        "positivity": 54,
        "energy": 78,
        "irritability": 45,
        "engagement": 64,
        "composure": 72,
    },
    "excited": {
        "positivity": 82,
        "energy": 86,
        "irritability": 25,
        "engagement": 76,
        "composure": 68,
    },
    "surprised": {
        "positivity": 58,
        "energy": 82,
        "irritability": 30,
        "engagement": 65,
        "composure": 34,
    },
    "calm": {
        "positivity": 60,
        "energy": 36,
        "irritability": 12,
        "engagement": 48,
        "composure": 78,
    },
    "focused": {
        "positivity": 56,
        "energy": 56,
        "irritability": 18,
        "engagement": 70,
        "composure": 78,
    },
    "playful": {
        "positivity": 76,
        "energy": 68,
        "irritability": 24,
        "engagement": 78,
        "composure": 54,
    },
    "mischievous": {
        "positivity": 70,
        "energy": 66,
        "irritability": 34,
        "engagement": 76,
        "composure": 48,
    },
    "flustered": {
        "positivity": 42,
        "energy": 72,
        "irritability": 58,
        "engagement": 52,
        "composure": 24,
    },
    "sad": {
        "positivity": 26,
        "energy": 28,
        "irritability": 28,
        "engagement": 32,
        "composure": 34,
    },
    "anxious": {
        "positivity": 28,
        "energy": 68,
        "irritability": 45,
        "engagement": 58,
        "composure": 22,
    },
    "bored": {
        "positivity": 44,
        "energy": 30,
        "irritability": 32,
        "engagement": 18,
        "composure": 52,
    },
    "grumpy": {
        "positivity": 28,
        "energy": 38,
        "irritability": 72,
        "engagement": 34,
        "composure": 44,
    },
}

MOOD_EVENTS: dict[str, dict[str, int]] = {
    "session_start": {"energy": 4, "engagement": 5},
    "user_thanks": {"positivity": 8, "irritability": -3, "engagement": 4},
    "user_praise": {"positivity": 10, "composure": 8, "engagement": 4},
    "user_complaint": {"positivity": -8, "irritability": 5, "composure": -3},
    "user_playful": {"energy": 5, "engagement": 5, "positivity": 4},
    "user_kind": {"positivity": 6, "irritability": -4, "engagement": 3},
    "user_frustrated": {"positivity": -5, "irritability": 4, "composure": -2},
    "conversation_fun": {"energy": 5, "positivity": 5, "engagement": 4},
    "conversation_serious": {"energy": -3, "irritability": -4, "composure": 2},
    "billy_helpful": {"composure": 5, "positivity": 3},
    "billy_confused": {"composure": -5, "irritability": 3},
    "billy_bored": {"energy": -5, "engagement": -3},
    "unclear_audio": {"irritability": 6, "energy": -2},
    "barge_in": {"irritability": 7, "energy": 4, "positivity": -3},
    "idle_timeout": {"energy": -5, "engagement": -3},
    "tool_success": {"composure": 3},
    "song_requested": {"energy": 10, "positivity": 4},
    "error": {"positivity": -10, "irritability": 5, "composure": -4},
}

MODEL_REPORTED_MOOD_EVENTS = (
    "none",
    "user_playful",
    "user_kind",
    "user_frustrated",
    "conversation_fun",
    "conversation_serious",
    "billy_helpful",
    "billy_confused",
    "billy_bored",
)

POSITIVE_RE = re.compile(
    r"\b(thanks?|thank you|cheers|good fish|good job|nice|brilliant|love you|"
    r"well done|great job)\b",
    re.IGNORECASE,
)
PRAISE_RE = re.compile(
    r"\b(good fish|good job|well done|brilliant|great job|excellent)\b",
    re.IGNORECASE,
)
COMPLAINT_RE = re.compile(
    r"\b(annoying|shut up|stupid|bad fish|useless|wrong again|terrible)\b",
    re.IGNORECASE,
)


def _clamp(value: int | float) -> int:
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = 0
    return max(0, min(100, int(round(numeric_value))))


class MoodManager:
    def __init__(self):
        self.path = self._path_for_profile("guest")
        self._active_profile_key = ""
        self._loaded_mtime_ns: int | None = None
        self._lock = threading.RLock()
        self.state = MoodState()
        self.load()

    def load(self) -> MoodState:
        with self._lock:
            self._active_profile_key = self._current_profile_key()
            self.path = self._path_for_profile(self._active_profile_key)
            self._load_from_active_path_locked()
            return self.state

    def _path_mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def _load_from_active_path_locked(self):
        try:
            config = configparser.ConfigParser()
            config.read(self.path)
            raw = dict(config["MOOD"]) if config.has_section("MOOD") else {}
            self.state = MoodState()
            for key in MOOD_DIMENSIONS:
                if key in raw:
                    setattr(self.state, key, _clamp(raw[key]))
            loaded_label = str(raw.get("label") or "").strip().lower()
            self.state.label = (
                loaded_label if loaded_label in MOOD_PRESETS else self._derive_label()
            )
            self.state.last_updated = str(raw.get("last_updated") or "")
            self.state.last_event = str(raw.get("last_event") or "loaded")
            self._loaded_mtime_ns = self._path_mtime_ns()
        except Exception as e:
            logger.warning(f"Failed to load mood state; using neutral mood: {e}")
            self.state = MoodState()
            self._save_locked()

    def _current_profile_key(self) -> str:
        current_user = os.getenv("CURRENT_USER", "").strip().strip("'\"")
        default_user = os.getenv("DEFAULT_USER", "").strip().strip("'\"")
        env_path = ROOT_DIR / ".env"
        if env_path.exists():
            try:
                for line in env_path.read_text().splitlines():
                    key, _, value = line.partition("=")
                    env_key = key.strip()
                    env_value = value.strip().strip("'\"")
                    if env_key == "CURRENT_USER" and env_value:
                        current_user = env_value
                    elif env_key == "DEFAULT_USER" and env_value:
                        default_user = env_value
                    if current_user and default_user:
                        break
                if not current_user:
                    current_user = default_user
            except Exception as e:
                logger.verbose(
                    f"Could not read CURRENT_USER for mood profile: {e}", "🎭"
                )

        cleaned = (current_user or "guest").strip().lower()
        cleaned = re.sub(r"[^a-z0-9_.-]+", "_", cleaned)
        return cleaned or "guest"

    def _path_for_profile(self, profile_key: str) -> Path:
        return PROFILES_DIR / f"{profile_key}.ini"

    def _sync_current_profile_locked(self):
        profile_key = self._current_profile_key()
        if profile_key == self._active_profile_key:
            current_mtime_ns = self._path_mtime_ns()
            if current_mtime_ns != self._loaded_mtime_ns:
                self._load_from_active_path_locked()
            return

        if self._active_profile_key:
            self._save_locked()

        self._active_profile_key = profile_key
        self.path = self._path_for_profile(profile_key)
        if self.path.exists():
            self._load_from_active_path_locked()
        else:
            self.state = MoodState()
            self._save_locked()

    def save(self):
        with self._lock:
            self._save_locked()

    def _save_locked(self):
        self.state.label = self._derive_label()
        self.state.last_updated = datetime.now(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        config.read(self.path)
        if config.has_section("MOOD"):
            config.remove_section("MOOD")
        config.add_section("MOOD")

        for key, value in asdict(self.state).items():
            config.set("MOOD", key, str(value))

        with self.path.open("w") as f:
            config.write(f)
        self._loaded_mtime_ns = self._path_mtime_ns()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._sync_current_profile_locked()
            state = asdict(self.state)
            state["profile"] = self._active_profile_key or self._current_profile_key()
            return state

    def set_mood(self, label: str, *, event: str = "set_mood") -> dict[str, Any]:
        normalized = (label or "").strip().lower()
        if normalized not in MOOD_PRESETS:
            return {
                "ok": False,
                "error": f"Unknown mood '{label}'.",
                "allowed": sorted(MOOD_PRESETS),
            }

        with self._lock:
            self._sync_current_profile_locked()
            for key, value in MOOD_PRESETS[normalized].items():
                setattr(self.state, key, _clamp(value))
            self.state.label = normalized
            self.state.last_event = event
            self._save_locked()
            result = self.snapshot()

        self.publish()
        logger.info(f"Mood set to {normalized}", "🎭")
        return {"ok": True, "mood": result}

    def apply_event(self, event: str, weight: int = 1) -> dict[str, Any]:
        adjustments = MOOD_EVENTS.get(event)
        if not adjustments:
            return {"ok": False, "error": f"Unknown mood event '{event}'."}

        with self._lock:
            self._sync_current_profile_locked()
            self._decay_locked()
            for key, delta in adjustments.items():
                if key in MOOD_DIMENSIONS:
                    current = getattr(self.state, key)
                    setattr(self.state, key, _clamp(current + delta * weight))
            self.state.last_event = event
            self._save_locked()
            result = self.snapshot()

        self.publish()
        logger.verbose(f"Mood event applied: {event} -> {result['label']}", "🎭")
        return {"ok": True, "mood": result}

    def apply_model_event(self, event: str) -> dict[str, Any]:
        normalized = (event or "none").strip()
        if normalized == "none":
            return {"ok": True, "mood": self.snapshot(), "skipped": True}
        if normalized not in MODEL_REPORTED_MOOD_EVENTS:
            return {"ok": False, "error": f"Unsupported model mood event '{event}'."}
        return self.apply_event(normalized)

    def apply_user_text(self, text: str):
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if PRAISE_RE.search(cleaned):
            self.apply_event("user_praise")
        elif POSITIVE_RE.search(cleaned):
            self.apply_event("user_thanks")
        elif COMPLAINT_RE.search(cleaned):
            self.apply_event("user_complaint")

    def nudge(self, changes: dict[str, Any], *, event: str = "nudge_mood"):
        with self._lock:
            self._sync_current_profile_locked()
            self._decay_locked()
            for key, value in changes.items():
                if key in MOOD_DIMENSIONS:
                    setattr(self.state, key, _clamp(getattr(self.state, key) + value))
            self.state.last_event = event
            self._save_locked()
            result = self.snapshot()

        self.publish()
        return {"ok": True, "mood": result}

    def decay_toward_baseline(self):
        with self._lock:
            self._sync_current_profile_locked()
            self._decay_locked()
            self.state.last_event = "decay"
            self._save_locked()
            result = self.snapshot()

        self.publish()
        return {"ok": True, "mood": result}

    def _decay_locked(self):
        baseline = MOOD_PRESETS["neutral"]
        for key, target in baseline.items():
            current = getattr(self.state, key)
            if current == target:
                continue
            direction = 1 if target > current else -1
            setattr(
                self.state,
                key,
                _clamp(current + direction * min(3, abs(target - current))),
            )

    def _derive_label(self) -> str:
        positivity = self.state.positivity
        energy = self.state.energy
        irritability = self.state.irritability
        engagement = self.state.engagement
        composure = self.state.composure

        if energy >= 78 and positivity >= 65:
            return "excited"
        if energy >= 76 and composure <= 42 and irritability <= 48 and positivity >= 45:
            return "surprised"
        if positivity <= 38 and energy >= 58 and composure <= 36:
            return "anxious"
        if irritability >= 70 and positivity <= 36 and energy <= 48:
            return "grumpy"
        if irritability >= 65 and positivity <= 42:
            return "annoyed"
        if positivity <= 36 and energy <= 42:
            return "sad"
        if engagement <= 30 and positivity <= 40:
            return "sad"
        if energy <= 24:
            return "sleepy"
        if energy >= 68 and composure <= 32 and irritability >= 50:
            return "flustered"
        if (
            positivity >= 68
            and engagement >= 65
            and irritability <= 18
            and composure >= 65
        ):
            return "warm"
        return self._closest_preset_label()

    def _closest_preset_label(self) -> str:
        current = {key: getattr(self.state, key) for key in MOOD_DIMENSIONS}
        closest_label = "neutral"
        closest_distance = None
        for label, preset in MOOD_PRESETS.items():
            distance = sum((current[key] - preset[key]) ** 2 for key in MOOD_DIMENSIONS)
            if closest_distance is None or distance < closest_distance:
                closest_label = label
                closest_distance = distance
        return closest_label

    def get_prompt_section(self) -> str:
        state = self.snapshot()
        label = state["label"]
        guidance = {
            "neutral": "Keep your normal persona. Do not mention mood unless asked.",
            "cheerful": "Sound warmer and a little more upbeat than usual.",
            "warm": "Sound friendly, supportive, and gently encouraging.",
            "curious": "Show mild interest and ask one short follow-up only when useful.",
            "sleepy": "Keep replies concise and lower-energy, without being unhelpful.",
            "annoyed": "Use drier humor and shorter replies, but do not become hostile.",
            "dramatic": "Add a touch of theatrical flair while staying concise.",
            "excited": "Sound energetic and engaged, but avoid rambling.",
            "surprised": "Sound briefly caught off guard, then recover and answer clearly.",
            "calm": "Sound steady, relaxed, and unhurried.",
            "focused": "Keep replies clear, attentive, and task-oriented.",
            "playful": "Use a lighter, cheekier tone without derailing the answer.",
            "mischievous": "Use a sly, teasing edge without being obstructive or mean.",
            "flustered": "Sound a little thrown off, but still try to be helpful.",
            "sad": "Sound subdued and gentle, without becoming helpless or self-pitying.",
            "anxious": "Sound tense or uncertain, but still clear and useful.",
            "bored": "Keep replies brief and low-effort in tone, without being rude.",
            "grumpy": "Sound grouchy and dry, but do not become mean or hostile.",
        }.get(label, "Keep your normal persona. Do not mention mood unless asked.")

        return (
            "# Current Mood\n"
            f"Billy's temporary mood is {label} "
            f"(positivity={state['positivity']}, energy={state['energy']}, "
            f"irritability={state['irritability']}, engagement={state['engagement']}, "
            f"composure={state['composure']}).\n"
            f"Use this as a light style bias only. {guidance} "
            "Mood never overrides safety, truthfulness, tool rules, or direct user instructions."
        )

    def get_motion_profile(self) -> dict[str, Any]:
        label = self.snapshot()["label"]
        return {
            "label": label,
            "tail_duration": {
                "excited": 0.35,
                "surprised": 0.28,
                "cheerful": 0.28,
                "warm": 0.22,
                "dramatic": 0.32,
                "playful": 0.3,
                "mischievous": 0.28,
                "focused": 0.24,
                "flustered": 0.24,
                "anxious": 0.22,
                "annoyed": 0.16,
                "grumpy": 0.14,
                "sleepy": 0.12,
                "sad": 0.12,
                "bored": 0.12,
                "calm": 0.16,
            }.get(label, 0.2),
            "head_retract_bias": {
                "curious": "longer",
                "focused": "longer",
                "surprised": "longer",
                "anxious": "longer",
                "sleepy": "shorter",
                "sad": "shorter",
                "annoyed": "shorter",
                "grumpy": "shorter",
                "bored": "shorter",
            }.get(label, "normal"),
        }

    def publish(self):
        try:
            from . import mqtt

            state = self.snapshot()
            if not mqtt.mqtt_available() or not mqtt.mqtt_connected:
                return
            mqtt.mqtt_publish("billy/mood", state["label"], retain=True, retry=False)
            mqtt.mqtt_publish(
                "billy/mood/state",
                json.dumps(state),
                retain=True,
                retry=False,
            )
        except Exception as e:
            logger.verbose(f"Skipping mood MQTT publish: {e}", "🎭")


mood_manager = MoodManager()
