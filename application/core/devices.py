"""Optional extension devices that drive the assistant from outside.

The built-in device is the fish (``BILLY_DEVICE=fish``), which needs nothing
from this module. Any other ``BILLY_DEVICE`` value names an extension device,
resolved in this order:

1. an installed entry point in the ``billy.devices`` group with that name;
2. a ``package.module:attribute`` import path;
3. a plain name (``BILLY_DEVICE=<name>``): the ``DEVICE`` attribute of the
   ``private.<name>`` package, for devices kept out of this repository in
   ``application/private/<name>``.

Such a device folder may also ship a system component (packages and a setup
script, ``setup/component.json``) that Billy's system-component installer runs;
see billy_runtime/system_packages.py.

The resolved object is either a device instance or a zero-argument factory
(such as a class) that returns one. A device needs two methods:

    start(controls)   called once the assistant is ready; must not block
    stop()            called on shutdown; must not raise

Optionally:

    model                 the product name shown in Home Assistant
    webconfig_panel()     (on the class or module) a settings panel for the
                          web UI; see webconfig_panel() below
    handle_say(opening)   deliver an MQTT "say" its own way (for example ring
                          first); return True when handled. ``opening`` has
                          the keys of button.request_session_start's opening.

``controls`` is a :class:`DeviceControls`. A device keeps its own threads; the
assistant keeps working with its button, wake word and MQTT triggers as
configured.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from importlib import metadata

from .logger import logger


ENTRY_POINT_GROUP = "billy.devices"

_active_device = None


class DeviceControls:
    """What an extension device may do with the assistant."""

    def request_session_start(
        self,
        source: str,
        opening: dict | None = None,
        situation: str | None = None,
        persona: str | None = None,
    ) -> bool:
        """Start a conversation; see button.request_session_start."""
        from . import button

        return button.request_session_start(
            source, opening=opening, situation=situation, persona=persona
        )

    def request_session_stop(self, source: str) -> bool:
        from . import button

        return button.request_session_stop(source)

    def subscribe(self, event: str, callback: Callable[..., object]):
        from . import session_events

        return session_events.subscribe(event, callback)

    def audio_takeover(self) -> str | None:
        """What owns Billy's audio instead of the assistant right now.

        "call" or "media" while a Bluetooth session has taken over the speaker
        and microphone (it has priority: it stops a conversation and blocks new
        ones), None when the assistant has them.
        """
        from .bluetooth import mode

        state, route, _ = mode.snapshot()
        if state == mode.IDLE:
            return None
        return "call" if route and route[1] else "media"

    def bluetooth_call(self) -> str | None:
        """A paired phone's call: "incoming" (ringing), "active" or None.

        Known from the phone's call state, also while no call audio is there
        yet: a ringing call does not take Billy's audio until it is answered,
        and a call answered elsewhere may still be on its way.
        """
        from .bluetooth import controls

        states = set(controls.call_states() or ())
        if states & controls.RINGING_CALL_STATES:
            return "incoming"
        if states & controls.ACTIVE_CALL_STATES:
            return "active"
        return None

    def bluetooth_phone_connected(self) -> bool:
        """True when a paired phone is connected and can make calls."""
        from .bluetooth import controls

        return controls.phone_connected()

    def bluetooth_dial(self, number: str) -> bool:
        """Make the paired phone call ``number``; its audio then takes over."""
        from .bluetooth import controls

        return controls.dial(number)

    def answer_takeover_call(self) -> bool:
        """Answer the ringing call of a paired phone."""
        from .bluetooth import controls

        return controls.answer_call()

    def end_audio_takeover(self) -> bool:
        """Hang up the Bluetooth call, or pause and leave Bluetooth media."""
        from .bluetooth import controls

        return controls.end_takeover()

    def mqtt_topic(self, topic: str, handler, discovery: tuple | list | None = None):
        """Handle an MQTT topic on Billy's connection; see mqtt.register_topic."""
        from . import mqtt

        return mqtt.register_topic(topic, handler, discovery=discovery)

    def parse_say_payload(self, payload: str) -> dict:
        """Read a payload the way ``billy/say`` does, as an ``opening``."""
        from .mqtt import _parse_say_payload

        text, interactive = _parse_say_payload(payload)
        kind = "literal"
        if text.startswith("{{") and text.endswith("}}"):
            kind, text = "prompt", text[2:-2].strip()
        return {"text": text, "kind": kind, "interactive": interactive}


