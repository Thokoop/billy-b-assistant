import signal
import sys
import threading
import traceback
from pathlib import Path


# Bootstrap before importing anything from core (including its logger).
if __name__ == '__main__':
    from billy_runtime.supervisor import monitor, worker_environment

    if '--billy-worker' not in sys.argv:
        monitor(Path(__file__), 'core')
        raise SystemExit(0)
    worker_environment()

# --- Imports that might use environment variables ---
from pathlib import Path

import core.button
from core.audio import initialize_devices, playback_queue
from core.bluetooth import watcher as bluetooth_watcher

# --- Reload logger level after environment is loaded ---
from core.logger import logger, reload_log_level
from core.movements import start_motor_watchdog
from core.mqtt import start_mqtt, stop_mqtt
from core.readiness import clear_ready, mark_ready
from core.status_led import (
    cleanup_status_led,
    initialize_status_led,
    set_status_led_state,
)


current_level = reload_log_level()
logger.info(f"🔧 Log level set to: {current_level.name}")


def signal_handler(sig, frame):
    logger.info("Exiting cleanly (signal received).", "👋")
    clear_ready()
    set_status_led_state("stopping")
    core.button.stop_background_services()
    playback_queue.put(None)
    from core.movements import cleanup_gpio

    cleanup_gpio()
    cleanup_status_led()
    stop_mqtt()
    sys.exit(0)


def main():
    clear_ready()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    initialize_status_led()
    set_status_led_state("starting")

    # Load default user profile BEFORE starting button loop
    # This ensures the persona manager is set to the correct persona before any sessions start
    from core.profile_manager import user_manager

    user_manager.load_default_user()

    initialize_devices()
    start_motor_watchdog()

    def ready():
        # Button setup and the boot animation finish before a connected phone
        # or MQTT command can take the audio hardware over.
        bluetooth_watcher.start()
        threading.Thread(target=start_mqtt, daemon=True).start()
        mark_ready()
        from billy_runtime.worker import ready as report_runtime_ready

        report_runtime_ready()

    core.button.start_loop(on_ready=ready)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Unhandled exception occurred: {e}")
        traceback.print_exc()
        clear_ready()
        set_status_led_state("error")
        core.button.stop_background_services()
        from core.movements import cleanup_gpio

        cleanup_gpio()
        cleanup_status_led()
        stop_mqtt()
        sys.exit(1)
