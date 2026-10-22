"""Disposable actual-process fixture. No real Billy hardware/services."""

import os
import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from flask import Flask

from billy_runtime import supervisor
from billy_runtime.recovery import Recovery
from billy_runtime.recovery_ui import create_recovery_app
from billy_runtime.worker import notify


root = Path(os.environ['RECOVERY_TEST_ROOT'])
if '--billy-worker' not in sys.argv:
    supervisor.START_TIMEOUT = 2
    supervisor.PROBE_INTERVAL = 0.3
    supervisor.CORE_TIMEOUT = 2
    supervisor.monitor(
        Path(__file__), os.environ.get('RECOVERY_TEST_ROLE', 'web'), root=root
    )
else:
    content = supervisor.worker_environment()
    if os.environ['BILLY_WORKER_MODE'] == 'core':
        notify('ready')
        if (root / 'mode').read_text() == 'core-loop-hang':
            notify('loop-start:test')
            while True:
                notify('progress')
                time.sleep(0.1)
        else:
            time.sleep(100)
    elif os.environ['BILLY_WORKER_MODE'] == 'recovery':
        supervisor.serve(create_recovery_app(Recovery(root)), wait_for_start=False)
    else:
        mode = (root / 'mode').read_text()
        if mode == 'crash':
            raise RuntimeError('Simulated broken core import')
        if mode == 'startup-hang':
            time.sleep(100)
        app = Flask('test')
        from flask_sock import Sock
        from werkzeug.middleware.dispatcher import DispatcherMiddleware

        sock = Sock(app)

        @sock.route('/ws')
        def websocket(ws):
            data = ws.receive(timeout=2)
            ws.send('echo:' + data)

        app.wsgi_app = DispatcherMiddleware(
            app.wsgi_app, {'/repair': create_recovery_app(Recovery(root))}
        )

        @app.get('/__runtime_health')
        def health():
            if (root / 'mode').read_text() == 'runtime-hang':
                time.sleep(100)
            return {'status': 'ok', 'revision': os.environ['BILLY_APPLIED_REVISION']}

        @app.get('/settings')
        def settings():
            return 'normal settings'

        supervisor.serve(app)