def _resolve(name: str):
    try:
        entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - broken distribution metadata
        entry_points = ()
    for entry_point in entry_points:
        if entry_point.name == name:
            return entry_point.load()
    if ":" in name:
        module_name, _, attribute = name.partition(":")
        target = importlib.import_module(module_name)
        for part in attribute.split("."):
            target = getattr(target, part)
        return target
    if name.replace("_", "").isalnum():
        try:
            package = importlib.import_module(f"private.{name}")
        except ModuleNotFoundError as exc:
            if exc.name not in {"private", f"private.{name}"}:
                raise
        else:
            target = getattr(package, "DEVICE", None)
            if target is not None:
                return target
    raise LookupError(
        f"No '{ENTRY_POINT_GROUP}' entry point named '{name}', no "
        f"private.{name} device and not a 'module:attribute' path"
    )


def webconfig_panel(name: str) -> dict | None:
    """The settings the configured device adds to the web UI's Device page.

    Called in the web UI process, which never starts the device: the panel
    comes from a ``webconfig_panel()`` on the resolved class or object.
    Returns a dict with:

        id            short slug (/settings/<id> opens the Device page)
        title, icon   section title and Material icon name
        template      Jinja template rendered on the Device page
        template_dir  folder holding that template
        config_keys   .env keys the panel's inputs save through /save
        context       optional callable returning extra template variables
        blueprint     optional Flask blueprint for the panel's own endpoints
        hide          optional settings the device lacks: "motors" (also tail
                      flap), "led", "camera", "audio-devices", "mic-gain",
                      "wakeword" (the Wake word page)
        fixed_settings  optional {.env key: value} the device fixes; kept on
                      every save (e.g. its audio devices, no status LED)
        audio_test    optional hooks for the Audio page's speaker/microphone
                      tests: {"open": f(timeout) -> (ok, message), "close": f(),
                      "hint", "mic_label", "speaker_label"}
    """
    if not name or name == "fish":
        return None
    try:
        target = _resolve(name)
        provider = getattr(target, "webconfig_panel", None)
        panel = provider() if callable(provider) else None
    except Exception as exc:
        logger.warning(f"Device settings panel unavailable: {exc}")
        return None
    if not isinstance(panel, dict) or not panel.get("id") or not panel.get("template"):
        return None
    return panel


def load_device(name: str):
    """Return the device for ``name``, or None for the fish or on failure.

    A device that cannot be loaded is logged and skipped rather than stopping
    the assistant, so the web configuration stays reachable to fix it.
    """
    if not name or name == "fish":
        return None
    try:
        target = _resolve(name)
        is_factory = isinstance(target, type) or (
            callable(target) and not hasattr(target, "start")
        )
        device = target() if is_factory else target
        if not callable(getattr(device, "start", None)):
            raise TypeError(f"{name} does not provide start(controls)")
    except Exception as exc:
        logger.error(f"Could not load device '{name}': {exc}")
        return None
    logger.info(f"Device profile: {name}", "🔌")
    return device


def start_device(device) -> bool:
    if device is None:
        return False
    try:
        device.start(DeviceControls())
    except Exception as exc:
        logger.error(f"Device failed to start: {exc}")
        return False
    return True


def stop_device(device) -> None:
    if device is None:
        return
    stop = getattr(device, "stop", None)
    if not callable(stop):
        return
    try:
        stop()
    except Exception as exc:
        logger.warning(f"Device failed to stop cleanly: {exc}")


def load_active_device(name: str):
    """Load the configured device; the fish leaves no active device."""
    global _active_device
    _active_device = load_device(name)
    return _active_device


def active_device_model() -> str | None:
    model = getattr(_active_device, "model", None)
    return str(model) if model else None


def device_handles_say(opening: dict) -> bool:
    """Let the active device deliver a "say"; False means Billy says it."""
    handler = getattr(_active_device, "handle_say", None)
    if not callable(handler):
        return False
    try:
        return bool(handler(opening))
    except Exception as exc:
        logger.error(f"Device failed to handle SAY: {exc}")
        return False


def start_active_device() -> bool:
    return start_device(_active_device)


def stop_active_device() -> None:
    global _active_device
    device, _active_device = _active_device, None
    stop_device(device)
