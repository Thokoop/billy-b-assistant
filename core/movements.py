import atexit
import contextlib
import random
import threading
import time
from threading import Lock, Thread

import numpy as np

from .config import BILLY_PINS, MOCKFISH, is_classic_billy
from .logger import logger


try:
    import lgpio

    lgpio_available = True
except ImportError:
    lgpio_available = False

if MOCKFISH or not lgpio_available:
    # Mock lgpio for development or when not available
    class MockLgpio:
        error = Exception

        @staticmethod
        def gpiochip_open(chip):
            return "mock_handle"

        @staticmethod
        def gpio_claim_output(h, pin):
            pass

        @staticmethod
        def gpio_write(h, pin, value):
            pass

        @staticmethod
        def tx_pwm(h, pin, freq, duty):
            pass

        @staticmethod
        def gpio_free(h, pin):
            pass

        @staticmethod
        def gpiochip_close(h):
            pass

    lgpio = MockLgpio
    if MOCKFISH:
        logger.info("Mockfish: GPIO mocked for development", "🐟")
    elif not lgpio_available:
        logger.info("lgpio not available: GPIO mocked", "🐟")


# === Configuration ===
USE_THIRD_MOTOR = is_classic_billy()
logger.info(f"Using third motor: {USE_THIRD_MOTOR} | Pin profile: {BILLY_PINS}", "⚙️")

# === GPIO Setup ===
h = lgpio.gpiochip_open(0)
FREQ = 10000  # PWM frequency
_gpio_active = True  # Flag to track if GPIO handle is still valid

# -------------------------------------------------------------------
# Pin mapping by profile
# -------------------------------------------------------------------
# We normalize to three "drive" pins (MOUTH, HEAD, TAIL) and up to three
# "mates" that must be held LOW for legacy wiring (GND_1..GND_3).
MOUTH = GND_1 = HEAD = TAIL = GND_2 = GND_3 = None

if BILLY_PINS == "legacy":
    # Original wiring (backwards compatible)
    # Controller 1: IN1=HEAD, IN2=TAIL (2-motor legacy) | IN3=MOUTH, IN4=GND_1
    MOUTH = 12
    HEAD = 13
    TAIL = 6
    GND_1 = 5
    if USE_THIRD_MOTOR:
        # Classic Billy (3 motors): dedicated tail bridge on second driver
        TAIL = 19  # second driver IN1 (PWM)
        GND_2 = 6  # head mate (keep LOW)
        GND_3 = 26  # tail mate (keep LOW)
else:
    # NEW quiet wiring (mates are tied to GND in hardware)
    HEAD = 22  # pin 15
    MOUTH = 17  # pin 11
    TAIL = 27  # pin 13

# Collect all pins we actually use
motor_pins = [p for p in (MOUTH, HEAD, TAIL, GND_1, GND_2, GND_3) if p is not None]


def _is_gpio_busy_error(exc: Exception) -> bool:
    return "busy" in str(exc).lower()


def _release_claimed_pins(claimed_pins: list[int]):
    for claimed_pin in reversed(claimed_pins):
        with contextlib.suppress(lgpio.error, Exception):
            lgpio.gpio_write(h, claimed_pin, 0)
        with contextlib.suppress(lgpio.error, Exception):
            lgpio.gpio_free(h, claimed_pin)


def _claim_motor_pins() -> bool:
    global _gpio_active
    if MOCKFISH or not lgpio_available:
        return True

    max_attempts = 12
    claimed_pins: list[int] = []
    for attempt in range(1, max_attempts + 1):
        claimed_pins.clear()
        try:
            for pin in motor_pins:
                lgpio.gpio_claim_output(h, pin)
                claimed_pins.append(pin)
                lgpio.gpio_write(h, pin, 0)
            return True
        except lgpio.error as e:
            _release_claimed_pins(claimed_pins)
            if not _is_gpio_busy_error(e):
                raise

            wait_seconds = min(0.25 * attempt, 1.5)
            logger.warning(
                f"GPIO pin is busy during motor setup (attempt {attempt}/{max_attempts}); waiting {wait_seconds:.2f}s before retry.",
                "⚠️",
            )
            time.sleep(wait_seconds)

    logger.error(
        "Motor GPIO pins are still busy after retrying. Disabling motor output so Billy can keep running.",
        "❌",
    )
    _gpio_active = False
    return False


_claim_motor_pins()

# === State ===
_head_tail_lock = Lock()
_motor_watchdog_running = False
_last_flap = 0
_mouth_open_until = 0
_last_rms = 0
head_out = False

