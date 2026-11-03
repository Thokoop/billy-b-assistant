"""Settings offered by an extension device (see core.devices).

The fish has none. Another device adds its own settings to the Device page
(Settings → Device): its own template, inputs saved to .env through the normal
/save, and its own endpoints. It can also hide the fish's hardware fields it
does not have and fix .env settings (e.g. its audio devices). A broken panel is
logged and left out; the settings page stays.
"""

from __future__ import annotations

import re

from core.logger import logger


def install(app) -> None:
    panels = []
    try:
        from core import config as core_config
        from core.devices import webconfig_panel

        panel = webconfig_panel(core_config.BILLY_DEVICE)
        if panel:
            panels.append(_register(app, panel))
    except Exception as exc:
        logger.warning(f"Device settings panel left out: {exc}")
    app.config["DEVICE_PANELS"] = panels
    app.config["DEVICE_FIXED_SETTINGS"] = fixed_settings(app)
    app.config["DEVICE_AUDIO_TEST"] = next(
        (panel["audio_test"] for panel in panels if panel["audio_test"]), None
    )

    @app.context_processor
    def device_panel_context():
        panels = app.config.get("DEVICE_PANELS", [])
        hidden = set()
        for panel in panels:
            hidden.update(panel["hide"])
        return {
            "device_panels": panels,
            "device_hidden": hidden,
            "device_fixed_settings": app.config.get("DEVICE_FIXED_SETTINGS", {}),
            "device_audio_test": app.config.get("DEVICE_AUDIO_TEST"),
        }


def fixed_settings(app) -> dict[str, str]:
    """.env values the device fixes; /save keeps them whatever is posted."""
    fixed = {}
    for panel in app.config.get("DEVICE_PANELS", []):
        fixed.update(panel["fixed_settings"])
    return fixed


def _register(app, panel: dict) -> dict:
    from jinja2 import ChoiceLoader, FileSystemLoader

    from .routes import system

    slug = re.sub(r"[^a-z0-9-]+", "-", str(panel["id"]).lower()).strip("-")
    template_dir = panel.get("template_dir")
    if template_dir:
        app.jinja_loader = ChoiceLoader([
            app.jinja_loader,
            FileSystemLoader(template_dir),
        ])
    blueprint = panel.get("blueprint")
    if blueprint is not None:
        app.register_blueprint(blueprint)
    system.DEVICE_CONFIG_KEYS.update(panel.get("config_keys") or ())
    context = panel.get("context")
    return {
        "id": slug,
        "title": panel.get("title") or slug.title(),
        "icon": panel.get("icon") or "devices",
        "template": panel["template"],
        "hide": {str(item) for item in panel.get("hide") or ()},
        "fixed_settings": {
            str(key): str(value)
            for key, value in (panel.get("fixed_settings") or {}).items()
        },
        "audio_test": _audio_test(panel.get("audio_test")),
        "load_context": _safe(context),
    }


def _audio_test(spec):
    """The device's hooks around the Audio page's speaker and microphone tests.

    ``open(timeout)`` makes the audio path usable (e.g. rings the handset and
    waits until it is lifted) and returns (ok, message); ``close()`` releases
    it. ``hint`` is shown before a test, ``mic_label``/``speaker_label``
    replace the device names on the Audio page.
    """
    if not isinstance(spec, dict):
        return None
    return {
        "open": spec.get("open") if callable(spec.get("open")) else None,
        "close": spec.get("close") if callable(spec.get("close")) else None,
        "hint": str(spec.get("hint") or ""),
        "mic_label": str(spec.get("mic_label") or ""),
        "speaker_label": str(spec.get("speaker_label") or ""),
    }


def _safe(context):
    def load():
        if not callable(context):
            return {}
        try:
            return context() or {}
        except Exception as exc:
            logger.warning(f"Device settings panel context failed: {exc}")
            return {}

    return load
