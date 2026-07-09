import configparser
import contextlib
import glob
import json
import os
import queue
import re
import subprocess
import threading
import time
import wave
from collections import deque

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
from dotenv import find_dotenv, set_key
from flask import Blueprint, Response, jsonify, request, send_from_directory

from core.audio import calculate_input_rms
from core.wakeup import generate_wake_clip_async

from ..core_imports import core_config
from ..state import PERSONA_PATH, PROJECT_ROOT, WAKE_UP_DIR


bp = Blueprint("audio", __name__)

mic_check_running = False
rms_queue = queue.Queue()
mic_check_record_queue: queue.Queue[bytes] | None = None
mic_check_started_at = 0.0
mic_check_record_gate = True
mic_check_record_threshold = float(core_config.SILENCE_THRESHOLD)
MIC_CHECK_GATE_PREROLL_SECONDS = 0.25
MIC_CHECK_GATE_RELEASE_SECONDS = 0.8
mic_check_record_gate_open = False
mic_check_record_preroll: deque[bytes] = deque()
mic_check_record_preroll_max_chunks = 8
mic_check_record_release_until = 0.0
MIC_TEST_RECORDING_PATH = os.path.join(PROJECT_ROOT, "sounds", "mic-test-recording.wav")
MIC_TEST_RECORDING_MAX_SECONDS = 30
mic_recording_lock = threading.Lock()
mic_recording_stop_event: threading.Event | None = None
mic_recording_thread: threading.Thread | None = None
mic_recording_started_at = 0.0
mic_recording_error: str | None = None


def _env_path() -> str:
    found = find_dotenv(usecwd=True)
    if found:
        return found
    return os.path.join(PROJECT_ROOT, ".env")


def _is_usb_audio_device_name(name: str) -> bool:
    """Heuristic: keep only real USB audio endpoints, skip virtual ALSA PCMs."""
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    # Typical virtual/non-hardware ALSA/PipeWire names.
    virtual_tokens = (
        "sysdefault",
        "default",
        "dmix",
        "dsnoop",
        "surround",
        "iec958",
        "pulse",
        "pipewire",
        "jack",
    )
    if any(token in lowered for token in virtual_tokens):
        return False
    return "usb" in lowered


