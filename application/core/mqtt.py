from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import threading
import time

import paho.mqtt.client as mqtt

from .config import (
    CHUNK_MS,
    MQTT_DEVICE_ID,
    MQTT_HOST,
    MQTT_PASSWORD,
    MQTT_PORT,
    MQTT_USERNAME,
    mqtt_canonical_topic,
    mqtt_wire_topic,
)
from .logger import logger
from .movements import stop_all_motors
from .status_led import set_status_led_state


mqtt_client: mqtt.Client | None = None
mqtt_connected = False
_last_state = "idle"
_say_lock = threading.Lock()
_lifecycle_lock = threading.Lock()
_shutdown_requested = threading.Event()
_mqtt_thread: threading.Thread | None = None

# Topics added by extension devices (core.devices): topic -> (handler,
# optional Home Assistant discovery as (config topic, payload)).
_extra_topics: dict[str, tuple] = {}
_extra_topics_lock = threading.Lock()


def mqtt_available():
    return all([MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD])


def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    with _lifecycle_lock:
        if _shutdown_requested.is_set() or client is not mqtt_client:
            return
        mqtt_connected = rc == 0
    if rc == 0:
        logger.success("MQTT connected successfully!", "🔌")
        mqtt_send_discovery()
        mqtt_publish("billy/state", _last_state, retain=True, retry=False)
        with contextlib.suppress(Exception):
            from .mood import mood_manager

            mood_manager.publish()
        with contextlib.suppress(Exception):
            from .song_manager import song_manager

            last_song = song_manager.get_last_song()
            if last_song:
                mqtt_publish("billy/song/last", last_song, retain=True, retry=False)
        client.subscribe(mqtt_wire_topic("billy/command"))
        client.subscribe(mqtt_wire_topic("billy/say"))  # single endpoint
        client.subscribe(mqtt_wire_topic("billy/song"))
        client.subscribe(mqtt_wire_topic("billy/wakeup/play"))
        client.subscribe(mqtt_wire_topic("billy/ringtone/play"))
        with _extra_topics_lock:
            extra = list(_extra_topics)
        for topic in extra:
            client.subscribe(mqtt_wire_topic(topic))
    else:
        logger.warning(f"MQTT connection failed with code {rc}")


def on_disconnect(client, userdata, rc):
    global mqtt_connected
    with _lifecycle_lock:
        if client is not mqtt_client:
            return
        mqtt_connected = False
    if not _shutdown_requested.is_set() and rc:
        logger.warning("MQTT disconnected unexpectedly; waiting for reconnect.", "🔌")


def _mqtt_worker():
    """Own Paho startup and cleanup so loop_stop cannot overtake loop_start."""
    global mqtt_client, mqtt_connected, _mqtt_thread
    client = None
    loop_started = False
    try:
        client = mqtt.Client()
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        # Paho's network loop handles initial connection retries and reconnects.
        # Separate reconnect threads could reopen the socket during shutdown.
        client.reconnect_delay_set(min_delay=5, max_delay=60)
        with _lifecycle_lock:
            if _shutdown_requested.is_set():
                return
            mqtt_client = client
        client.connect_async(MQTT_HOST, MQTT_PORT, 60)
        if _shutdown_requested.is_set():
            return
        result = client.loop_start()
        if result != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT network loop could not start: {result}")
        loop_started = True
        if _shutdown_requested.is_set():
            return
        _shutdown_requested.wait()
    except Exception as exc:
        if not _shutdown_requested.is_set():
            logger.error(f"MQTT connection error: {exc}")
    finally:
        if client is not None:
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning(f"MQTT disconnect failed: {exc}")
            if loop_started:
                try:
                    client.loop_stop()
                except Exception as exc:
                    logger.warning(f"MQTT network loop cleanup failed: {exc}")
        with _lifecycle_lock:
            if mqtt_client is client:
                mqtt_client = None
                mqtt_connected = False
            if _mqtt_thread is threading.current_thread():
                _mqtt_thread = None
        logger.info("MQTT stopped.", "🔌")


def start_mqtt():
    """Start at most one MQTT owner; shutdown is terminal for this process."""
    global _mqtt_thread
    if not mqtt_available():
        logger.info("MQTT not configured, skipping.")
        return
    with _lifecycle_lock:
        if _shutdown_requested.is_set() or _mqtt_thread is not None:
            return
        _mqtt_thread = threading.Thread(
            target=_mqtt_worker, name="billy-mqtt", daemon=True
        )
        try:
            _mqtt_thread.start()
        except Exception:
            _mqtt_thread = None
            raise


