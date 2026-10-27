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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from test_updates import candidate, queued

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


def wait_for(url, text, timeout=15, *, exact=False):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                body = response.read().decode()
            if (body == text) if exact else (text in body):
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
            wait_for(url, 'original', exact=True)
            pid = proc.pid
            updates.activate(root, record, updates.baseline(root), trial=True)
            wait_for(url, 'v2', exact=True)
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
            wait_for(url, 'original', exact=True)
            assert proc.pid == pid and proc.poll() is None
            assert (root / '.env').read_text() == f'FLASK_PORT={port}\n'
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def prepare_installation(tmp_path):
    root = tmp_path / 'installed'
    root.mkdir()
    shutil.copytree(
        ROOT / 'billy_runtime',
        root / 'billy_runtime',
        ignore=shutil.ignore_patterns('__pycache__'),
    )
    (root / 'webconfig').mkdir()
    entry = ENTRY.replace(
        "if '--billy-worker' not in sys.argv:",
        "from billy_runtime import updates\nupdates.resume = lambda *_: None\n"
        "if '--billy-worker' not in sys.argv:",
    ).replace(
        "return os.environ.get('BILLY_RELEASE_VERSION','original')",
        "return os.environ.get('BILLY_RELEASE_VERSION','original') + ':' + "
        "os.environ.get('REALTIME_AI_PROVIDER','openai')",
    )
    (root / 'webconfig/server.py').write_text(entry)
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'venv/bin/python').symlink_to(sys.executable)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    (root / '.env').write_text(f'FLASK_PORT={port}\nREALTIME_AI_PROVIDER=openai\n')
    (root / 'profiles').mkdir()
    (root / 'profiles/owner.ini').write_text('[USER_INFO]\nname=Owner\n')
    record = candidate(root)
    source, python = updates.resolve(record, root)
    python.unlink()
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    shutil.copytree(root / 'billy_runtime', source / 'billy_runtime')
    (source / 'webconfig').mkdir()
    (source / 'webconfig/server.py').write_text(entry)
    return root, record, source, f'http://127.0.0.1:{port}/settings'


@contextmanager
def running_supervisor(root, tmp_path):
    with (tmp_path / 'process.log').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(root / 'webconfig/server.py')],
            stdout=log,
            stderr=log,
            env={k: v for k, v in os.environ.items() if not k.startswith('BILLY_')},
        )
        try:
            yield proc
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def test_release_owns_settings_and_incompatible_rollback_preserves_working_release(
    tmp_path,
    monkeypatch,
):
    from billy_runtime.configuration import save_env
    from billy_runtime.recovery import Recovery

    root, record, source, url = prepare_installation(tmp_path)
    schema = root / 'billy_runtime/configuration_schema.py'
    # The fixed root application does not know a later release's new provider.
    schema.write_text(
        schema.read_text().replace("'openai', 'xai', 'gpt-live'", "'openai', 'xai'")
    )
    original = (root / '.env').read_text()
    newer = original.replace(
        'REALTIME_AI_PROVIDER=openai', 'REALTIME_AI_PROVIDER=gpt-live'
    )
    store = Recovery(root)
    monkeypatch.setattr(updates, 'command', Mock())

    with running_supervisor(root, tmp_path) as proc:
        wait_for(url, 'original:openai', exact=True)
        # A real worker handoff, with the supervisor remaining in the old root.
        updates.activate(root, record, updates.baseline(root), trial=False)
        wait_for(url, 'v2:openai', exact=True)
        save_env(root / '.env', newer)
        store.state(retry='new-provider')
        wait_for(url, 'v2:gpt-live', exact=True)
        assert not list(store.directory.glob('corrupt-*'))

        def incompatible(*_):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if 'REALTIME_AI_PROVIDER at line 2' in store.notice()['message']:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Worker configuration error was not reported')
            assert store.notice()['kind'] == 'configuration'
            wait_for(url, 'Billy recovery')
            assert (root / '.env').read_text() == newer
            return False

        # Restoring an older release with incompatible settings fails its startup
        # checks and returns to the working release without resetting those settings.
        queued(root, action='rollback')
        monkeypatch.setattr(updates, 'wait_healthy', incompatible)
        updates.execute(root, 'job1')
        assert updates.job(root)['state'] == 'rolled_back'
        wait_for(url, 'v2:gpt-live', exact=True)
        assert (root / '.env').read_text() == newer
        assert (root / 'current-source').resolve() == source

        # Compatible settings still allow a normal rollback to the root baseline.
        save_env(root / '.env', original)
        store.state(retry='compatible-provider')
        wait_for(url, 'v2:openai', exact=True)
        queued(root, action='rollback')
        monkeypatch.setattr(
            updates,
            'wait_healthy',
            lambda *_: bool(wait_for(url, 'original:openai', exact=True)),
        )
        updates.execute(root, 'job1')
        assert updates.job(root)['state'] == 'complete'
        assert (root / 'current-source').resolve() == root
        assert (root / '.env').read_text() == original
        assert (root / 'profiles/owner.ini').read_text() == '[USER_INFO]\nname=Owner\n'
        assert proc.poll() is None


def test_broken_root_schema_does_not_break_release_or_emergency_ui(tmp_path):
    root, record, source, url = prepare_installation(tmp_path)
    (root / 'billy_runtime/configuration_schema.py').write_text(
        'raise RuntimeError("Root application schema is broken")\n'
    )
    updates.activate(root, record, updates.baseline(root), trial=True)
    before = (root / '.env').read_bytes()
    with running_supervisor(root, tmp_path) as proc:
        wait_for(url, 'v2:openai', exact=True)
        # Both application schemas may be broken; recovery must still start.
        (source / 'billy_runtime/configuration_schema.py').write_text(
            'invalid python syntax!\n'
        )
        updates.activate(root, record, updates.baseline(root), trial=True)
        wait_for(url, 'Billy recovery')
        assert (root / '.env').read_bytes() == before
        assert proc.poll() is None