# === PWM tracking (so watchdog can see PWM activity) ===
_pwm = {pin: {"duty": 0, "since": None} for pin in motor_pins}

# Pending "stop" timer per pin, so a new run_motor_async() call for a pin
# that's already active can cancel the earlier call's stop timer instead of
# racing it - see run_motor_async() for why this matters.
_pin_stop_timers: dict[int, threading.Timer] = {}


def set_pwm(pin: int, duty: int):
    """Start/adjust PWM on pin and remember when it went active."""
    global _gpio_active
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    try:
        lgpio.tx_pwm(h, pin, FREQ, int(duty))
    except (lgpio.error, Exception):
        # Handle already closed or invalid - ignore during shutdown
        _gpio_active = False
        return
    if duty > 0:
        _pwm[pin]["duty"] = int(duty)
        _pwm[pin]["since"] = (
            time.time() if _pwm[pin]["since"] is None else _pwm[pin]["since"]
        )
    else:
        _pwm[pin]["duty"] = 0
        _pwm[pin]["since"] = None


def clear_pwm(pin: int):
    """Stop PWM on pin and clear active since timestamp."""
    global _gpio_active
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    try:
        lgpio.tx_pwm(h, pin, FREQ, 0)
    except (lgpio.error, Exception):
        # Handle already closed or invalid - ignore during shutdown
        _gpio_active = False
        return
    _pwm[pin]["duty"] = 0
    _pwm[pin]["since"] = None


# === Motor Helpers ===
def brake_motor(pin1, pin2=None):
    """Actively stop the channel: zero PWM and drive LOW."""
    global _gpio_active
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    clear_pwm(pin1)
    if pin2 is not None:
        clear_pwm(pin2)
        try:
            lgpio.gpio_write(h, pin2, 0)
        except (lgpio.error, Exception):
            _gpio_active = False
            return
    try:
        lgpio.gpio_write(h, pin1, 0)
    except (lgpio.error, Exception):
        _gpio_active = False
        return


def run_motor_async(pwm_pin, low_pin=None, speed_percent=100, duration=0.3, brake=True):
    global _gpio_active
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    if low_pin is not None:
        try:
            lgpio.gpio_write(h, low_pin, 0)
        except (lgpio.error, Exception):
            _gpio_active = False
            return

    # A pin can be re-triggered (e.g. mouth flaps during a sustained loud
    # note) before its previous call's stop timer has fired. Without this,
    # that earlier, shorter-duration timer still fires on schedule and cuts
    # power mid-hold, even though this newer call means to keep it open
    # longer - the motor visibly flickers/jitters instead of holding steady.
    # Only the most recent call should get to decide when the pin stops.
    pending = _pin_stop_timers.get(pwm_pin)
    if pending is not None:
        pending.cancel()

    set_pwm(pwm_pin, int(speed_percent))
    if brake:
        timer = threading.Timer(duration, lambda: brake_motor(pwm_pin, low_pin))
    else:
        # still auto-close after duration, but just clear PWM (no active brake)
        timer = threading.Timer(duration, lambda: clear_pwm(pwm_pin))
    _pin_stop_timers[pwm_pin] = timer
    timer.start()


# === Movement Functions (keep signatures/behavior) ===
def move_mouth(speed_percent, duration, brake=False):
    run_motor_async(MOUTH, GND_1, speed_percent, duration, brake)


def stop_mouth():
    brake_motor(MOUTH, GND_1)


def move_head(state="on"):
    global head_out

    def _move_head_on():
        global _gpio_active
        if not _gpio_active:
            return  # GPIO handle already closed, skip
        # Ensure opposite input is LOW if sharing a bridge (2-motor cases)
        # For 3-motor "new" layout, mate is hard GND so this is a no-op.
        if TAIL is not None:
            try:
                lgpio.gpio_write(h, TAIL, 0)
            except (lgpio.error, Exception):
                _gpio_active = False
                return
        set_pwm(HEAD, 80)
        time.sleep(0.5)
        set_pwm(HEAD, 100)  # stay extended

    if state == "on":
        if not head_out:
            threading.Thread(target=_move_head_on, daemon=True).start()
            head_out = True
    else:
        # Brake both sides of shared bridge where relevant
        brake_motor(HEAD, TAIL)
        head_out = False


