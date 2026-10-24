"""Stable service entry points; application imports belong only to workers."""

import os
import sys

from .paths import CODE_ROOT, INSTALL_ROOT


def run(entry, role):
    from .supervisor import monitor, serve, worker_environment

    if '--billy-worker' not in sys.argv:
        monitor(entry, role)
        return

    worker_environment()
    if os.environ['BILLY_WORKER_MODE'] == 'recovery':
        from .recovery import Recovery
        from .recovery_ui import create_recovery_app

        serve(create_recovery_app(Recovery(INSTALL_ROOT)), wait_for_start=False)
        return

    application = CODE_ROOT / 'application'
    startup = 'core/application.py' if role == 'core' else 'webconfig/startup.py'
    if not (application / startup).is_file():
        # Never silently load leftover code from an earlier checkout in root.
        raise RuntimeError(
            'Application startup files are missing; use software recovery'
        )
    sys.path.insert(0, str(application))
    if role == 'core':
        from core.application import run as run_assistant

        run_assistant()
    else:
        from webconfig.startup import normal_app

        serve(normal_app())