def stop_mqtt(*, wait=True, timeout=2.0):
    """Request cleanup without racing startup or delaying other hardware cleanup."""
    global mqtt_connected
    deadline = time.monotonic() + max(0, timeout)
    _shutdown_requested.set()
    mqtt_connected = False
    if not wait:
        # A Paho callback must return before the owner can join its network loop.
        return
    if not _lifecycle_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        logger.warning("MQTT startup is still finishing; cleanup deferred.")
        return
    try:
        worker = _mqtt_thread
        mqtt_connected = False
    finally:
        _lifecycle_lock.release()
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive():
            logger.warning("MQTT cleanup is still finishing in the background.")


def mqtt_publish(topic, payload, retain=True, retry=True):
    global _last_state
    if _shutdown_requested.is_set():
        return

    if topic == "billy/state":
        _last_state = payload
        set_status_led_state(str(payload))

    if mqtt_available():
        client = mqtt_client
        if client is None or not mqtt_connected:
            # Keep retry in the public signature for existing callers. Paho's
            # running network loop retries regardless of individual publishes.
            logger.verbose(
                f"MQTT not connected. Skipping publish {topic}={payload}", "🔌"
            )
            return

        try:
            client.publish(mqtt_wire_topic(topic), payload, retain=retain)
            logger.verbose(f"MQTT publish: {topic} = {payload} (retain={retain})", "📡")
        except Exception as e:
            logger.error(f"MQTT publish failed: {e}")


def register_topic(topic: str, handler, *, discovery: tuple | list | None = None):
    """Let an extension device handle an MQTT topic on Billy's connection.

    ``handler(payload: str)`` runs on the MQTT network thread and must return
    quickly (start a thread for anything slow). ``discovery`` is an optional
    Home Assistant ``(config_topic, payload)``, or a list of them for several
    entities on one topic; payloads get Billy's device block. Retained
    messages are not passed on: these topics carry commands. Safe to call before or after the connection is up.
    Returns a function that removes the topic again.
    """
    with _extra_topics_lock:
        _extra_topics[topic] = (handler, discovery)
    client = mqtt_client
    if client is not None and mqtt_connected:
        with contextlib.suppress(Exception):
            client.subscribe(mqtt_wire_topic(topic))
            _publish_extra_discovery(client, discovery)

    def unregister():
        with _extra_topics_lock:
            if _extra_topics.get(topic, (None,))[0] is handler:
                del _extra_topics[topic]
        current = mqtt_client
        if current is not None and mqtt_connected:
            with contextlib.suppress(Exception):
                current.unsubscribe(mqtt_wire_topic(topic))

    return unregister


def _publish_extra_discovery(client, discovery):
    """One ``(config_topic, payload)`` or a list of them (several entities)."""
    if not discovery:
        return
    if isinstance(discovery, tuple):
        discovery = [discovery]
    for config_topic, payload in discovery:
        _announce(client, config_topic, payload)


def _announce(client, config_topic: str, payload: dict | None):
    """Publish a Home Assistant discovery config under this Billy's identity.

    Payloads use the canonical "billy" names; with another MQTT_DEVICE_ID the
    topics, unique_id and config topic move to that name, so two Billys on one
    broker show up as two devices.
    """
    if payload is None:
        # An empty retained config removes an entity from Home Assistant.
        if MQTT_DEVICE_ID != "billy":
            config_topic = config_topic.replace("/billy/", f"/{MQTT_DEVICE_ID}/", 1)
        client.publish(config_topic, "", retain=True)
        return
    payload = dict(payload)
    for key, value in payload.items():
        if key.endswith("_topic") and isinstance(value, str):
            payload[key] = mqtt_wire_topic(value)
    if MQTT_DEVICE_ID != "billy":
        unique_id = payload.get("unique_id")
        if isinstance(unique_id, str) and unique_id.startswith("billy_"):
            payload["unique_id"] = f"{MQTT_DEVICE_ID}_{unique_id[len('billy_') :]}"
        config_topic = config_topic.replace("/billy/", f"/{MQTT_DEVICE_ID}/", 1)
    payload["device"] = _discovery_device()
    client.publish(config_topic, json.dumps(payload), retain=True)


