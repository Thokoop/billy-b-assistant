"""Normal web application startup; never imported by emergency recovery."""

import os


def normal_app():
    from core.providers import initialize_providers

    from webconfig.app import create_app

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
        return redirect('/settings/software')

    @app.get('/__runtime_health')
    def runtime_health():
        return {'status': 'ok', 'revision': os.environ.get('BILLY_APPLIED_REVISION')}

    @app.context_processor
    def recovery_context():
        return {'recovery_notice': Recovery(INSTALL_ROOT).notice()}

    return app
