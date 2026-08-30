"""Single-pixel WS2812B status LED controller."""

from __future__ import annotations

import contextlib
import math
import threading
import time
from pathlib import Path

from . import config
from .logger import logger


try:
    from rpi_ws281x import Color, PixelStrip, ws

    _ws281x_available = True
except ImportError:
    Color = None
    PixelStrip = None
    ws = None
    _ws281x_available = False

try:
    import board
    import neopixel_spi

    _spi_neopixel_available = True
except ImportError:
    board = None
    neopixel_spi = None
    _spi_neopixel_available = False


ColorTuple = tuple[int, int, int]


def _is_raspberry_pi_5() -> bool:
    """Detect Raspberry Pi 5 from the device model when available."""
    model_path = Path("/proc/device-tree/model")
    try:
        model = model_path.read_text(encoding="utf-8", errors="ignore").strip("\x00 \n")
    except Exception:
        return False
    return "Raspberry Pi 5" in model


class StatusLed:
    """Drive a single WS2812B LED with simple state animations."""

    _STATE_CONFIG: dict[str, dict[str, object]] = {
        "starting": {"mode": "pulse", "color": (0, 96, 255), "period": 1.4},
        "idle": {"mode": "pulse", "color": (0, 32, 12), "period": 2.8},
        "listening": {"mode": "solid", "color": (0, 180, 24)},
        "speaking": {"mode": "pulse", "color": (255, 110, 0), "period": 0.9},
        "interrupted": {"mode": "blink", "color": (255, 28, 0), "period": 0.12},
        "playing_song": {"mode": "rainbow", "period": 1.2},
        "error": {"mode": "blink", "color": (255, 0, 0), "period": 0.45},
        "stopping": {"mode": "blink", "color": (255, 48, 0), "period": 0.8},
        "off": {"mode": "solid", "color": (0, 0, 0)},
    }

    def __init__(self):
        self.enabled = config.STATUS_LED_ENABLED and not config.MOCKFISH
        self._strip = None
        self._animation_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._state = "off"
        self._transient_state: str | None = None
        self._transient_until = 0.0
        self._timeout_progress: float | None = None
        self._timeout_started_at = 0.0
        self._song_color: ColorTuple | None = None
        self._song_led_off = False
        self._initialized = False
        self._brightness = max(0.0, min(config.STATUS_LED_BRIGHTNESS, 1.0))
        self._backend: str | None = None
        self._last_output_color: ColorTuple | None = None

    def initialize(self):
        """Initialize the hardware strip and start the animation worker."""
        if not self.enabled or self._initialized:
            return

        backend = str(config.STATUS_LED_BACKEND).strip().lower()
        if backend not in {"auto", "pwm", "spi"}:
            logger.warning(
                f"Unknown STATUS_LED_BACKEND '{config.STATUS_LED_BACKEND}', using auto.",
                "💡",
            )
            backend = "auto"

        init_error: Exception | None = None
        backend_attempts = self._resolve_backend_attempts(backend)

        for backend_name in backend_attempts:
            try:
                if backend_name == "pwm":
                    self._initialize_pwm()
                else:
                    self._initialize_spi()

                self._initialized = True
                self._backend = backend_name
                self._set_pixels((0, 0, 0))
                self._animation_thread = threading.Thread(
                    target=self._run_animation_loop,
                    daemon=True,
                )
                self._animation_thread.start()
                logger.info(
                    f"Status LED initialized with {backend_name.upper()} backend.",
                    "💡",
                )
                return
            except Exception as e:
                init_error = e
                logger.warning(
                    f"Status LED {backend_name.upper()} init failed: {e}",
                    "💡",
                )
                self._strip = None
                self._backend = None
                self._initialized = False

        if init_error:
            logger.warning(
                f"Status LED initialization failed after trying {', '.join(backend_attempts)}: {init_error}",
                "💡",
            )

    def set_state(self, state: str):
        """Set the current LED state."""
        if state not in self._STATE_CONFIG:
            logger.warning(f"Unknown status LED state '{state}', ignoring.", "💡")
            return
        with self._lock:
            self._state = state
            # A real state transition ends any previous listening countdown.
            # The timeout checker will explicitly start a new one when needed.
            self._timeout_progress = None
            self._timeout_started_at = 0.0

    def get_state(self) -> str:
        """Return the current logical LED state."""
        with self._lock:
            return self._state

    def flash_state(self, state: str, duration_seconds: float = 0.45):
        """Temporarily show a state, then reveal the latest logical state."""
        if state not in self._STATE_CONFIG:
            logger.warning(f"Unknown status LED state '{state}', ignoring.", "💡")
            return
        duration = max(0.0, float(duration_seconds))
        if duration <= 0.0:
            return
        with self._lock:
            self._transient_state = state
            self._transient_until = time.monotonic() + duration

    def show_interruption(self, duration_seconds: float = 0.75):
        """Atomically queue listening green beneath an interruption flash."""
        duration = max(0.0, float(duration_seconds))
        with self._lock:
            self._state = "listening"
            self._timeout_progress = None
            self._timeout_started_at = 0.0
            if duration > 0.0:
                self._transient_state = "interrupted"
                self._transient_until = time.monotonic() + duration

    def set_timeout_progress(self, progress: float):
        """Show listening timeout progress from green (0) to red (1)."""
        with self._lock:
            if self._timeout_progress is None:
                self._timeout_started_at = time.monotonic()
            self._timeout_progress = max(0.0, min(1.0, float(progress)))

    def clear_timeout_progress(self):
        """Return to the normal animation for the current logical state."""
        with self._lock:
            self._timeout_progress = None
            self._timeout_started_at = 0.0

    def set_song_color(self, color: ColorTuple | None):
        """Set (or clear) a custom pulse color for the "playing_song" state.

        When set, "playing_song" pulses this color instead of the default
        rainbow animation. Pass None to fall back to rainbow.
        """
        with self._lock:
            self._song_color = color

    def set_song_led_off(self, off: bool):
        """Force the "playing_song" state to stay dark for this song.

        Takes priority over set_song_color()'s solid pulse and the default
        rainbow animation alike - a third option alongside them, not a
        variant of either.
        """
        with self._lock:
            self._song_led_off = bool(off)

    @staticmethod
    def parse_hex_color(value: str | None) -> ColorTuple | None:
        """Parse a '#rrggbb' string into an (r, g, b) tuple, or None if empty/invalid."""
        if not value:
            return None
        value = value.strip().lstrip("#")
        if len(value) != 6:
            return None
        try:
            return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
        except ValueError:
            return None

    def _animation_state(self, now: float) -> str:
        """Return a transient animation state without replacing logical state."""
        with self._lock:
            if self._transient_state and now < self._transient_until:
                return self._transient_state
            self._transient_state = None
            self._transient_until = 0.0
            return self._state

    def _animation_config(self, now: float) -> dict[str, object]:
        """Resolve transient and timeout overlays for the animation worker."""
        state = self._animation_state(now)
        with self._lock:
            timeout_progress = self._timeout_progress
            timeout_started_at = self._timeout_started_at
            song_color = self._song_color
            song_led_off = self._song_led_off

        if state == "listening" and timeout_progress is not None:
            # Stay solid while listening; the color blend alone (green through
            # amber to red) conveys remaining time, so blinking isn't needed.
            return {
                "mode": "solid",
                "color": self._timeout_color(timeout_progress),
            }
        if state == "playing_song" and song_led_off:
            return self._STATE_CONFIG["off"]
        if state == "playing_song" and song_color is not None:
            # A song-specific color pulses instead of the default rainbow.
            return {"mode": "pulse", "color": song_color, "period": 1.2}
        return self._STATE_CONFIG.get(state, self._STATE_CONFIG["off"])

    @staticmethod
    def _timeout_color(progress: float) -> ColorTuple:
        """Blend timeout progress from green through amber to red."""
        progress = max(0.0, min(1.0, float(progress)))
        if progress <= 0.5:
            blend = progress / 0.5
            return (
                round(255 * blend),
                round(180 + (20 * blend)),
                round(24 * (1.0 - blend)),
            )
        blend = (progress - 0.5) / 0.5
        return (255, round(200 * (1.0 - blend)), 0)

    def cleanup(self):
        """Stop animations and turn the LED off."""
        self._stop_event.set()
        if self._animation_thread and self._animation_thread.is_alive():
            self._animation_thread.join(timeout=1.0)
        if self._initialized:
            self._set_pixels((0, 0, 0))
        strip = self._strip
        self._strip = None
        self._initialized = False
        self._backend = None
        self._animation_thread = None
        self._last_output_color = None
        self._stop_event = threading.Event()
        with self._lock:
            self._transient_state = None
            self._transient_until = 0.0
            self._timeout_progress = None
            self._timeout_started_at = 0.0
            self._song_color = None
            self._song_led_off = False
        if strip and hasattr(strip, "deinit"):
            with contextlib.suppress(Exception):
                strip.deinit()

    def _resolve_backend_attempts(self, backend: str) -> list[str]:
        if backend == "pwm":
            return ["pwm"]
        if backend == "spi":
            return ["spi"]
        if _is_raspberry_pi_5():
            return ["spi", "pwm"]
        return ["pwm", "spi"]

    def _initialize_pwm(self):
        if not _ws281x_available:
            raise RuntimeError("rpi_ws281x is not installed")

        self._strip = PixelStrip(
            config.STATUS_LED_COUNT,
            config.STATUS_LED_PIN,
            800000,
            config.STATUS_LED_DMA_CHANNEL,
            False,
            255,
            config.STATUS_LED_PWM_CHANNEL,
            ws.WS2811_STRIP_GRB,
        )
        self._strip.begin()

    def _initialize_spi(self):
        if not _spi_neopixel_available:
            raise RuntimeError(
                "SPI NeoPixel support is not installed "
                "(requires adafruit-blinka and adafruit-circuitpython-neopixel-spi)"
            )

        spi = board.SPI()
        self._strip = neopixel_spi.NeoPixel_SPI(
            spi,
            config.STATUS_LED_COUNT,
            auto_write=False,
            brightness=1.0,
        )

    def _run_animation_loop(self):
        while not self._stop_event.is_set():
            now = time.monotonic()
            config_for_state = self._animation_config(now)
            mode = str(config_for_state["mode"])
            color = config_for_state.get("color", (0, 0, 0))
            period = float(config_for_state.get("period", 1.0))
            if mode == "solid":
                self._set_pixels(color)
                self._stop_event.wait(0.1)
            elif mode == "blink":
                phase_origin = float(config_for_state.get("phase_origin", 0.0))
                phase_time = now - phase_origin if phase_origin > 0.0 else now
                phase_on = (phase_time % period) < (period / 2.0)
                self._set_pixels(color if phase_on else (0, 0, 0))
                self._stop_event.wait(0.08)
            elif mode == "pulse":
                wave = (math.sin((2 * math.pi * now) / period) + 1.0) / 2.0
                scale = 0.18 + (0.82 * wave)
                self._set_pixels(self._scale_color(color, scale))
                self._stop_event.wait(0.02)
            elif mode == "rainbow":
                position = int((now * 255 / period) % 255)
                self._set_pixels(self._wheel(position))
                self._stop_event.wait(0.02)
            else:
                self._set_pixels((0, 0, 0))
                self._stop_event.wait(0.1)

    def _set_pixels(self, color: ColorTuple):
        if not self._initialized or not self._strip:
            return

        scaled = self._scale_color(color, self._brightness)
        if scaled == self._last_output_color:
            return
        if self._backend == "pwm":
            packed = Color(*scaled)
            for index in range(config.STATUS_LED_COUNT):
                self._strip.setPixelColor(index, packed)
            self._strip.show()
            self._last_output_color = scaled
            return

        for index in range(config.STATUS_LED_COUNT):
            self._strip[index] = scaled
        self._strip.show()
        self._last_output_color = scaled

    @staticmethod
    def _scale_color(color: ColorTuple, scale: float) -> ColorTuple:
        return tuple(max(0, min(255, int(channel * scale))) for channel in color)

    @staticmethod
    def _wheel(position: int) -> ColorTuple:
        position = 255 - (position % 256)
        if position < 85:
            return 255 - position * 3, 0, position * 3
        if position < 170:
            position -= 85
            return 0, position * 3, 255 - position * 3
        position -= 170
        return position * 3, 255 - position * 3, 0