def _dispatch_extra_topic(topic: str, payload: str, retained: bool = False) -> bool:
    with _extra_topics_lock:
        entry = _extra_topics.get(topic)
    if entry is None:
        return False
    if retained:
        # A broker replays retained messages on every (re)connect; a command
        # such as "ring" must not run again each time Billy starts.
        logger.info(f"Ignoring retained MQTT command on {topic}.", "🔕")
        return True
    try:
        entry[0](payload)
    except Exception as exc:
        logger.error(f"MQTT handler for {topic} failed: {exc}")
    return True


def _discovery_device():
    if MQTT_DEVICE_ID == "billy":
        return {
            "identifiers": ["billy_bass"],
            "name": "Big Mouth Billy Bass",
            "model": "Billy Bassistant",
            "manufacturer": "ThingsFromThom",
        }
    model = "Billy Bassistant"
    with contextlib.suppress(Exception):
        from .devices import active_device_model

        model = active_device_model() or model
    return {
        "identifiers": [f"billy_{MQTT_DEVICE_ID}"],
        "name": MQTT_DEVICE_ID,
        "model": model,
        "manufacturer": "ThingsFromThom",
    }


def _play_ringtone_preview(payload: bytes):
    """Play one ringtone from the Bluetooth settings (the Play button)."""
    try:
        from .audio import enqueue_wav_to_playback, ensure_playback_worker_started
        from .bluetooth import mode as bluetooth_mode
        from .bluetooth import ringtones

        ringtone_id = str(json.loads(payload.decode() or "{}").get("id") or "")
        entry = ringtones.find(ringtone_id)
        if entry is None or not ringtones.path_for(entry).is_file():
            logger.warning(f"Ringtone preview not found: {ringtone_id}", "⚠️")
            return
        if bluetooth_mode.active():
            logger.info(
                "Ignoring ringtone preview: Bluetooth audio owns the speaker.", "🔕"
            )
            return
        ensure_playback_worker_started(CHUNK_MS)
        enqueue_wav_to_playback(str(ringtones.path_for(entry)))
        logger.info(f"Playing ringtone preview: {entry.get('name')}", "🔔")
    except Exception as exc:
        logger.error(f"Failed to play the ringtone preview: {exc}")


def mqtt_send_discovery():
    """Send MQTT discovery messages for Home Assistant."""
    if not mqtt_client:
        return

    device = _discovery_device()  # replaced per payload by _announce
    with _extra_topics_lock:
        extra_discovery = [entry[1] for entry in _extra_topics.values()]
    for discovery in extra_discovery:
        with contextlib.suppress(Exception):
            _publish_extra_discovery(mqtt_client, discovery)

    # Sensor for Billy's state
    payload_sensor = {
        "name": "Billy State",
        "unique_id": "billy_state",
        "state_topic": "billy/state",
        "icon": "mdi:fish",
        "device": device,
    }
    _announce(mqtt_client, "homeassistant/sensor/billy/state/config", payload_sensor)

    payload_mood_sensor = {
        "name": "Billy Mood",
        "unique_id": "billy_mood",
        "state_topic": "billy/mood",
        "json_attributes_topic": "billy/mood/state",
        "icon": "mdi:emoticon-outline",
        "device": device,
    }
    _announce(
        mqtt_client, "homeassistant/sensor/billy/mood/config", payload_mood_sensor
    )

    # Button to send shutdown command
    payload_button = {
        "name": "Billy Shutdown",
        "unique_id": "billy_shutdown",
        "command_topic": "billy/command",
        "payload_press": "shutdown",
        "device": device,
    }
    _announce(mqtt_client, "homeassistant/button/billy/shutdown/config", payload_button)

    # Button to restart Billy service
    payload_button_restart = {
        "name": "Billy Restart",
        "unique_id": "billy_restart",
        "command_topic": "billy/command",
        "payload_press": "restart-billy",
        "device": device,
    }
    _announce(
        mqtt_client, "homeassistant/button/billy/restart/config", payload_button_restart
    )

    # Button to reboot the system
    payload_button_reboot = {
        "name": "Billy Reboot",
        "unique_id": "billy_reboot",
        "command_topic": "billy/command",
        "payload_press": "reboot",
        "device": device,
    }
    _announce(
        mqtt_client, "homeassistant/button/billy/reboot/config", payload_button_reboot
    )

    # Button to toggle listening
    payload_button_listen = {
        "name": "Billy Listen",
        "unique_id": "billy_listen",
        "command_topic": "billy/command",
        "payload_press": "listen",
        "device": device,
    }
    _announce(
        mqtt_client, "homeassistant/button/billy/listen/config", payload_button_listen
    )

    # Single text entity
    payload_text_input = {
        "name": "Billy Say",
        "unique_id": "billy_say",
        "command_topic": "billy/say",
        "mode": "text",
        "max": 255,
        "device": device,
    }
    _announce(mqtt_client, "homeassistant/text/billy/say/config", payload_text_input)

    # Single text entity for playing songs
    payload_song_input = {
        "name": "Billy Song",
        "unique_id": "billy_song",
        "command_topic": "billy/song",
        "mode": "text",
        "max": 255,
        "device": device,
    }
    _announce(mqtt_client, "homeassistant/text/billy/song/config", payload_song_input)

    # Sensor for the last song Billy played (e.g. offline Song mode picks)
    payload_last_song_sensor = {
        "name": "Billy Last Song",
        "unique_id": "billy_last_song",
        "state_topic": "billy/song/last",
        "icon": "mdi:music",
        "device": device,
    }
    _announce(
        mqtt_client,
        "homeassistant/sensor/billy/last_song/config",
        payload_last_song_sensor,
    )