def move_tail(duration=0.2):
    """
    Tail drive matrix:
      - legacy + classic(3): TAIL has dedicated bridge => mate = GND_3
      - legacy + modern(2):  shared with HEAD => mate = HEAD
      - new    + classic(3): dedicated channel with mate tied to GND => mate = None
      - new    + modern(2):  shared bridge with HEAD => mate = HEAD
    """
    if BILLY_PINS == "legacy":
        if USE_THIRD_MOTOR and TAIL is not None and GND_3 is not None:
            run_motor_async(TAIL, GND_3, speed_percent=80, duration=duration)
        else:
            run_motor_async(TAIL, HEAD, speed_percent=80, duration=duration)
    else:
        if USE_THIRD_MOTOR:
            run_motor_async(TAIL, None, speed_percent=80, duration=duration)
        else:
            run_motor_async(TAIL, HEAD, speed_percent=80, duration=duration)


def move_tail_async(duration=0.3):
    threading.Thread(target=move_tail, args=(duration,), daemon=True).start()


def _articulation_level_to_multiplier(level):
    """Map a 0-10 articulation level to the actual flap-duration multiplier.

    Used to map 1:1 (multiplier == level), which made the top of the range
    feel exaggerated once run_motor_async() stopped letting a stale timer
    cut a flap short (see its _pin_stop_timers) - a flap now actually holds
    for its full requested duration instead of sometimes getting cut off
    early. This compresses the range so the same felt intensity now sits
    roughly 1.5-2 levels higher than it used to (e.g. level 8 here feels
    about like the old level 5).
    """
    return 1 + max(0.0, min(10.0, float(level))) * 0.5


def _articulation_multiplier():
    """Return the current persona's articulation level (0-10, default 5)."""
    try:
        # Try to get mouth articulation from current persona
        from .persona_manager import persona_manager

        current_persona_data = persona_manager.get_current_persona_data()

        if current_persona_data and current_persona_data.get('meta', {}).get(
            'mouth_articulation'
        ):
            persona_articulation = current_persona_data['meta']['mouth_articulation']
            return max(0, min(10, float(persona_articulation)))
        # Fall back to global setting
        return 5
    except Exception as e:
        # Fall back to global setting on error
        return 5


# === Mouth Sync ===
def flap_from_pcm_chunk(
    audio,
    threshold=1500,
    min_flap_gap=0.15,
    chunk_ms=40,
    sample_rate=24000,
    articulation_override=None,
):
    global _last_flap, _mouth_open_until, _last_rms
    now = time.time()

    if audio.size == 0:
        return

    rms = np.sqrt(np.mean(audio.astype(np.float32) ** 2))
    peak = np.max(np.abs(audio))

    # Smooth out sudden fluctuations
    if '_last_rms' not in globals():
        _last_rms = rms
    alpha = 1  # smoothing factor
    rms = alpha * rms + (1 - alpha) * _last_rms
    _last_rms = rms

    # If too quiet and mouth might be open, stop motor
    if rms < threshold / 2 and now >= _mouth_open_until:
        stop_mouth()
        return

    if rms <= threshold or (now - _last_flap) < min_flap_gap:
        return

    normalized = np.clip(rms / 32768.0, 0.0, 1.0)
    dyn_range = peak / (rms + 1e-5)

    # Flap speed and duration scaling
    speed = int(np.clip(np.interp(normalized, [0.005, 0.15], [25, 100]), 25, 100))
    duration_ms = np.interp(normalized, [0.005, 0.15], [15, 70])

    duration_ms = np.clip(duration_ms, 15, chunk_ms)
    duration = duration_ms / 1000.0

    articulation_level = (
        articulation_override
        if articulation_override is not None
        else _articulation_multiplier()
    )
    duration *= _articulation_level_to_multiplier(articulation_level)

    _last_flap = now
    _mouth_open_until = now + duration

    move_mouth(speed, duration, brake=False)


# === Interlude Behavior ===
def _interlude_routine():
    try:
        move_head("off")
        time.sleep(random.uniform(0.2, 2))
        flap_count = random.randint(1, 3)
        for _ in range(flap_count):
            move_tail()
            time.sleep(random.uniform(0.25, 0.9))
        if random.random() < 0.9:
            move_head("on")
            # Head movement during interlude (no logging needed)
            # Auto-turn off head after max 3 seconds to prevent getting stuck
            threading.Timer(5.0, lambda: move_head("off")).start()
    except Exception as e:
        print(f"⚠️ Interlude error: {e}")


def interlude():
    """Run head/tail interlude in a background thread if not already running."""
    if _head_tail_lock.locked():
        return
    Thread(target=lambda: _interlude_routine(), daemon=True).start()


# === Motor Watchdog (per-pin continuous activity) ===
WATCHDOG_TIMEOUT_SEC = 30  # max continuous ON time per pin
WATCHDOG_POLL_SEC = 1.0  # poll cadence


