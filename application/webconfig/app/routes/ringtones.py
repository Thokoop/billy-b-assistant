"""Ringtones for incoming Bluetooth calls (Bluetooth settings).

Works like the wake-up sounds: add a ringtone, generate it in the current
persona's voice or upload your own file, play, remove. Removed built-in tones
can always be restored. The list itself lives in core.bluetooth.ringtones.
"""

import json
import os
import subprocess

from flask import Blueprint, jsonify, request

from ..core_imports import core_config


bp = Blueprint("ringtones", __name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
FALLBACK_IDEAS = [
    "Ring ring! Ring ring!",
    "Brrring! Pick up, pick up!",
    "Phone call! Somebody wants you!",
    "Ding-a-ling-a-ling!",
    "Tring tring, tring tring!",
    "Hello? Is anybody going to answer that?",
    "Incoming call! Incoming call!",
    "Ring-a-ding-ding!",
]


def _ringtones():
    from core.bluetooth import ringtones

    return ringtones


def _persona(data=None) -> str:
    name = str((data or {}).get("persona") or "").strip()
    if name:
        return name
    try:
        from core.persona_manager import persona_manager

        return persona_manager.current_persona or "default"
    except Exception:
        return "default"


def _error(message, status=400):
    return jsonify({"error": message}), status


def _telephony_available() -> bool:
    """Whether PipeWire's Telephony service runs (PipeWire 1.4+).

    Without it Billy cannot tell a ringing call from an answered one: only
    the phone's own ringtone plays, and calls are answered on the phone.
    """
    try:
        from core.bluetooth.controls import call_states

        return call_states(timeout=1.0) is not None
    except Exception:
        return False


@bp.get("/ringtones")
def list_ringtones():
    return jsonify({
        "ringtones": _ringtones().entries(),
        "telephony": _telephony_available(),
    })


@bp.post("/ringtones/add")
def add_ringtone():
    data = request.get_json(silent=True) or {}
    entry = _ringtones().add(
        name=str(data.get("name") or ""), phrase=str(data.get("phrase") or "")
    )
    return jsonify({"status": "ok", "ringtone": entry})


@bp.post("/ringtones/<ringtone_id>/update")
def update_ringtone(ringtone_id):
    data = request.get_json(silent=True) or {}
    try:
        entry = _ringtones().update(
            ringtone_id, name=data.get("name"), phrase=data.get("phrase")
        )
    except KeyError:
        return _error("Ringtone not found", 404)
    return jsonify({"status": "ok", "ringtone": entry})


@bp.post("/ringtones/<ringtone_id>/generate")
def generate_ringtone(ringtone_id):
    data = request.get_json(silent=True) or {}
    phrase = str(data.get("phrase") or "").strip()
    if not phrase:
        return _error("Enter what the ringtone should say first")
    ringtones = _ringtones()
    if ringtones.find(ringtone_id) is None:
        return _error("Ringtone not found", 404)
    try:
        pcm = ringtones.generate_pcm(phrase, _persona(data))
        entry = ringtones.save_audio(ringtone_id, pcm, name=phrase)
        ringtones.update(entry["id"], phrase=phrase)
    except KeyError:
        return _error("Ringtone not found", 404)
    except Exception as exc:
        return _error(str(exc), 500)
    return jsonify({"status": "ok", "ringtone": entry})


@bp.post("/ringtones/<ringtone_id>/upload")
def upload_ringtone(ringtone_id):
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return _error("Choose an audio file to upload")
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _error("This file is too large (at most 20 MB)")
    ringtones = _ringtones()
    name, suffix = os.path.splitext(os.path.basename(upload.filename))
    try:
        pcm = ringtones.convert_to_pcm(data, suffix.lower())
        entry = ringtones.save_audio(ringtone_id, pcm, name=name or "Uploaded ringtone")
        ringtones.update(entry["id"], phrase="")
    except KeyError:
        return _error("Ringtone not found", 404)
    except Exception as exc:
        return _error(str(exc), 500)
    return jsonify({"status": "ok", "ringtone": entry})


@bp.post("/ringtones/<ringtone_id>/remove")
def remove_ringtone(ringtone_id):
    try:
        _ringtones().remove(ringtone_id)
    except KeyError:
        return _error("Ringtone not found", 404)
    return jsonify({"status": "ok"})


@bp.post("/ringtones/restore")
def restore_ringtones():
    restored = _ringtones().restore_defaults()
    return jsonify({"status": "ok", "restored": restored})


@bp.post("/ringtones/<ringtone_id>/play")
def play_ringtone(ringtone_id):
    ringtones = _ringtones()
    entry = ringtones.find(ringtone_id)
    if entry is None:
        return _error("Ringtone not found", 404)
    path = ringtones.path_for(entry)
    if not path.is_file():
        return _error("This ringtone has no audio yet: generate or upload it first")

    # Billy's own process owns the speaker (and the mouth): ask it to play the
    # tone over MQTT, the same way the wake-up sound preview does.
    host = (core_config.MQTT_HOST or "").strip()
    user = (core_config.MQTT_USERNAME or "").strip()
    password = (core_config.MQTT_PASSWORD or "").strip()
    if host and user and password:
        try:
            import paho.mqtt.client as mqtt

            client = mqtt.Client()
            client.username_pw_set(user, password)
            client.connect(host, int(core_config.MQTT_PORT or 1883), 5)
            result = client.publish(
                core_config.mqtt_wire_topic("billy/ringtone/play"),
                json.dumps({"id": entry["id"]}),
                retain=False,
            )
            client.disconnect()
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                return jsonify({"status": f"Playing {entry['name']}"})
        except Exception:
            pass

    from .audio import alsa_play_device, get_usb_pcm_card_index

    device = alsa_play_device(get_usb_pcm_card_index())
    subprocess.Popen(["aplay", "-q", "-D", device, str(path)])
    return jsonify({"status": f"Playing {entry['name']}"})


@bp.post("/ringtones/ideas")
def ringtone_ideas():
    data = request.get_json(silent=True) or {}
    count = max(1, min(8, int(data.get("count") or 5)))
    existing = {
        str(entry.get("phrase") or entry.get("name") or "").strip().lower()
        for entry in _ringtones().entries()
    }
    fallback = [idea for idea in FALLBACK_IDEAS if idea.lower() not in existing][:count]
    if not core_config.OPENAI_API_KEY:
        return jsonify({"ideas": fallback, "source": "fallback"})

    persona_name = _persona(data)
    try:
        from core.persona_manager import persona_manager

        instructions = persona_manager.get_persona_instructions(persona_name) or ""
    except Exception:
        instructions = ""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=core_config.OPENAI_API_KEY)
        prompt = (
            "Write short ringtone lines for a talking animatronic assistant persona. "
            "The persona's voice says or sings the line over and over while a phone "
            "call comes in, as if it is the ringing phone. Lively, rhythmic and fun; "
            "2 to 8 words each; sound effects like 'brrring' are welcome. "
            "Vary the shapes; do not repeat the same line with small changes. "
            'Return strict JSON only: {"ideas": ["...", "..."]}.\n\n'
            f"Persona instructions: {instructions[:1200]}\n"
            f"Existing ringtones to avoid: {', '.join(sorted(existing)) or 'none'}\n"
            f"Generate {count} ideas."
        )
        response = client.chat.completions.create(
            model=os.getenv("WAKEUP_IDEAS_MODEL", "gpt-4o-mini"),
            messages=[
                {
                    "role": "system",
                    "content": "You write concise voice UI copy and return strict JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content or "{}")
        ideas = [
            str(idea).strip()
            for idea in parsed.get("ideas", [])
            if str(idea).strip() and str(idea).strip().lower() not in existing
        ][:count]
        return jsonify({
            "ideas": ideas or fallback,
            "source": "openai" if ideas else "fallback",
        })
    except Exception as exc:
        return jsonify({"ideas": fallback, "source": "fallback", "warning": str(exc)})