# ----- Helpers ----------------------------------------------------------

FORCE_OFF_TAGS = ("[[nochat]]", "[[announce-only]]", "[[one-shot]]", "[[no-follow-up]]")
FORCE_ON_TAGS = ("[[chat]]", "[[follow-up]]")


def _parse_say_payload(raw: str):
    """
    Accept raw text or JSON: {"text":"...", "interactive": true/false}
    Plus inline flags inside text:
      [[nochat]] / [[announce-only]] / [[one-shot]] / [[no-follow-up]] -> interactive=False
      [[chat]] / [[follow-up]] -> interactive=True
    Returns (clean_text, interactive: None|True|False)
    """
    s = raw.strip()
    interactive = None
    text = s

    # JSON override (still single endpoint; optional for power-users)
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            text = str(data.get("text", "")).strip()
            if "interactive" in data:
                interactive = bool(data["interactive"])
    except json.JSONDecodeError:
        pass

    low = text.lower()

    # Inline flags take precedence over JSON 'interactive'
    for tag in FORCE_OFF_TAGS:
        if tag in low:
            interactive = False
            text = re_sub_ignorecase(text, tag, "")

    for tag in FORCE_ON_TAGS:
        if tag in low:
            interactive = True
            text = re_sub_ignorecase(text, tag, "")

    return text.strip(), interactive


def _parse_song_payload(raw: str) -> str:
    """Accept raw text or JSON: {"song":"..."}; returns song name or empty string."""
    s = raw.strip()
    song = s
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            song = str(data.get("song", "")).strip()
    except json.JSONDecodeError:
        pass
    return song.strip()


def re_sub_ignorecase(s: str, find: str, repl: str) -> str:
    import re

    return re.sub(re.escape(find), repl, s, flags=re.IGNORECASE)


def _run_async(coro):
    def _runner():
        asyncio.run(coro)

    threading.Thread(target=_runner, daemon=True).start()


# Helper functions for toggle listening
def mqtt_toggle_listening():
    from . import button as button_mod

    if button_mod.is_active:
        mqtt_stop_listening()
    else:
        mqtt_start_listening()


