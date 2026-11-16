"""Custom GPT-Live voices (OpenAI's custom voices, made in the voice Playground
or through /v1/audio/voices).

Stored in .env as OPENAI_LIVE_CUSTOM_VOICES, one entry per voice:

    Name|voice_id|accent|description;Other name|voice_id2|

The accent and description are optional (older values have no description).
A persona uses a voice by its ID. GPT-Live gets it as {"id": "<voice_id>"}
in session.audio.output.voice (named voices stay plain strings); the voice's
description and accent are added to the instructions, as OpenAI's custom
voice guide asks. Prompt-generated voices only work with GPT-Live, so on
other providers a persona's fallback voice is used (persona_manager.py).

create() makes a new voice from a description (POST /v1/audio/voices with
type "prompt"); recording-based voices need OpenAI's approval and a consent
recording, so those are made on the OpenAI platform and pasted in by ID.
"""

import re


ENV_KEY = "OPENAI_LIVE_CUSTOM_VOICES"
MAX_VOICES = 100  # OpenAI: up to 100 prompt-generated voices per organization
MAX_DESCRIPTION = 1000
MAX_SCRIPT = 2000  # OpenAI's limit for script_hint
API_URL = "https://api.openai.com/v1/audio/voices"

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,79}")
_TEXT = re.compile(r"[^|;\n\r]{1,40}")
_DESCRIPTION = re.compile(rf"[^|;\n\r]{{1,{MAX_DESCRIPTION}}}")


class VoiceError(Exception):
    """A voice could not be created; the message is safe to show."""


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def parse(raw) -> list[dict]:
    """The valid voices in an OPENAI_LIVE_CUSTOM_VOICES value, in order."""
    from .providers.live_provider import LIVE_VOICES

    voices, seen = [], set()
    for entry in str(raw or "").split(";"):
        parts = [_clean(part) for part in entry.split("|")]
        if len(parts) < 2:
            continue
        name, voice_id = parts[0], parts[1]
        accent = parts[2] if len(parts) > 2 else ""
        description = parts[3] if len(parts) > 3 else ""
        if (
            not _TEXT.fullmatch(name)
            or not _ID.fullmatch(voice_id)
            or voice_id.lower() in LIVE_VOICES
            or voice_id in seen
        ):
            continue
        if accent and not _TEXT.fullmatch(accent):
            accent = ""
        if description and not _DESCRIPTION.fullmatch(description):
            description = ""
        seen.add(voice_id)
        voices.append({
            "name": name,
            "id": voice_id,
            "accent": accent,
            "description": description,
        })
        if len(voices) == MAX_VOICES:
            break
    return voices


def serialize(voices) -> str:
    def entry(v):
        text = f"{v['name']}|{v['id']}|{v.get('accent', '')}"
        return f"{text}|{v['description']}" if v.get("description") else text

    return ";".join(entry(v) for v in voices)


def normalize(raw) -> str:
    """The value as it is saved: invalid entries dropped."""
    return serialize(parse(raw))


def current() -> list[dict]:
    """The custom voices of the running configuration."""
    import importlib

    # The loaded configuration module (not a cached package attribute).
    config = importlib.import_module(f"{__package__}.config")
    return parse(getattr(config, ENV_KEY, ""))


def find(voice, voices=None) -> dict | None:
    voice = str(voice or "").strip()
    return next(
        (v for v in (current() if voices is None else voices) if v["id"] == voice), None
    )


def _storable(text) -> str:
    """Text as it can be stored in the .env value (no separators)."""
    return _clean(re.sub(r"[|;]", " ", str(text or "")))


def _error_message(response, data) -> str:
    error = data.get("error") if isinstance(data, dict) else None
    detail = error.get("message") if isinstance(error, dict) else ""
    detail = _clean(detail)[:300]
    status = getattr(response, "status_code", 0)
    if status in (401, 403):
        hint = "OpenAI refused the API key"
    elif status == 404:
        hint = "This API key's project has no access to custom voices"
    elif status == 429:
        hint = "OpenAI's limit was reached (at most 100 voices per organization)"
    else:
        hint = f"OpenAI could not create the voice (HTTP {status})"
    return f"{hint}: {detail}" if detail else hint


def create(api_key, name, description, script="", *, post=None, timeout=60) -> dict:
    """Create a GPT-Live voice from a description through OpenAI's API.

    Returns the voice as stored ({"name", "id", "accent", "description"});
    raises VoiceError with a message for the user. Only the description goes
    to OpenAI; nothing is saved here.
    """
    name = _storable(name)
    description = _storable(description)
    script = str(script or "").strip()
    if not api_key:
        raise VoiceError("Save an OpenAI API key first")
    if not _TEXT.fullmatch(name):
        raise VoiceError("Give the voice a name of at most 40 characters")
    if not _DESCRIPTION.fullmatch(description) or len(description) < 10:
        raise VoiceError(f"Describe the voice in 10 to {MAX_DESCRIPTION} characters")
    if len(script) > MAX_SCRIPT:
        raise VoiceError(f"The example script can have at most {MAX_SCRIPT} characters")

    body = {"type": "prompt", "name": name, "prompt": description, "model": "auto"}
    if script:
        body["script_hint"] = script
    if post is None:
        import requests

        post = requests.post
    try:
        response = post(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
            timeout=timeout,
        )
    except Exception as exc:
        raise VoiceError("Could not reach OpenAI to create the voice") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not 200 <= getattr(response, "status_code", 0) < 300:
        raise VoiceError(_error_message(response, data))
    voice_id = str((data or {}).get("id") or "").strip()
    if not _ID.fullmatch(voice_id):
        raise VoiceError("OpenAI did not return a voice ID")
    return {"name": name, "id": voice_id, "accent": "", "description": description}
