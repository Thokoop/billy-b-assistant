import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]


def wait_for(url, text, timeout=20, *, exact=False):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                body = r.read().decode()
            matches = body == text if exact else text in body
            if matches:
                return body
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.15)
    raise AssertionError('Did not receive ' + text)


def test_normal_settings_readiness_waits_past_recovery_page(monkeypatch):
    from io import BytesIO

    from billy_runtime.recovery_ui import PAGE

    # Recovery links to "normal settings" before the normal worker is ready.
    assert 'normal settings' in PAGE
    responses = iter([PAGE.encode(), b'normal settings'])
    monkeypatch.setattr(
        urllib.request, 'urlopen', lambda *args, **kwargs: BytesIO(next(responses))
    )
    monkeypatch.setattr(time, 'sleep', lambda _: None)

    assert wait_for('http://localhost/settings', 'normal settings', exact=True) == (
        'normal settings'
    )


@pytest.mark.parametrize('mode', ['crash', 'startup-hang', 'healthy'])
def test_independent_recovery_and_normal_process_handoff(tmp_path, mode):
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    (tmp_path / '.env').write_text(f'FLASK_PORT={port}\n')
    (tmp_path / 'mode').write_text(mode)
    env = dict(
        os.environ, RECOVERY_TEST_ROOT=str(tmp_path), PYTHONDONTWRITEBYTECODE='1'
    )
    log = (tmp_path / 'output').open('w')
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / 'tests/recovery/probe_worker.py')],
        env=env,
        stdout=log,
        stderr=log,
    )
    url = f'http://127.0.0.1:{port}/settings'
    try:
        if mode == 'healthy':
            wait_for(url, 'normal settings', exact=True)
            import asyncio

            import websockets

            async def check_websocket():
                async with websockets.connect(f'ws://127.0.0.1:{port}/ws') as ws:
                    await ws.send('bluetooth-status')
                    assert await ws.recv() == 'echo:bluetooth-status'

            asyncio.run(check_websocket())
            (tmp_path / 'mode').write_text('runtime-hang')
            wait_for(url, 'Billy recovery', timeout=25)
        else:
            wait_for(url, 'Billy recovery')
            time.sleep(3)
            wait_for(url, 'Billy recovery')
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
        if proc.returncode not in {0, -15}:
            print((tmp_path / 'output').read_text())


@pytest.mark.parametrize('mode', ['core-control-hang', 'core-loop-hang'])
def test_core_hang_restarts_are_bounded(tmp_path, mode):
    from billy_runtime.recovery import Recovery

    (tmp_path / '.env').write_text('FLASK_PORT=8080\n')
    (tmp_path / 'mode').write_text(mode)
    env = dict(
        os.environ,
        RECOVERY_TEST_ROOT=str(tmp_path),
        RECOVERY_TEST_ROLE='core',
        PYTHONDONTWRITEBYTECODE='1',
    )
    with (tmp_path / 'output').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'tests/recovery/probe_worker.py')],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            store = Recovery(tmp_path)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                state = store.read_state()
                if max(state.get('failures', {}).values(), default=0) >= 3:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError('No bounded core failure recovery')
            time.sleep(1)
            assert proc.poll() is None
            assert not store.backups()
        finally:
            proc.terminate()
            proc.wait(timeout=12)


def test_real_web_entrypoint_survives_broken_app_import(tmp_path):
    import shutil

    shutil.copytree(
        ROOT / 'billy_runtime',
        tmp_path / 'billy_runtime',
        ignore=shutil.ignore_patterns('__pycache__'),
    )
    (tmp_path / 'webconfig').mkdir()
    (tmp_path / 'application/webconfig/app').mkdir(parents=True)
    shutil.copy2(ROOT / 'webconfig' / 'server.py', tmp_path / 'webconfig' / 'server.py')
    shutil.copy2(
        ROOT / 'application/webconfig/startup.py',
        tmp_path / 'application/webconfig/startup.py',
    )
    (tmp_path / 'application/webconfig/__init__.py').write_text('')
    (tmp_path / 'application/core').mkdir()
    (tmp_path / 'application/core/__init__.py').write_text('')
    (tmp_path / 'application/core/providers.py').write_text(
        'def initialize_providers(): pass\n'
    )
    (tmp_path / 'application/webconfig/app/__init__.py').write_text(
        'raise RuntimeError("Broken profile import fixture")\n'
    )
    # A legacy root/app directory must not shadow the intended web package.
    (tmp_path / 'app').mkdir()
    (tmp_path / 'app' / '__init__.py').write_text(
        'raise RuntimeError("Wrong application selected")\n'
    )
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    (tmp_path / '.env').write_text(f'FLASK_PORT={port}\n')
    with (tmp_path / 'output').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(tmp_path / 'webconfig/server.py')],
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'),
            stdout=log,
            stderr=log,
        )
        try:
            wait_for(f'http://127.0.0.1:{port}/settings', 'Billy recovery')
            deadline = time.monotonic() + 4
            while (
                'Broken profile import fixture' not in (tmp_path / 'output').read_text()
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            assert 'Broken profile import fixture' in (tmp_path / 'output').read_text()
            assert 'Wrong application selected' not in (tmp_path / 'output').read_text()
            assert not (tmp_path / 'state/recovery/access-token').exists()
            body = wait_for(f'http://127.0.0.1:{port}/repair/', 'Software recovery')
            assert 'Unlock recovery' not in body
            assert f'FLASK_PORT={port}' in body
        finally:
            proc.terminate()
            proc.wait(timeout=12)


def test_operator_retry_reloads_healthy_web_worker_without_counting_failure(tmp_path):
    from billy_runtime.recovery import Recovery

    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    (tmp_path / '.env').write_text(f'FLASK_PORT={port}\n')
    (tmp_path / 'mode').write_text('healthy')
    with (tmp_path / 'output').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'tests/recovery/probe_worker.py')],
            env=dict(
                os.environ,
                RECOVERY_TEST_ROOT=str(tmp_path),
                PYTHONDONTWRITEBYTECODE='1',
            ),
            stdout=log,
            stderr=log,
        )
        try:
            wait_for(f'http://127.0.0.1:{port}/settings', 'normal settings', exact=True)
            store = Recovery(tmp_path)
            report = store.runtime / 'web.json'
            import json

            deadline = time.monotonic() + 4
            while (
                not report.exists() or not json.loads(report.read_text()).get('healthy')
            ) and time.monotonic() < deadline:
                time.sleep(0.1)
            old = json.loads(report.read_text())['identity']
            store.state(retry='operator-request')
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                new = json.loads(report.read_text())
                if new['identity'] != old and new['healthy']:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError('Healthy worker did not restart on explicit retry')
            assert not store.read_state().get('failures')
        finally:
            proc.terminate()
            proc.wait(timeout=12)


def test_core_activity_reaches_status_without_a_broker(tmp_path):
    from billy_runtime.worker import read_activity

    (tmp_path / '.env').write_text('FLASK_PORT=8080\n')
    (tmp_path / 'mode').write_text('core-activity')
    with (tmp_path / 'output').open('w') as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / 'tests/recovery/probe_worker.py')],
            env=dict(
                os.environ,
                RECOVERY_TEST_ROOT=str(tmp_path),
                RECOVERY_TEST_ROLE='core',
                BILLY_INSTALL_ROOT=str(tmp_path),
                PYTHONDONTWRITEBYTECODE='1',
            ),
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if read_activity(tmp_path) == 'bt_media':
                    break
                time.sleep(0.1)
            else:
                raise AssertionError((tmp_path / 'output').read_text())
        finally:
            proc.terminate()
            proc.wait(timeout=12)