def mqtt_start_listening():
    import contextlib
    import time

    from . import button as button_mod
    from .bluetooth import mode as bluetooth_mode

    if bluetooth_mode.active():
        logger.info(
            "Ignoring MQTT start: Bluetooth audio owns the speaker and mic.", "🔕"
        )
        return

    if button_mod.is_active:
        return

    if not button_mod._session_start_lock.acquire(blocking=False):
        logger.warning("Session start already in progress, ignoring MQTT start", "⚠️")
        return

    try:
        if button_mod.session_thread and button_mod.session_thread.is_alive():
            button_mod.session_thread.join(timeout=2.0)
            if button_mod.session_thread.is_alive():
                logger.warning(
                    "Previous session thread did not finish, attempting forced stop",
                    "⚠️",
                )
                if button_mod.session_instance and button_mod.session_instance.loop:
                    with contextlib.suppress(Exception):
                        future = asyncio.run_coroutine_threadsafe(
                            button_mod.session_instance.request_stop(),
                            button_mod.session_instance.loop,
                        )
                        future.result(timeout=1.0)
                    with contextlib.suppress(Exception):
                        future = asyncio.run_coroutine_threadsafe(
                            button_mod.session_instance._close_ws(timeout=0.5),
                            button_mod.session_instance.loop,
                        )
                        future.result(timeout=2.0)
                button_mod.session_thread.join(timeout=1.5)
                if button_mod.session_thread.is_alive():
                    if not button_mod.is_active:
                        logger.warning(
                            "Detaching stale inactive session thread and continuing",
                            "🧹",
                        )
                        button_mod.session_thread = None
                        button_mod.session_instance = None
                    else:
                        logger.error(
                            "Previous session thread did not finish, aborting new session",
                            "❌",
                        )
                        button_mod._session_start_lock.release()
                        return

        button_mod.audio.ensure_playback_worker_started(button_mod.config.CHUNK_MS)
        button_mod.audio.playback_done_event.clear()
        threading.Thread(
            target=button_mod.audio.play_random_wake_up_clip, daemon=True
        ).start()

        button_mod.is_active = True
        button_mod.interrupt_event = threading.Event()

        def run_session():
            try:
                button_mod.move_head("on")
                button_mod.session_instance = button_mod.BillySession(
                    interrupt_event=button_mod.interrupt_event
                )
                button_mod.session_instance.last_activity[0] = time.time()
                asyncio.run(button_mod.session_instance.start())
            finally:
                button_mod.move_head("off")
                button_mod.is_active = False
                button_mod.session_instance = None
                with contextlib.suppress(Exception):
                    button_mod._session_start_lock.release()

        button_mod.session_thread = threading.Thread(target=run_session, daemon=True)
        button_mod.session_thread.start()
    except Exception:
        with contextlib.suppress(Exception):
            button_mod._session_start_lock.release()
        raise


def mqtt_stop_listening():
    import contextlib
    from concurrent.futures import CancelledError

    from . import button as button_mod

    if not button_mod.is_active:
        return

    button_mod.interrupt_event.set()
    button_mod.audio.stop_playback()

    if button_mod.session_instance:
        with contextlib.suppress(CancelledError):
            future = asyncio.run_coroutine_threadsafe(
                button_mod.session_instance.stop_session(),
                button_mod.session_instance.loop,
            )
            try:
                future.result(timeout=5.0)
            except TimeoutError:
                logger.warning("MQTT stop timeout, forcing cleanup", "⚠️")
                with contextlib.suppress(Exception):
                    force_stop_future = asyncio.run_coroutine_threadsafe(
                        button_mod.session_instance.request_stop(),
                        button_mod.session_instance.loop,
                    )
                    force_stop_future.result(timeout=1.0)
                with contextlib.suppress(Exception):
                    force_close_future = asyncio.run_coroutine_threadsafe(
                        button_mod.session_instance._close_ws(timeout=0.5),
                        button_mod.session_instance.loop,
                    )
                    force_close_future.result(timeout=2.0)

    button_mod.is_active = False


# -----------------------------------------------------------------------


