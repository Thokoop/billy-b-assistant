"""Real process handoff on one listen socket, without GPIO, apt or systemd."""

import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from test_updates import candidate

from billy_runtime import updates


ROOT = Path(__file__).resolve().parents[2]

ENTRY = '''import os, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from billy_runtime.supervisor import monitor, worker_environment, serve
from billy_runtime.recovery import Recovery
from billy_runtime.paths import INSTALL_ROOT
from billy_runtime.recovery_ui import create_recovery_app
if '--billy-worker' not in sys.argv:
    monitor(Path(__file__), 'web')
else:
    worker_environment()
    if os.environ['BILLY_WORKER_MODE']=='recovery':
        serve(create_recovery_app(Recovery(INSTALL_ROOT)),wait_for_start=False)
    else:
        if (ROOT/'broken').exists(): raise RuntimeError('candidate intentionally broken')
        from flask import Flask
        from flask_sock import Sock
        app=Flask('candidate')
        sock=Sock(app)
        @sock.route('/ws')
        def ws(socket): socket.send(socket.receive())
        @app.get('/__runtime_health')
        def health(): return {'revision': os.environ['BILLY_APPLIED_REVISION']}
        @app.get('/settings')
        def settings(): return os.environ.get('BILLY_RELEASE_VERSION','original')
        serve(app)
'''


def wait_for(url, text, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read().decode()
            if text in body:
                return body
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise AssertionError('No response containing ' + text)


def test_real_handoff_broken_candidate_and_rollback_keep_rescue_and_socket(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    shutil.copytree(
        ROOT / 'billy_runtime',
        root / 'billy_runtime',
        ignore=shutil.ignore_patterns('__pycache__'),
    )
    (root / 'webconfig').mkdir()
    (root / 'webconfig/server.py').write_text(ENTRY)
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'venv/bin/python').symlink_to(sys.executable)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    (root / '.env').write_text(f'FLASK_PORT={port}\n')
    record = candidate(root)
    source, python = updates.resolve(record, root)
    python.unlink()
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    shutil.copytree(root / 'billy_runtime', source / 'billy_runtime')
    (source / 'webconfig').mkdir()
    (source / 'webconfig/server.py').write_text(ENTRY)
    with (tmp_path / 'process.log').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(root / 'webconfig/server.py')],
            stdout=log,
            stderr=log,
            env={k: v for k, v in os.environ.items() if not k.startswith('BILLY_')},
        )
        url = f'http://127.0.0.1:{port}/settings'
        try:
            wait_for(url, 'original')
            pid = proc.pid
            updates.activate(root, record, updates.baseline(root), trial=True)
            wait_for(url, 'v2')
            assert proc.poll() is None and proc.pid == pid
            import asyncio

            import websockets

            async def echo():
                async with websockets.connect(f'ws://127.0.0.1:{port}/ws') as ws:
                    await ws.send('connected after handoff')
                    assert await ws.recv() == 'connected after handoff'

            asyncio.run(echo())
            # Kill the selected worker via a new generation that fails on import.
            (source / 'broken').touch()
            updates.activate(root, record, updates.baseline(root), trial=True)
            wait_for(url, 'Billy recovery')
            assert proc.poll() is None
            updates.activate(root, updates.baseline(root), record, trial=False)
            wait_for(url, 'original')
            assert proc.pid == pid and proc.poll() is None
            assert (root / '.env').read_text() == f'FLASK_PORT={port}\n'
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
