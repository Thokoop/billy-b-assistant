"""The ringtones Billy picks from when a paired phone rings.

Works like the wake-up sounds: a list the user can add to, generate in the
current persona's voice, upload their own file for, play and remove. Billy
plays a random one for every incoming call.

The list starts with the built-in tones, which ship with the code. Removing
one only takes it off the list; "restore" puts every missing built-in tone
back, so the defaults can always be recovered. The list and the user's own
tones live with the user's data, so updates keep them.
"""

from __future__ import annotations

import json
import os
import random
import re
import tempfile
import threading
import wave
from pathlib import Path

from billy_runtime.paths import CODE_ROOT, INSTALL_ROOT


DEFAULT_DIR = CODE_ROOT / "sounds" / "ringtones"
USER_DIR = INSTALL_ROOT / "sounds" / "ringtones"
MANIFEST = USER_DIR / "ringtones.json"
# Built-in tones: id -> name. The WAV is DEFAULT_DIR/<number>.wav.
DEFAULTS = {
    "default-1": "Classic bell",
    "default-2": "Trill",
    "default-3": "Digital",
    "default-4": "Chime",
    "default-5": "Old bell",
}
RATE = 24000  # Billy's playback format: 24 kHz, mono, 16-bit

_lock = threading.Lock()


def _default_file(ringtone_id: str) -> Path:
    return DEFAULT_DIR / f"{ringtone_id.removeprefix('default-')}.wav"


def _user_file(ringtone_id: str) -> Path:
    return USER_DIR / f"{ringtone_id}.wav"


def path_for(entry: dict) -> Path:
    if entry.get("kind") == "default":
        return _default_file(entry["id"])
    return _user_file(entry["id"])


def _defaults() -> list[dict]:
    return [
        {"id": key, "kind": "default", "name": name} for key, name in DEFAULTS.items()
    ]


def _read() -> list[dict]:
    try:
        data = json.loads(MANIFEST.read_text())
        entries = data.get("ringtones")
        if isinstance(entries, list):
            return [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("id")
            ]
    except (OSError, ValueError):
        pass
    return _defaults()


def _write(entries: list[dict]) -> None:
    USER_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=USER_DIR, suffix=".tmp", delete=False
    ) as pending:
        json.dump({"ringtones": entries}, pending, indent=2)
        temporary = pending.name
    os.replace(temporary, MANIFEST)


def entries() -> list[dict]:
    """The list as shown in settings, with whether each tone has audio."""
    with _lock:
        result = []
        for entry in _read():
            item = dict(entry)
            item["has_audio"] = path_for(entry).is_file()
            result.append(item)
        return result


def find(ringtone_id: str) -> dict | None:
    with _lock:
        return next((e for e in _read() if e["id"] == ringtone_id), None)


def pick() -> Path | None:
    """A random ringtone that has audio, or None when there is none."""
    with _lock:
        paths = [path_for(entry) for entry in _read()]
    paths = [path for path in paths if path.is_file()]
    return random.choice(paths) if paths else None


def add(name: str = "", phrase: str = "") -> dict:
    """A new, still silent ringtone: generate or upload its audio next."""
    with _lock:
        current = _read()
        numbers = [
            int(match[1])
            for entry in current
            if (match := re.fullmatch(r"tone-(\d+)", entry["id"]))
        ]
        entry = {
            "id": f"tone-{max(numbers, default=0) + 1}",
            "kind": "custom",
            "name": name.strip() or phrase.strip() or "New ringtone",
            "phrase": phrase.strip(),
        }
        current.append(entry)
        _write(current)
        return dict(entry)


def update(ringtone_id: str, **fields) -> dict:
    with _lock:
        current = _read()
        entry = next((e for e in current if e["id"] == ringtone_id), None)
        if entry is None:
            raise KeyError(ringtone_id)
        for key in ("name", "phrase"):
            if key in fields and fields[key] is not None:
                entry[key] = str(fields[key]).strip()
        _write(current)
        return dict(entry)