def on_message(client, userdata, msg):
    if _shutdown_requested.is_set() or client is not mqtt_client:
        return
    logger.verbose(f"MQTT message received: {msg.topic} = {msg.payload.decode()}", "📩")
    topic = mqtt_canonical_topic(msg.topic)
    if _dispatch_extra_topic(
        topic,
        msg.payload.decode(errors="replace"),
        retained=bool(getattr(msg, "retain", False)),
    ):
        return
    if topic == "billy/command":
        command = msg.payload.decode().strip().lower()
        if command == "shutdown":
            logger.warning(
                "Shutdown command received over MQTT. Shutting down...", "🛑"
            )
            try:
                stop_all_motors()
            except Exception as e:
                logger.warning(f"Error stopping motors: {e}")
            stop_mqtt(wait=False)
            subprocess.Popen(["sudo", "shutdown", "now"])

        elif command == "restart-billy":
            logger.warning(
                "Restart Billy command received over MQTT. Restarting service...", "🔁"
            )
            try:
                stop_all_motors()
            except Exception as e:
                logger.warning(f"Error stopping motors: {e}")
            subprocess.Popen(["sudo", "systemctl", "restart", "billy.service"])
        elif command == "reboot":
            logger.warning(
                "Reboot command received over MQTT. Rebooting system...", "🔁"
            )
            try:
                stop_all_motors()
            except Exception as e:
                logger.warning(f"Error stopping motors: {e}")
            stop_mqtt(wait=False)
            subprocess.Popen(["sudo", "shutdown", "-r", "now"])
        elif command == "listen":
            logger.warning(
                "Listen command received over MQTT. Starting or stopping listening...",
                "🔁",
            )
            try:
                mqtt_toggle_listening()
            except Exception as e:
                logger.warning(f"Error starting/stopping listening: {e}")
        return

    if topic == "billy/say":
        logger.info(f"Received SAY command: {msg.payload.decode()}", "📩")

        import asyncio

        # 🔁 Lazy import here to avoid circular import with session.py
        from .say import say

        try:
            text, interactive = _parse_say_payload(msg.payload.decode())
            if text:
                # Default behavior:
                # - literal text => one-shot announce
                # - prompt text ({{...}}) => auto follow-up behavior
                if interactive is None:
                    is_prompt = text.startswith("{{") and text.endswith("}}")
                    interactive = None if is_prompt else False

                from .devices import device_handles_say

                is_prompt = text.startswith("{{") and text.endswith("}}")
                opening = {
                    "text": text[2:-2].strip() if is_prompt else text,
                    "kind": "prompt" if is_prompt else "literal",
                    "interactive": interactive,
                }
                if device_handles_say(opening):
                    logger.info("SAY handed to the device.", "📨")
                    return

                if not _say_lock.acquire(blocking=False):
                    logger.warning(
                        "SAY command ignored: another SAY request is still running.",
                        "⏳",
                    )
                    return

                def run_say():
                    try:
                        logger.info(
                            f"SAY dispatch | interactive={interactive} | text={text!r}",
                            "🗣️",
                        )
                        asyncio.run(say(text=text, interactive=interactive))
                    finally:
                        _say_lock.release()

                threading.Thread(target=run_say, daemon=True).start()
            else:
                logger.warning("SAY command received, but text was empty")
        except Exception as e:
            with contextlib.suppress(Exception):
                if _say_lock.locked():
                    _say_lock.release()
            logger.error(f"Failed to run say(): {e}")
        return

    if topic == "billy/song":
        logger.info(f"📩 Received SONG command: {msg.payload.decode()}")
        try:
            from .audio import play_song

            song_name = _parse_song_payload(msg.payload.decode())
            if song_name:
                _run_async(play_song(song_name))
            else:
                logger.warning("SONG command received, but song name was empty")
        except Exception as e:
            logger.error(f"Failed to run play_song(): {e}")
        return

    if topic == "billy/ringtone/play":
        _play_ringtone_preview(msg.payload)
        return

    if topic == "billy/wakeup/play":
        try:
            payload = json.loads(msg.payload.decode() or "{}")
            index = int(payload.get("index", 0))
            persona_name = str(payload.get("persona", "")).strip() or None

            if index < 1 or index > 99:
                logger.warning(
                    f"Invalid wakeup preview index received over MQTT: {index}", "⚠️"
                )
                return

            if not persona_name:
                from .persona_manager import persona_manager

                persona_name = persona_manager.current_persona

            from billy_runtime.paths import INSTALL_ROOT

            project_root = str(INSTALL_ROOT)
            sound_path = None
            if persona_name and persona_name != "default":
                persona_sound_path = os.path.join(
                    project_root, "personas", persona_name, "wakeup", f"{index}.wav"
                )
                if os.path.exists(persona_sound_path):
                    sound_path = persona_sound_path
            elif persona_name == "default":
                sound_path = os.path.join(
                    project_root, "sounds", "wake-up", "custom", f"{index}.wav"
                )

            if not sound_path:
                sound_path = os.path.join(
                    project_root, "sounds", "wake-up", "custom", f"{index}.wav"
                )

            if not os.path.exists(sound_path):
                logger.warning(
                    f"Wakeup preview clip not found: persona={persona_name} index={index}",
                    "⚠️",
                )
                return

            from .audio import enqueue_wav_to_playback, ensure_playback_worker_started
            from .bluetooth import mode as bluetooth_mode

            if bluetooth_mode.active():
                # The playback queue is carrying a call; a preview clip would
                # play into it.
                logger.info(
                    "Ignoring MQTT wakeup preview: Bluetooth audio owns the speaker.",
                    "🔕",
                )
                return

            ensure_playback_worker_started(CHUNK_MS)
            enqueue_wav_to_playback(sound_path)
            logger.info(
                f"Queued wakeup preview via MQTT: persona={persona_name} index={index}",
                "🔊",
            )
        except Exception as e:
            logger.error(f"Failed to process MQTT wakeup preview: {e}")
