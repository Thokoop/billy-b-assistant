import os
import sys
import threading
import time

from flask import Flask


try:
    from .sock_compat import SOCK_AVAILABLE
except ModuleNotFoundError:
    # Older/stale deployments may not include sock_compat yet.
    SOCK_AVAILABLE = False


def create_app() -> Flask:
    # Ensure project root on path for core imports
    sys.path.append(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )

    base_dir = os.path.dirname(__file__)
    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "..", "templates"),
        static_folder=os.path.join(base_dir, "..", "static"),
    )

    # Late imports to avoid circulars
    from . import websocket
    from .routes.audio import apply_configured_mic_gain
    from .routes.audio import bp as audio_bp
    from .routes.knowledge import knowledge_bp
    from .routes.misc import bp as misc_bp
    from .routes.persona import bp as persona_bp
    from .routes.profiles import profiles_bp
    from .routes.songs import songs_bp
    from .routes.system import bp as system_bp
    from .state import bootstrap_versions_and_release_note

    # Register blueprints
    app.register_blueprint(system_bp)
    app.register_blueprint(persona_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(audio_bp)
    app.register_blueprint(knowledge_bp)
    app.register_blueprint(misc_bp)
    app.register_blueprint(songs_bp)

    # Register WebSocket routes when flask_sock is available.
    if SOCK_AVAILABLE:
        websocket.sock.init_app(app)
        app.register_blueprint(websocket.bp)
    else:
        app.logger.warning("flask_sock is unavailable; WebSocket updates are disabled.")

    def refresh_version_metadata():
        # Let Flask bind its port before doing any external network work.
        time.sleep(0.25)
        started = time.monotonic()
        bootstrap_versions_and_release_note()
        app.logger.info(
            "Background version metadata refresh completed in %.2fs",
            time.monotonic() - started,
        )

    def apply_mic_gain_after_startup():
        # ALSA probing is local but can still pause while devices settle. It must
        # not keep the web interface from accepting connections.
        time.sleep(0.25)
        started = time.monotonic()
        mic_gain_result = apply_configured_mic_gain()
        if mic_gain_result.get("status") == "updated":
            app.logger.info(
                "Applied configured mic gain: %(old)s -> %(gain)s",
                mic_gain_result,
            )
        elif mic_gain_result.get("status") not in {"kept"}:
            app.logger.info("Configured mic gain skipped: %s", mic_gain_result)
        app.logger.info(
            "Background microphone gain check completed in %.2fs",
            time.monotonic() - started,
        )

    threading.Thread(
        target=refresh_version_metadata,
        name="webconfig-version-refresh",
        daemon=True,
    ).start()
    threading.Thread(
        target=apply_mic_gain_after_startup,
        name="webconfig-mic-gain",
        daemon=True,
    ).start()

    return app
