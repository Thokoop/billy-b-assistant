"""Supervised web UI with a hardware-independent recovery mode."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Prefer webconfig/app over any legacy app/ directory in the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def normal_app():
    from app import create_app

    from core.providers import initialize_providers

    initialize_providers()
    from core.profile_manager import user_manager

    user_manager.load_default_user()
    app = create_app()
    from flask import redirect

    from billy_runtime.paths import INSTALL_ROOT
    from billy_runtime.recovery import Recovery

    # Normal recovery lives in Software settings. The independent emergency
    # app is launched only by the supervisor when the normal app cannot run.
    @app.get('/repair/')
    @app.get('/repair/recovery')
    def repair_settings():
        return redirect('/settings#section-software-version')

    @app.get('/__runtime_health')
    def runtime_health():
        return {'status': 'ok', 'revision': os.environ.get('BILLY_APPLIED_REVISION')}

    @app.context_processor
    def recovery_context():
        return {'recovery_notice': Recovery(INSTALL_ROOT).notice()}

    return app


if __name__ == '__main__':
    from billy_runtime.supervisor import monitor, serve, worker_environment

    if '--billy-worker' not in sys.argv:
        monitor(Path(__file__), 'web')
    else:
        worker_environment()
        if os.environ['BILLY_WORKER_MODE'] == 'recovery':
            from billy_runtime.paths import INSTALL_ROOT
            from billy_runtime.recovery import Recovery
            from billy_runtime.recovery_ui import create_recovery_app

            serve(create_recovery_app(Recovery(INSTALL_ROOT)), wait_for_start=False)
        else:
            serve(normal_app())