def _mate_for(pin: int):
    """
    Return the logical 'mate' input that should be LOW when 'pin' drives.
    This lets the watchdog brake a channel safely.
    """
    if pin == MOUTH:
        return GND_1
    if pin == HEAD:
        if BILLY_PINS == "legacy":
            # legacy modern shares bridge with tail
            return TAIL
        # new layout: 3-motor => mate hard GND (None); 2-motor => mate is TAIL
        return None if USE_THIRD_MOTOR else TAIL
    if pin == TAIL:
        if BILLY_PINS == "legacy":
            return GND_3 if USE_THIRD_MOTOR else HEAD
        return None if USE_THIRD_MOTOR else HEAD
    return None


def _stop_channel(pin: int):
    """Brake one channel safely (pin + its mate)."""
    global _gpio_active
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    mate = _mate_for(pin)
    clear_pwm(pin)
    try:
        lgpio.gpio_write(h, pin, 0)
    except (lgpio.error, Exception):
        _gpio_active = False
        return
    if mate is not None:
        clear_pwm(mate)
        try:
            lgpio.gpio_write(h, mate, 0)
        except (lgpio.error, Exception):
            _gpio_active = False
            return


def _pin_is_active(pin: int) -> bool:
    """Active if line is HIGH or PWM duty > 0."""
    if not _gpio_active:
        # If GPIO is inactive, only check PWM state
        return _pwm.get(pin, {}).get("duty", 0) > 0
    try:
        if lgpio.gpio_read(h, pin) == 1:
            return True
    except (lgpio.error, Exception):
        # Handle might be closed, fall back to PWM state
        pass
    return _pwm.get(pin, {}).get("duty", 0) > 0


def stop_all_motors():
    global _gpio_active, head_out
    logger.info("Stopping all motors", "🛑")
    # Reset regardless of GPIO state - a stale True here (e.g. from a manual
    # head move that never got its own "off" tick) would wrongly defer tail
    # flaps on the next song even though the head is no longer actually out.
    head_out = False
    if not _gpio_active:
        return  # GPIO handle already closed, skip
    for pin in motor_pins:
        clear_pwm(pin)
        try:
            lgpio.gpio_write(h, pin, 0)
        except (lgpio.error, Exception):
            # Handle already closed or invalid - ignore during shutdown
            _gpio_active = False
            return


def cleanup_gpio():
    """Close GPIO chip handle to prevent memory corruption on shutdown."""
    global _gpio_active
    try:
        stop_all_motors()
        _gpio_active = False
        time.sleep(0.1)  # Give any pending timer threads a moment to check the flag

        # Free all GPIO pins before closing the chip handle
        for pin in motor_pins:
            with contextlib.suppress(lgpio.error, Exception):
                lgpio.gpio_free(h, pin)

        with contextlib.suppress(lgpio.error, Exception):
            lgpio.gpiochip_close(h)  # Handle might already be closed, ignore
        logger.info("GPIO cleanup complete", "✅")
    except Exception as e:
        logger.warning(f"GPIO cleanup error: {e}", "⚠️")


def is_motor_active():
    return any(_pin_is_active(pin) for pin in motor_pins)


def motor_watchdog():
    """Stop any single pin that stays active longer than WATCHDOG_TIMEOUT_SEC."""
    global _motor_watchdog_running
    _motor_watchdog_running = True

    # Track continuous-on start time per pin
    since_on = {pin: None for pin in motor_pins}

    while _motor_watchdog_running:
        now = time.time()
        for pin in motor_pins:
            active = _pin_is_active(pin)
            if active:
                if since_on[pin] is None:
                    since_on[pin] = now
                else:
                    if (now - since_on[pin]) >= WATCHDOG_TIMEOUT_SEC:
                        logger.warning(
                            f"Watchdog: pin {pin} active > {WATCHDOG_TIMEOUT_SEC}s → braking channel",
                            "⏱️",
                        )
                        _stop_channel(pin)
                        since_on[pin] = None
            else:
                since_on[pin] = None
        time.sleep(WATCHDOG_POLL_SEC)


def start_motor_watchdog():
    Thread(target=motor_watchdog, daemon=True).start()


def stop_motor_watchdog():
    global _motor_watchdog_running
    _motor_watchdog_running = False


# Ensure safe shutdown. cleanup_gpio frees lgpio claims; stopping motors alone can
# still leave pins busy during a fast service restart.
atexit.register(cleanup_gpio)
atexit.register(stop_motor_watchdog)