status_led = StatusLed()


def initialize_status_led():
    """Initialize the shared status LED controller."""
    status_led.initialize()


def set_status_led_state(state: str):
    """Set the shared status LED to a named state."""
    status_led.set_state(state)


def get_status_led_state() -> str:
    """Return the shared status LED state."""
    return status_led.get_state()


def flash_status_led_state(state: str, duration_seconds: float = 0.75):
    """Show a temporary LED animation without delaying later state updates."""
    status_led.flash_state(state, duration_seconds)


def show_status_led_interruption(duration_seconds: float = 0.75):
    """Flash a distinct interruption signal, then settle on listening green."""
    # Session/MQTT state can still replace the queued fallback if Billy
    # legitimately enters another state meanwhile.
    status_led.show_interruption(duration_seconds)


def set_status_led_timeout_progress(progress: float):
    """Show normalized microphone timeout progress on the shared LED."""
    status_led.set_timeout_progress(progress)


def clear_status_led_timeout_progress():
    """Clear microphone timeout progress from the shared LED."""
    status_led.clear_timeout_progress()


def set_status_led_song_color(color: ColorTuple | None):
    """Set (or clear) the shared status LED's per-song pulse color."""
    status_led.set_song_color(color)


def set_status_led_song_off(off: bool):
    """Force (or release) the shared status LED staying dark for this song."""
    status_led.set_song_led_off(off)


def cleanup_status_led():
    """Stop the shared status LED controller."""
    status_led.cleanup()


def run_status_led_test(duration_seconds: float = 4.5) -> tuple[bool, str]:
    """Run a short standalone LED test sequence."""
    if not config.STATUS_LED_ENABLED:
        return False, "Status LED is disabled in settings."
    if config.MOCKFISH:
        return False, "Status LED test is unavailable in mock mode."

    try:
        status_led.initialize()
        if not status_led._initialized:
            return False, "Status LED could not be initialized."

        sequence: list[tuple[str, float]] = [
            ("starting", 0.9),
            ("listening", 0.9),
            ("speaking", 1.1),
            ("error", 0.8),
        ]
        remaining = max(0.0, float(duration_seconds)) - sum(
            delay for _, delay in sequence
        )
        if remaining > 0:
            sequence.append(("playing_song", remaining))

        for state, delay in sequence:
            status_led.set_state(state)
            time.sleep(delay)

        status_led.set_state("off")
        return True, "Status LED test completed."
    except Exception as e:
        return False, str(e)
    finally:
        status_led.cleanup()
