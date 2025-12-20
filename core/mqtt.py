import asyncio
import json
import subprocess
import threading

import paho.mqtt.client as mqtt

from .config import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USERNAME
from .logger import logger
from .movements import stop_all_motors


mqtt_client: mqtt.Client | None = None
mqtt_connected = False


def mqtt_available():
    return all([MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD])


def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        logger.success("MQTT connected successfully!", "🔌")
        mqtt_send_discovery()
        client.subscribe("billy/command")
        client.subscribe("billy/say")  # single endpoint
    else:
        logger.warning(f"MQTT connection failed with code {rc}")


def start_mqtt():
    global mqtt_client
    if not mqtt_available():
        logger.warning("MQTT not configured, skipping.")
        return

    mqtt_client = mqtt.Client()
    mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        # Make the MQTT loop thread daemon so it doesn't prevent process exit
        if hasattr(mqtt_client, '_thread') and mqtt_client._thread:
            mqtt_client._thread.daemon = True
        mqtt_publish("billy/state", "idle", retain=True)
    except Exception as e:
        logger.error(f"MQTT connection error: {e}")


def stop_mqtt():
    global mqtt_client
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        logger.info("MQTT disconnected.", "🔌")


def mqtt_publish(topic, payload, retain=True, retry=True):
    global mqtt_client, mqtt_connected

    if mqtt_available():
        if not mqtt_client or not mqtt_connected:
            if retry:
                logger.info("MQTT not connected. Trying to reconnect...", "🔁")
                try:
                    mqtt_client.reconnect()
                    mqtt_connected = True
                except Exception as e:
                    logger.error(f"MQTT reconnect failed: {e}")
                    return
            else:
                logger.warning(
                    f"MQTT not connected. Skipping publish {topic}={payload}"
                )
                return

        try:
            mqtt_client.publish(topic, payload, retain=retain)
            logger.verbose(f"MQTT publish: {topic} = {payload} (retain={retain})", "📡")
        except Exception as e:
            logger.error(f"MQTT publish failed: {e}")


def mqtt_send_discovery():
    """Send MQTT discovery messages for Home Assistant."""
    if not mqtt_client:
        return

    device = {
        "identifiers": ["billy_bass"],
        "name": "Big Mouth Billy Bass",
        "model": "Billy Bassistant",
        "manufacturer": "ThingsFromThom",
    }

    # Sensor for Billy's state
    payload_sensor = {
        "name": "Billy State",
        "unique_id": "billy_state",
        "state_topic": "billy/state",
        "icon": "mdi:fish",
        "device": device,
    }
    mqtt_client.publish(
        "homeassistant/sensor/billy/state/config",
        json.dumps(payload_sensor),
        retain=True,
    )

    # Button to send shutdown command
    payload_button = {
        "name": "Billy Shutdown",
        "unique_id": "billy_shutdown",
        "command_topic": "billy/command",
        "payload_press": "shutdown",
        "device": device,
    }
    mqtt_client.publish(
        "homeassistant/button/billy/shutdown/config",
        json.dumps(payload_button),
        retain=True,
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
    mqtt_client.publish(
        "homeassistant/text/billy/say/config",
        json.dumps(payload_text_input),
        retain=True,
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


def re_sub_ignorecase(s: str, find: str, repl: str) -> str:
    import re

    return re.sub(re.escape(find), repl, s, flags=re.IGNORECASE)


def _run_async(coro):
    def _runner():
        asyncio.run(coro)

    threading.Thread(target=_runner, daemon=True).start()


# -----------------------------------------------------------------------


def on_message(client, userdata, msg):
    logger.verbose(f"MQTT message received: {msg.topic} = {msg.payload.decode()}", "📩")
    if msg.topic == "billy/command":
        command = msg.payload.decode().strip().lower()
        if command == "shutdown":
            logger.warning(
                "Shutdown command received over MQTT. Shutting down...", "🛑"
            )
            try:
                stop_all_motors()
            except Exception as e:
                logger.warning(f"Error stopping motors: {e}")
            stop_mqtt()
            subprocess.Popen(["sudo", "shutdown", "now"])
        return

    if msg.topic == "billy/say":
        print(f"📩 Received SAY command: {msg.payload.decode()}")

        import asyncio
        import threading

        # 🔁 Lazy import here to avoid circular import with session.py
        from .say import say

        try:
            text = msg.payload.decode().strip()
            if text:

                def run_say():
                    asyncio.run(say(text=text))  # interactive=None -> AUTO follow-up

                threading.Thread(target=run_say, daemon=True).start()
            else:
                print("⚠️ SAY command received, but text was empty")
        except Exception as e:
            logger.error(f"Failed to run say(): {e}")