def save_audio(ringtone_id: str, pcm: bytes, *, name: str | None = None) -> dict:
    """Store 24 kHz mono 16-bit PCM as this ringtone's audio.

    A built-in tone that gets new audio becomes the user's own tone (its
    built-in file is never touched; restore brings it back).
    """
    if not pcm or len(pcm) % 2:
        raise ValueError("No valid audio for the ringtone.")
    with _lock:
        current = _read()
        entry = next((e for e in current if e["id"] == ringtone_id), None)
        if entry is None:
            raise KeyError(ringtone_id)
        if entry.get("kind") == "default":
            numbers = [
                int(match[1])
                for item in current
                if (match := re.fullmatch(r"tone-(\d+)", item["id"]))
            ]
            entry["id"] = f"tone-{max(numbers, default=0) + 1}"
            entry["kind"] = "custom"
        if name:
            entry["name"] = name.strip()
        USER_DIR.mkdir(parents=True, exist_ok=True)
        path = _user_file(entry["id"])
        with tempfile.NamedTemporaryFile(
            dir=USER_DIR, suffix=".tmp", delete=False
        ) as pending:
            temporary = pending.name
            with wave.open(pending, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(RATE)
                wf.writeframes(pcm)
        os.replace(temporary, path)
        _write(current)
        return dict(entry)


def remove(ringtone_id: str) -> None:
    with _lock:
        current = _read()
        remaining = [e for e in current if e["id"] != ringtone_id]
        if len(remaining) == len(current):
            raise KeyError(ringtone_id)
        _write(remaining)
    if not ringtone_id.startswith("default-"):
        _user_file(ringtone_id).unlink(missing_ok=True)


def restore_defaults() -> int:
    """Put every missing built-in tone back; returns how many came back."""
    with _lock:
        current = _read()
        present = {entry["id"] for entry in current}
        missing = [entry for entry in _defaults() if entry["id"] not in present]
        if missing:
            _write(missing + current)
        return len(missing)


def convert_to_pcm(data: bytes, suffix: str) -> bytes:
    """Any audio file (MP3, M4A, WAV, ...) as Billy's PCM, at most 30 s."""
    import subprocess

    from ..config import FFMPEG_BIN

    with tempfile.TemporaryDirectory() as workspace:
        source = Path(workspace) / f"source{suffix or '.wav'}"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source),
                    "-t",
                    "30",
                    "-map_metadata",
                    "-1",
                    "-f",
                    "s16le",
                    "-acodec",
                    "pcm_s16le",
                    "-ac",
                    "1",
                    "-ar",
                    str(RATE),
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"ffmpeg not found at '{FFMPEG_BIN}'; it is needed to convert audio."
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(f"Could not convert this file: {detail or exc}") from exc
    if not result.stdout:
        raise RuntimeError("This file contains no audio.")
    return result.stdout


def generate_pcm(phrase: str, persona_name: str = "default") -> bytes:
    """The current persona's voice saying (or singing) a ringtone phrase."""
    import asyncio

    from ..config import CUSTOM_INSTRUCTIONS
    from ..providers.realtime import voice_provider_registry

    try:
        from ..persona_manager import persona_manager

        voice = persona_manager.get_persona_voice(persona_name)
        persona_instructions = persona_manager.get_persona_instructions(persona_name)
    except Exception:
        voice, persona_instructions = "ballad", CUSTOM_INSTRUCTIONS
    instructions = (
        "Keep the persona's voice, accent and attitude, but do not change the "
        "words. This is a short recorded ringtone that plays while a phone call "
        "comes in: lively, rhythmic and attention-grabbing, as if you are the "
        "phone ringing. Sing it or chant it if that suits the words. It is not "
        "a conversation.\n\n" + persona_instructions
    )
    provider = voice_provider_registry.get_provider()
    pcm = asyncio.run(
        provider.generate_wakeup_clip(
            prompt=phrase, voice=voice, instructions=instructions
        )
    )
    if not pcm or len(pcm) % 2:
        raise RuntimeError("No valid ringtone audio received. Please try again.")
    return pcm