def _normalize_audio_preference(value: str) -> str:
    normalized = (value or "").strip().lower()
    # Hardware indices can change across reboots; ignore them for matching.
    normalized = re.sub(r"\s*\(hw:\d+,\d+\)\s*", " ", normalized)
    # Optional UI duplicate suffixes like "(#1)" should not affect matching.
    normalized = re.sub(r"\s*\(#\d+\)\s*", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _parse_hw_card_index(name: str) -> int | None:
    match = re.search(r"\(hw\s*:\s*(\d+)\s*,\s*\d+\)", name or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _usb_path_for_card(card_index: int | None) -> str | None:
    if card_index is None:
        return None
    try:
        sysfs = f"/sys/class/sound/card{card_index}/device"
        real = os.path.realpath(sysfs)
        base = os.path.basename(real).split(":", 1)[0]
        if re.match(r"^\d+-\d+(?:\.\d+)*$", base):
            return base
        return None
    except Exception:
        return None


def _preference_from_device_name(device_name: str) -> str:
    card_index = _parse_hw_card_index(device_name)
    usb_path = _usb_path_for_card(card_index)
    if usb_path:
        return f"usbpath:{usb_path}"
    return str(device_name or "").strip()


def _preference_matches_device_name(preference: str, device_name: str) -> bool:
    preference = (preference or "").strip()
    if not preference:
        return True
    if preference.lower().startswith("usbpath:"):
        wanted = preference.split(":", 1)[1].strip().lower()
        candidate = _usb_path_for_card(_parse_hw_card_index(device_name))
        return bool(wanted and candidate and candidate.lower() == wanted)
    pref_norm = _normalize_audio_preference(preference)
    name_norm = _normalize_audio_preference(device_name)
    return pref_norm in name_norm or name_norm in pref_norm


def _resolve_selected_preference(
    saved_preference: str, devices: list[dict[str, str | int]]
) -> str:
    if not saved_preference:
        return ""

    for dev in devices:
        value = str(dev.get("value", ""))
        alias = str(dev.get("legacy_value", ""))
        if saved_preference in (value, alias):
            return value

    saved_norm = _normalize_audio_preference(saved_preference)
    for dev in devices:
        value = str(dev.get("value", ""))
        alias = str(dev.get("legacy_value", ""))
        if (
            _normalize_audio_preference(value) == saved_norm
            or _normalize_audio_preference(alias) == saved_norm
        ):
            return value

    return saved_preference


def get_usb_pcm_card_index(preference_override: str | None = None):
    raw_preference = (
        preference_override
        if preference_override is not None
        else core_config.SPEAKER_PREFERENCE
    )
    preference = (raw_preference or "").strip()
    normalized_preference = _normalize_audio_preference(preference)
    try:
        out = subprocess.check_output(["aplay", "-l"], text=True)
        cards = re.findall(
            r"card (\d+): ([^\s]+) \[(.*?)\], device (\d+): (.*?) \[", out
        )

        if preference.lower().startswith("usbpath:"):
            wanted_path = preference.split(":", 1)[1].strip().lower()
            for card_index, *_ in cards:
                if (_usb_path_for_card(int(card_index)) or "").lower() == wanted_path:
                    return int(card_index)

        # If SPEAKER_PREFERENCE includes "(hw:X,Y)", trust that card index first.
        if preference:
            hw_match = re.search(r"hw\s*:\s*(\d+)\s*,\s*\d+", preference.lower())
            if hw_match:
                card_from_hw = int(hw_match.group(1))
                if any(int(card_index) == card_from_hw for card_index, *_ in cards):
                    return card_from_hw

        if not preference:
            return None

        for card_index, shortname, longname, device_index, desc in cards:
            name = f"{shortname} {longname} {desc}"
            normalized_name = _normalize_audio_preference(name)
            if normalized_preference and (
                normalized_preference in normalized_name
                or normalized_name in normalized_preference
            ):
                return int(card_index)

        # Last fallback: first USB audio card from aplay list.
        for card_index, shortname, longname, _, desc in cards:
            combined = f"{shortname} {longname} {desc}".lower()
            if "usb" in combined:
                return int(card_index)

        return None
    except Exception as e:
        print("Failed to detect speaker card:", e)
        return None


def get_usb_capture_card_index():
    preference = (core_config.MIC_PREFERENCE or "").strip()
    normalized_pref = _normalize_audio_preference(preference)
    try:
        output = subprocess.check_output(["arecord", "-l"], text=True)
        cards = re.findall(
            r"card (\d+): ([^\s]+) \[(.*?)\], device (\d+): (.*?) \[", output
        )
        if preference.lower().startswith("usbpath:"):
            wanted_path = preference.split(":", 1)[1].strip().lower()
            for card_index, *_ in cards:
                if (_usb_path_for_card(int(card_index)) or "").lower() == wanted_path:
                    return int(card_index)
        for card_index, shortname, longname, device_index, desc in cards:
            name_combined = f"{shortname} {longname} {desc}"
            normalized_name = _normalize_audio_preference(name_combined)
            if normalized_pref and (
                normalized_pref in normalized_name or normalized_name in normalized_pref
            ):
                return int(card_index)
        for card_index, _, longname, _, _ in cards:
            if "usb" in longname.lower():
                return int(card_index)
        return None
    except Exception as e:
        print("Failed to detect mic card:", e)
        return None


def amixer_base_args_for_card(card_index):
    return ["-D", "default"] if card_index is None else ["-c", str(card_index)]


def alsa_play_device(card_index):
    return "default" if card_index is None else f"plughw:{card_index},0"


def _wav_duration_seconds(path: str) -> float:
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        if rate <= 0:
            return 0.0
        return float(wav.getnframes()) / float(rate)


def _play_wav_on_speaker(path: str, speaker_preference: str | None = None):
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    duration = _wav_duration_seconds(path)
    if duration <= 0:
        raise RuntimeError("Audio file is empty or invalid")

    card_index = get_usb_pcm_card_index(speaker_preference)
    device = alsa_play_device(card_index)
    process = subprocess.Popen(
        ["aplay", "-q", "-D", device, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.1)
    if process.poll() is not None and process.returncode != 0:
        _, stderr = process.communicate(timeout=1)
        raise RuntimeError(
            (stderr or f"aplay exited with {process.returncode}").strip()
        )

    return device, duration


def _pick_input_device_index() -> int | None:
    preference = (core_config.MIC_PREFERENCE or "").strip()
    devices = sd.query_devices()
    first_input_index = None
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0) or 0) <= 0:
            continue
        if first_input_index is None:
            first_input_index = idx
        if preference and _preference_matches_device_name(
            preference, str(dev.get("name", ""))
        ):
            return idx
    return first_input_index


def _pick_supported_input_format(device_index) -> tuple[int, int]:
    device_info = sd.query_devices(device_index, "input")
    max_channels = max(1, int(device_info.get("max_input_channels") or 1))
    default_rate = int(device_info.get("default_samplerate") or 24000)
    channel_candidates = []
    for channels in (1, 2, max_channels):
        if 1 <= channels <= max_channels and channels not in channel_candidates:
            channel_candidates.append(channels)
    rate_candidates = []
    for rate in (24000, 48000, 44100, default_rate):
        if rate not in rate_candidates:
            rate_candidates.append(rate)
    for channels in channel_candidates:
        for rate in rate_candidates:
            try:
                sd.check_input_settings(
                    device=device_index,
                    samplerate=rate,
                    channels=channels,
                )
                return int(channels), int(rate)
            except Exception:
                continue
    return 1, default_rate


def _ensure_core_audio_input_config():
    from core import audio as core_audio

    device_index = core_audio.MIC_DEVICE_INDEX
    if device_index is None:
        device_index = _pick_input_device_index()
    channels = int(core_audio.MIC_CHANNELS or 1)
    rate = int(core_audio.MIC_RATE or 0)
    if rate:
        try:
            sd.check_input_settings(
                device=device_index,
                samplerate=rate,
                channels=channels,
            )
        except Exception:
            channels, rate = _pick_supported_input_format(device_index)
    else:
        channels, rate = _pick_supported_input_format(device_index)

    core_audio.MIC_DEVICE_INDEX = device_index
    core_audio.MIC_CHANNELS = channels
    core_audio.MIC_RATE = rate
    core_audio.CHUNK_SIZE = int(rate * int(core_config.CHUNK_MS or 40) / 1000)
    return core_audio


@contextlib.contextmanager
def _input_stream_with_retry(**kwargs):
    stream = None
    last_error = None
    for attempt in range(6):
        try:
            stream = sd.InputStream(**kwargs)
            stream.__enter__()
            break
        except Exception as e:
            last_error = e
            stream = None
            if "Device unavailable" not in str(e) or attempt == 5:
                raise
            time.sleep(0.5)
    if stream is None:
        raise RuntimeError(str(last_error or "Failed to open input stream"))
    try:
        yield stream
    finally:
        stream.__exit__(None, None, None)


def get_mic_gain_numid(card_index):
    """Find the numid for mic gain on the specified card."""
    try:
        output = subprocess.check_output(
            ["amixer", "-c", str(card_index), "controls"], text=True
        )
        for line in output.splitlines():
            if "Mic Capture Volume" in line:
                match = re.search(r"numid=(\d+)", line)
                if match:
                    return int(match.group(1))
    except Exception as e:
        print("Failed to get mic gain numid:", e)
        return None


def parse_amixer_control_range(output: str) -> tuple[int, int]:
    min_match = re.search(r"\bmin=(-?\d+)", output or "")
    max_match = re.search(r"\bmax=(-?\d+)", output or "")
    min_value = int(min_match.group(1)) if min_match else 0
    max_value = int(max_match.group(1)) if max_match else 16
    if max_value < min_value:
        return 0, 16
    return min_value, max_value


def parse_amixer_control_value(output: str) -> int | None:
    match = re.search(r": values=(-?\d+)", output or "")
    return int(match.group(1)) if match else None


def resolve_configured_mic_gain(
    raw_value: str | int | float | None,
    min_value: int,
    max_value: int,
) -> int:
    value = str(raw_value if raw_value is not None else "max").strip().lower()
    if value in {"", "max", "maximum", "default"}:
        return max_value
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return max_value
    return max(min_value, min(max_value, parsed))


def save_configured_mic_gain(value: int) -> None:
    env_path = _env_path()
    if not os.path.exists(env_path):
        open(env_path, "a", encoding="utf-8").close()
    set_key(env_path, "MIC_GAIN", str(value), quote_mode="never")
    os.environ["MIC_GAIN"] = str(value)
    core_config.MIC_GAIN = str(value)


def apply_configured_mic_gain() -> dict:
    """Apply configured capture gain to ALSA. Defaults to the device maximum."""

    try:
        card_index = get_usb_capture_card_index()
        numid = get_mic_gain_numid(card_index)
        if card_index is None or numid is None:
            return {"status": "skipped", "reason": "mic_gain_control_unavailable"}

        output = subprocess.check_output(
            ["amixer", "-c", str(card_index), "cget", f"numid={numid}"],
            text=True,
        )
        current_value = parse_amixer_control_value(output)
        if current_value is None:
            return {"status": "skipped", "reason": "mic_gain_value_unavailable"}

        min_value, max_value = parse_amixer_control_range(output)
        configured_value = getattr(core_config, "MIC_GAIN", "max")
        target_value = resolve_configured_mic_gain(
            configured_value,
            min_value,
            max_value,
        )
        if current_value != target_value:
            subprocess.check_call([
                "amixer",
                "-c",
                str(card_index),
                "cset",
                f"numid={numid}",
                str(target_value),
            ])
            status = "updated"
        else:
            status = "kept"

        return {
            "status": status,
            "old": current_value,
            "gain": target_value,
            "min": min_value,
            "max": max_value,
            "configured": str(configured_value),
        }
    except Exception as e:
        return {"status": "skipped", "reason": str(e)}


@bp.route("/audio/devices")
def audio_devices():
    try:
        devices = sd.query_devices()
        mic_preference = (core_config.MIC_PREFERENCE or "").strip()
        speaker_preference = (core_config.SPEAKER_PREFERENCE or "").strip()

        input_devices = []
        output_devices = []
        seen_inputs: set[str] = set()
        seen_outputs: set[str] = set()
        for idx, dev in enumerate(devices):
            name = str(dev.get("name", "")).strip()
            if not name:
                continue
            if not _is_usb_audio_device_name(name):
                continue
            preference_value = _preference_from_device_name(name)
            card_index = _parse_hw_card_index(name)
            usb_path = _usb_path_for_card(card_index)
            label_suffix = f" (usb:{usb_path})" if usb_path else f" (#{idx})"
            if int(dev.get("max_input_channels", 0) or 0) > 0:
                key = preference_value.lower()
                if key in seen_inputs:
                    continue
                seen_inputs.add(key)
                input_devices.append({
                    "value": preference_value,
                    "legacy_value": name,
                    "label": f"{name}{label_suffix}",
                    "index": idx,
                })
            if int(dev.get("max_output_channels", 0) or 0) > 0:
                key = preference_value.lower()
                if key in seen_outputs:
                    continue
                seen_outputs.add(key)
                output_devices.append({
                    "value": preference_value,
                    "legacy_value": name,
                    "label": f"{name}{label_suffix}",
                    "index": idx,
                })

        selected_mic = _resolve_selected_preference(mic_preference, input_devices)
        selected_speaker = _resolve_selected_preference(
            speaker_preference, output_devices
        )

        return jsonify({
            "input_devices": input_devices,
            "output_devices": output_devices,
            "selected_mic": selected_mic,
            "selected_speaker": selected_speaker,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def audio_callback(indata, frames, time_info, status):
    global mic_check_record_gate_open
    global mic_check_record_preroll
    global mic_check_running
    global mic_check_record_release_until
    if not mic_check_running:
        raise sd.CallbackStop()
    frame_bytes = indata.copy().tobytes()
    rms = calculate_input_rms(indata)
    rms_queue.put(rms)
    if mic_check_record_queue is not None:
        now = time.time()
        above_threshold = rms >= mic_check_record_threshold
        if above_threshold:
            mic_check_record_release_until = now + MIC_CHECK_GATE_RELEASE_SECONDS
        gate_open = (
            not mic_check_record_gate
            or above_threshold
            or now <= mic_check_record_release_until
        )
        if gate_open:
            if not mic_check_record_gate_open:
                while mic_check_record_preroll:
                    mic_check_record_queue.put(mic_check_record_preroll.popleft())
            mic_check_record_queue.put(frame_bytes)
            mic_check_record_gate_open = True
        else:
            mic_check_record_gate_open = False
            mic_check_record_preroll.append(frame_bytes)
            while len(mic_check_record_preroll) > mic_check_record_preroll_max_chunks:
                mic_check_record_preroll.popleft()
    if time.time() - mic_check_started_at >= MIC_TEST_RECORDING_MAX_SECONDS:
        mic_check_running = False
        raise sd.CallbackStop()


def _record_mic_test(stop_event: threading.Event):
    global mic_recording_error
    try:
        core_audio = _ensure_core_audio_input_config()

        os.makedirs(os.path.dirname(MIC_TEST_RECORDING_PATH), exist_ok=True)
        with wave.open(MIC_TEST_RECORDING_PATH, "wb") as wav:
            wav.setnchannels(int(core_audio.MIC_CHANNELS))
            wav.setsampwidth(2)
            wav.setframerate(int(core_audio.MIC_RATE))

            started_at = time.time()
            frame_queue = queue.Queue()

            def callback(indata, frames, time_info, status):
                if stop_event.is_set():
                    raise sd.CallbackStop()
                frame_queue.put(indata.copy().tobytes())
                if time.time() - started_at >= MIC_TEST_RECORDING_MAX_SECONDS:
                    stop_event.set()
                    raise sd.CallbackStop()

            with _input_stream_with_retry(
                samplerate=core_audio.MIC_RATE,
                device=core_audio.MIC_DEVICE_INDEX,
                channels=core_audio.MIC_CHANNELS,
                dtype="int16",
                blocksize=core_audio.CHUNK_SIZE,
                callback=callback,
            ):
                while (
                    not stop_event.is_set()
                    and time.time() - started_at < MIC_TEST_RECORDING_MAX_SECONDS
                ):
                    with contextlib.suppress(queue.Empty):
                        wav.writeframes(frame_queue.get(timeout=0.1))
                while not frame_queue.empty():
                    wav.writeframes(frame_queue.get_nowait())
    except Exception as e:
        mic_recording_error = str(e)


def _mic_recording_active() -> bool:
    return bool(mic_recording_thread and mic_recording_thread.is_alive())


def _mic_recording_duration_seconds() -> float:
    try:
        return _wav_duration_seconds(MIC_TEST_RECORDING_PATH)
    except Exception:
        return 0.0


@bp.route("/mic-record/start", methods=["POST"])
def mic_record_start():
    global mic_recording_error
    global mic_recording_started_at
    global mic_recording_stop_event
    global mic_recording_thread

    with mic_recording_lock:
        if _mic_recording_active():
            return jsonify({"status": "already_recording"}), 409

        mic_recording_error = None
        mic_recording_started_at = time.time()
        try:
            if os.path.exists(MIC_TEST_RECORDING_PATH):
                os.remove(MIC_TEST_RECORDING_PATH)
        except Exception:
            pass
        mic_recording_stop_event = threading.Event()
        mic_recording_thread = threading.Thread(
            target=_record_mic_test,
            args=(mic_recording_stop_event,),
            daemon=True,
        )
        mic_recording_thread.start()

    return jsonify({
        "status": "recording",
        "max_seconds": MIC_TEST_RECORDING_MAX_SECONDS,
    })


@bp.route("/mic-record/stop", methods=["POST"])
def mic_record_stop():
    global mic_recording_stop_event

    with mic_recording_lock:
        if mic_recording_stop_event:
            mic_recording_stop_event.set()
        thread = mic_recording_thread

    if thread and thread.is_alive():
        thread.join(timeout=2.0)

    return jsonify(_mic_record_status_payload())


def _mic_record_status_payload():
    standalone_active = _mic_recording_active()
    active = standalone_active or mic_check_running
    if standalone_active:
        elapsed = time.time() - mic_recording_started_at
    elif mic_check_running:
        elapsed = time.time() - mic_check_started_at
    else:
        elapsed = 0.0
    exists = os.path.exists(MIC_TEST_RECORDING_PATH)
    size = os.path.getsize(MIC_TEST_RECORDING_PATH) if exists else 0
    return {
        "recording": active,
        "elapsed": round(elapsed, 1),
        "max_seconds": MIC_TEST_RECORDING_MAX_SECONDS,
        "exists": exists and size > 44,
        "bytes": size,
        "duration": round(_mic_recording_duration_seconds(), 1),
        "error": mic_recording_error,
    }


@bp.route("/mic-record/status")
def mic_record_status():
    return jsonify(_mic_record_status_payload())


@bp.route("/mic-record/play", methods=["POST"])
def mic_record_play():
    if _mic_recording_active() or mic_check_running:
        return jsonify({"error": "Recording is still in progress"}), 409
    if not os.path.exists(MIC_TEST_RECORDING_PATH):
        return jsonify({"error": "No mic test recording found"}), 404

    try:
        data = request.get_json(silent=True) or {}
        speaker_preference = str(data.get("speaker_preference") or "").strip() or None
        device, duration = _play_wav_on_speaker(
            MIC_TEST_RECORDING_PATH,
            speaker_preference,
        )
        return jsonify({"status": f"playing on {device}", "duration": duration})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/wakeup", methods=["GET"])
def list_wakeup_clips():
    # Get current persona to check for persona-specific clips
    current_persona = "default"
    try:
        from core.persona_manager import persona_manager

        current_persona = persona_manager.current_persona
    except Exception:
        pass

    # Load wake-up data from the current persona's configuration
    config = configparser.ConfigParser()
    if current_persona == "default":
        # For default persona, use the main persona file
        config.read(PERSONA_PATH)
    else:
        # For other personas, use their specific persona file
        persona_file = os.path.join("personas", current_persona, "persona.ini")
        if os.path.exists(persona_file):
            config.read(persona_file)
        else:
            # Fallback to main persona file if persona file doesn't exist
            config.read(PERSONA_PATH)

    wakeup_data = dict(config["WAKEUP"]) if "WAKEUP" in config else {}

    # Check for appropriate wake-up files based on persona
    files = []
    if current_persona and current_persona != "default":
        # For non-default personas, check persona-specific directory
        persona_wakeup_dir = os.path.join("personas", current_persona, "wakeup")
        if os.path.exists(persona_wakeup_dir):
            files = glob.glob(os.path.join(persona_wakeup_dir, "*.wav"))
    elif current_persona == "default":
        # For default persona, use the custom directory
        files = glob.glob(str(WAKE_UP_DIR / "*.wav"))

    # If no files found yet, check custom directory as fallback
    if not files:
        files = glob.glob(str(WAKE_UP_DIR / "*.wav"))

    available = {os.path.splitext(os.path.basename(f))[0] for f in files}
    clips = []
    for k in sorted(wakeup_data.keys(), key=lambda x: int(x)):
        phrase = wakeup_data[k]
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", phrase).strip("_").lower()
        has_audio = slug in available or k in available
        clips.append({"index": int(k), "phrase": phrase, "has_audio": has_audio})
    return jsonify({"clips": clips})


@bp.route("/wakeup/play", methods=["POST"])
def play_wakeup_clip():
    try:
        index = int(request.json.get("index"))
        persona_name = request.json.get("persona")  # Get persona from request

        if index < 1 or index > 99:
            return jsonify({"error": "Invalid clip index"}), 400

        # If no persona specified, get current persona from persona manager
        if not persona_name:
            try:
                from core.persona_manager import persona_manager

                persona_name = persona_manager.current_persona
            except Exception:
                persona_name = "default"

        # Check appropriate directory based on persona
        sound_path = None
        if persona_name and persona_name != "default":
            # For non-default personas, check persona-specific directory
            persona_sound_path = os.path.join(
                PROJECT_ROOT, "personas", persona_name, "wakeup", f"{index}.wav"
            )
            if os.path.exists(persona_sound_path):
                sound_path = persona_sound_path
        elif persona_name == "default":
            # For default persona, use custom directory
            sound_path = os.path.join(
                PROJECT_ROOT, "sounds", "wake-up", "custom", f"{index}.wav"
            )

        # If no sound path found yet, check custom directory as fallback
        if not sound_path:
            sound_path = os.path.join(
                PROJECT_ROOT, "sounds", "wake-up", "custom", f"{index}.wav"
            )

        if not os.path.exists(sound_path):
            return jsonify({"error": f"Clip {index}.wav not found"}), 404

        # Prefer sending preview request to the running Billy service process.
        # That process owns GPIO, so mouth movement works reliably.
        mqtt_host = (core_config.MQTT_HOST or "").strip()
        mqtt_user = (core_config.MQTT_USERNAME or "").strip()
        mqtt_pass = (core_config.MQTT_PASSWORD or "").strip()
        mqtt_port = int(core_config.MQTT_PORT or 1883)
        if mqtt_host and mqtt_user and mqtt_pass:
            try:
                client = mqtt.Client()
                client.username_pw_set(mqtt_user, mqtt_pass)
                client.connect(mqtt_host, mqtt_port, 5)
                payload = json.dumps({
                    "index": index,
                    "persona": persona_name,
                })
                result = client.publish("billy/wakeup/play", payload, retain=False)
                client.disconnect()
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    return jsonify({
                        "status": f"Queued clip {index}.wav via MQTT for Billy service playback"
                    })
            except Exception:
                pass

        # Fallback: plain local playback without touching Billy GPIO ownership.
        card_index = get_usb_pcm_card_index()
        device = alsa_play_device(card_index)
        subprocess.Popen(["aplay", "-q", "-D", device, sound_path])
        return jsonify({"status": f"Playing clip {index}.wav on {device}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/sounds/wake-up/<filename>")
def serve_wakeup_sound(filename):
    return send_from_directory("sounds/wake-up", filename)


@bp.route("/wakeup/generate", methods=["POST"])
def generate_wakeup_clip():
    data = request.get_json()
    prompt = data.get("text", "").strip()
    index = data.get("index")
    persona_name = data.get("persona")  # Get persona from request

    if not prompt or index is None:
        return jsonify({"error": "Missing 'text' or 'index'"}), 400

    # If no persona specified, get current persona from persona manager
    if not persona_name:
        try:
            from core.persona_manager import persona_manager

            persona_name = persona_manager.current_persona
        except Exception:
            persona_name = "default"

    try:
        path = generate_wake_clip_async(prompt, index, persona_name)
        return jsonify({"status": "ok", "path": path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/wakeup/remove", methods=["POST"])
def remove_wakeup_clip():
    data = request.get_json()
    index_to_remove = str(data.get("index"))

    # Get current persona to determine which file to modify
    current_persona = "default"
    try:
        from core.persona_manager import persona_manager

        current_persona = persona_manager.current_persona
    except Exception:
        pass

    # Determine the file path based on current persona
    if current_persona == "default":
        persona_file = PERSONA_PATH
    else:
        from pathlib import Path

        personas_dir = Path("personas")
        persona_file = personas_dir / current_persona / "persona.ini"

    config = configparser.ConfigParser()
    config.read(persona_file)
    if "WAKEUP" not in config:
        return jsonify({"error": "No wakeup section found"}), 400
    wakeup = dict(config["WAKEUP"])
    if index_to_remove not in wakeup:
        return jsonify({"error": f"Clip {index_to_remove} not found"}), 404
    removed_phrase = wakeup.pop(index_to_remove)
    new_wakeup = {}
    old_to_new_index = {}
    for i, (old_k, phrase) in enumerate(wakeup.items(), start=1):
        new_wakeup[str(i)] = phrase
        old_to_new_index[old_k] = str(i)
    config["WAKEUP"] = new_wakeup
    with open(persona_file, "w") as f:
        config.write(f)
    audio_path_num = WAKE_UP_DIR / f"{index_to_remove}.wav"
    audio_path_slug = (
        WAKE_UP_DIR
        / f"{re.sub(r'[^a-zA-Z0-9_-]+', '_', removed_phrase).strip('_').lower()}.wav"
    )
    for p in (audio_path_num, audio_path_slug):
        if p.exists():
            p.unlink()
    for old_k, new_k in old_to_new_index.items():
        old_path = WAKE_UP_DIR / f"{old_k}.wav"
        new_path = WAKE_UP_DIR / f"{new_k}.wav"
        if old_path.exists() and old_path != new_path:
            old_path.rename(new_path)
    return jsonify({"status": "removed and reindexed"})


@bp.route("/speaker-test", methods=["POST"])
def speaker_test():
    try:
        data = request.get_json(silent=True) or {}
        speaker_preference = str(data.get("speaker_preference") or "").strip() or None
        sound_path = os.path.join(PROJECT_ROOT, "sounds", "speakertest.wav")
        device, duration = _play_wav_on_speaker(sound_path, speaker_preference)
        return jsonify({"status": f"playing on {device}", "duration": duration})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/mic-check/config", methods=["POST"])
def mic_check_config():
    global mic_check_record_gate_open
    global mic_check_record_gate
    global mic_check_record_preroll
    global mic_check_record_release_until
    global mic_check_record_threshold

    data = request.get_json(silent=True) or {}
    if "threshold" in data:
        try:
            mic_check_record_threshold = max(
                0.0, min(32768.0, float(data["threshold"]))
            )
        except Exception:
            return jsonify({"error": "Invalid threshold"}), 400
        mic_check_record_gate_open = False
        mic_check_record_preroll.clear()
        mic_check_record_release_until = 0.0
    mic_check_record_gate = True
    return jsonify({
        "gate_recording": mic_check_record_gate,
        "threshold": mic_check_record_threshold,
        "preroll_ms": round(MIC_CHECK_GATE_PREROLL_SECONDS * 1000),
        "release_ms": round(MIC_CHECK_GATE_RELEASE_SECONDS * 1000),
    })


@bp.route("/mic-check")
def mic_check():
    threshold_arg = request.args.get("threshold", core_config.SILENCE_THRESHOLD)

    def rms_stream_generator():
        # use module-level flag
        global mic_check_record_gate_open
        global mic_check_record_gate
        global mic_check_record_preroll
        global mic_check_record_preroll_max_chunks
        global mic_check_record_queue
        global mic_check_record_release_until
        global mic_check_record_threshold
        global mic_check_running
        global mic_check_started_at
        global mic_recording_error
        mic_check_running = False
        mic_check_started_at = time.time()
        mic_recording_error = None
        mic_check_record_queue = queue.Queue()
        mic_check_record_gate_open = False
        mic_check_record_preroll.clear()
        mic_check_record_release_until = 0.0
        try:
            mic_check_record_threshold = max(
                0.0,
                min(
                    32768.0,
                    float(threshold_arg),
                ),
            )
        except Exception:
            mic_check_record_threshold = float(core_config.SILENCE_THRESHOLD)
        mic_check_record_gate = True
        try:
            core_audio = _ensure_core_audio_input_config()
            chunk_seconds = (
                float(core_audio.CHUNK_SIZE) / float(core_audio.MIC_RATE)
                if core_audio.MIC_RATE
                else 0.04
            )
            mic_check_record_preroll_max_chunks = max(
                1,
                int(
                    np.ceil(MIC_CHECK_GATE_PREROLL_SECONDS / max(chunk_seconds, 0.001))
                ),
            )
            os.makedirs(os.path.dirname(MIC_TEST_RECORDING_PATH), exist_ok=True)
            try:
                if os.path.exists(MIC_TEST_RECORDING_PATH):
                    os.remove(MIC_TEST_RECORDING_PATH)
            except Exception:
                pass
            while not rms_queue.empty():
                rms_queue.get_nowait()

            with wave.open(MIC_TEST_RECORDING_PATH, "wb") as wav:
                wav.setnchannels(int(core_audio.MIC_CHANNELS))
                wav.setsampwidth(2)
                wav.setframerate(int(core_audio.MIC_RATE))
                mic_check_running = True
                with _input_stream_with_retry(
                    samplerate=core_audio.MIC_RATE,
                    device=core_audio.MIC_DEVICE_INDEX,
                    channels=core_audio.MIC_CHANNELS,
                    dtype="int16",
                    blocksize=core_audio.CHUNK_SIZE,
                    callback=audio_callback,
                ):
                    while mic_check_running:
                        try:
                            rms = rms_queue.get(timeout=1.0)
                            while (
                                mic_check_record_queue
                                and not mic_check_record_queue.empty()
                            ):
                                wav.writeframes(mic_check_record_queue.get_nowait())
                            elapsed = time.time() - mic_check_started_at
                            payload = {
                                "rms": round(rms, 4),
                                "threshold": round(
                                    float(mic_check_record_threshold), 4
                                ),
                                "gate_recording": mic_check_record_gate,
                                "gate_preroll_ms": round(
                                    MIC_CHECK_GATE_PREROLL_SECONDS * 1000
                                ),
                                "gate_release_ms": round(
                                    MIC_CHECK_GATE_RELEASE_SECONDS * 1000
                                ),
                                "recording": True,
                                "elapsed": round(elapsed, 1),
                                "max_seconds": MIC_TEST_RECORDING_MAX_SECONDS,
                            }
                            yield f"data: {json.dumps(payload)}\n\n"
                        except queue.Empty:
                            while (
                                mic_check_record_queue
                                and not mic_check_record_queue.empty()
                            ):
                                wav.writeframes(mic_check_record_queue.get_nowait())
                            continue
                    while mic_check_record_queue and not mic_check_record_queue.empty():
                        wav.writeframes(mic_check_record_queue.get_nowait())
        except Exception as e:
            mic_recording_error = str(e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            mic_check_running = False
            mic_check_record_queue = None

    return Response(rms_stream_generator(), mimetype="text/event-stream")


@bp.route("/mic-check/stop")
def mic_check_stop():
    global mic_check_running
    mic_check_running = False
    return jsonify({"status": "stopped"})


@bp.route("/mic-gain", methods=["GET", "POST"])
def mic_gain():
    card_index = get_usb_capture_card_index()
    numid = get_mic_gain_numid(card_index)
    if card_index is None or numid is None:
        return jsonify({"error": "Could not determine mic card or control ID"}), 500
    if request.method == "GET":
        try:
            output = subprocess.check_output(
                ["amixer", "-c", str(card_index), "cget", f"numid={numid}"], text=True
            )
            gain = parse_amixer_control_value(output)
            min_value, max_value = parse_amixer_control_range(output)
            return jsonify({
                "gain": gain,
                "min": min_value,
                "max": max_value,
                "recommended": max_value,
                "configured": str(getattr(core_config, "MIC_GAIN", "max")),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    if request.method == "POST":
        try:
            data = request.get_json()
            value = int(data.get("value", 16))
            output = subprocess.check_output(
                ["amixer", "-c", str(card_index), "cget", f"numid={numid}"], text=True
            )
            min_value, max_value = parse_amixer_control_range(output)
            if min_value <= value <= max_value:
                subprocess.check_call([
                    "amixer",
                    "-c",
                    str(card_index),
                    "cset",
                    f"numid={numid}",
                    str(value),
                ])
                save_configured_mic_gain(value)
                return jsonify({
                    "gain": value,
                    "min": min_value,
                    "max": max_value,
                    "recommended": max_value,
                    "configured": str(value),
                })
            return jsonify({
                "error": f"Mic gain must be between {min_value} and {max_value}"
            }), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Unsupported method"}), 405


@bp.route("/volume", methods=["GET", "POST"])
def volume():
    try:
        card_index = get_usb_pcm_card_index()
        base = amixer_base_args_for_card(card_index)
        control = "PCM"
        if request.method == "GET":
            output = subprocess.check_output(
                ["amixer", *base, "get", control], text=True
            )
            m = re.search(r"\[(\d{1,3})%\]", output)
            if not m:
                return jsonify({"error": f"Could not parse volume for {control}"}), 500
            return jsonify({
                "volume": int(m.group(1)),
                "control": control,
                "target": "default" if card_index is None else f"card {card_index}",
            })
        data = request.get_json()
        if data is None or "volume" not in data:
            return jsonify({"error": "Missing volume"}), 400
        value = int(data["volume"])
        if not (0 <= value <= 100):
            return jsonify({"error": "Volume must be 0–100"}), 400
        subprocess.check_call(["amixer", *base, "set", control, f"{value}%"])
        return jsonify({
            "volume": value,
            "control": control,
            "target": "default" if card_index is None else f"card {card_index}",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/device-info")
def device_info():
    try:
        max_attempts = 6
        for attempt in range(max_attempts):
            mic_name = "Unknown"
            speaker_name = "Unknown"
            fallback_mic = None
            fallback_speaker = None
            devices = sd.query_devices()
            for dev in devices:
                if fallback_mic is None and dev["max_input_channels"] > 0:
                    fallback_mic = dev["name"]
                if fallback_speaker is None and dev["max_output_channels"] > 0:
                    fallback_speaker = dev["name"]
                if (
                    mic_name == "Unknown"
                    and dev["max_input_channels"] > 0
                    and _preference_matches_device_name(
                        core_config.MIC_PREFERENCE, str(dev["name"])
                    )
                ):
                    mic_name = dev["name"]
                if (
                    speaker_name == "Unknown"
                    and dev["max_output_channels"] > 0
                    and _preference_matches_device_name(
                        core_config.SPEAKER_PREFERENCE, str(dev["name"])
                    )
                ):
                    speaker_name = dev["name"]
            # If preferred device is not matchable yet, still show first available
            # device so UI does not remain "Unknown" while audio itself is usable.
            if mic_name == "Unknown" and fallback_mic:
                mic_name = fallback_mic
            if speaker_name == "Unknown" and fallback_speaker:
                speaker_name = fallback_speaker
            if mic_name != "Unknown" or speaker_name != "Unknown":
                return jsonify({"mic": mic_name, "speaker": speaker_name})
            if attempt < max_attempts - 1:
                time.sleep(0.4)
        return jsonify({"mic": mic_name, "speaker": speaker_name})
    except Exception as e:
        return jsonify({"mic": "Unknown", "speaker": "Unknown", "error": str(e)}), 500
