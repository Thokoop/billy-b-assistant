"""Existing service commands must survive application moves and release switches."""

import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from test_update_process import wait_for
from test_updates import candidate

from billy_runtime import updates


ROOT = Path(__file__).resolve().parents[2]

CORE = '''import time
from billy_runtime.worker import ready, progress
def run():
    ready()
    while True:
        progress()
        time.sleep(0.1)
'''
WEB = '''import os
from flask import Flask
def normal_app():
    app = Flask(__name__)
    @app.get('/settings')
    def settings():
        return os.environ.get('BILLY_RELEASE_VERSION', 'baseline')
    @app.get('/__runtime_health')
    def health():
        return {'revision': os.environ['BILLY_APPLIED_REVISION']}
    return app
'''


def copy_runtime(source):
    shutil.copytree(
        ROOT / 'billy_runtime',
        source / 'billy_runtime',
        ignore=shutil.ignore_patterns('__pycache__'),
    )
    (source / 'webconfig').mkdir()
    shutil.copy2(ROOT / 'main.py', source / 'main.py')
    shutil.copy2(ROOT / 'webconfig/server.py', source / 'webconfig/server.py')


def make_application(source):
    for package in ('core', 'webconfig'):
        folder = source / 'application' / package
        folder.mkdir(parents=True)
        (folder / '__init__.py').write_text('')
    (source / 'application/core/application.py').write_text(CORE)
    (source / 'application/webconfig/startup.py').write_text(WEB)


def make_legacy_application(source):
    # Earlier development layouts use the same worker protocol and service paths,
    # but their own startup files import code without an application/ directory.
    prefix = '''import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from billy_runtime.supervisor import worker_environment, serve
worker_environment()
'''
    (source / 'webconfig/server.py').write_text(
        prefix + WEB + '\nserve(normal_app())\n'
    )
    (source / 'main.py').write_text(
        'from billy_runtime.supervisor import worker_environment\nworker_environment()\n'
        + CORE
        + '\nrun()\n'
    )


def wait_healthy(root, expected):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if updates.healthy(root, expected, window=0):
            return
        time.sleep(0.1)
    raise AssertionError('Both workers did not become healthy for ' + expected)


@pytest.mark.parametrize('layout', ['grouped', 'legacy'])
def test_unchanged_service_commands_update_recover_and_rollback(tmp_path, layout):
    root = tmp_path / 'billy-b-assistant'
    root.mkdir()
    copy_runtime(root)
    make_application(root)
    # Real-world git checkouts may retain old untracked files. They must not win.
    for relative in [
        'core/__init__.py',
        'app/__init__.py',
        'webconfig/app/__init__.py',
    ]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('raise AssertionError("Stale root application loaded")\n')
    (root / 'venv/bin').mkdir(parents=True)
    # Invoke the test environment through the exact legacy service path.
    # A bare interpreter symlink would lose its source venv's site-packages.
    interpreter = root / 'venv/bin/python'
    interpreter.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    interpreter.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    data = {
        '.env': f'FLASK_PORT={port}\nREALTIME_AI_PROVIDER=openai\n',
        'persona.ini': '[PERSONA]\nname=Original\n',
        'profiles/owner.ini': '[USER_INFO]\nname=Owner\n',
        'sounds/wake-up/custom/1.wav': 'existing clip',
    }
    for relative, content in data.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    record = candidate(root)
    source, python = updates.resolve(record, root)
    python.unlink()
    python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    copy_runtime(source)
    (make_application if layout == 'grouped' else make_legacy_application)(source)
    updates.share_data(source, root)
    environment = {k: v for k, v in os.environ.items() if not k.startswith('BILLY_')}
    url = f'http://127.0.0.1:{port}/settings'
    with ExitStack() as stack:
        processes = []
        for entry in ['main.py', 'webconfig/server.py']:
            log = stack.enter_context(
                (tmp_path / (Path(entry).stem + '.log')).open('w')
            )
            process = subprocess.Popen(
                [str(root / 'venv/bin/python'), str(root / entry)],
                cwd=root,
                env=environment,
                stdout=log,
                stderr=log,
            )
            processes.append(process)
        try:
            wait_for(url, 'baseline', exact=True)
            wait_healthy(root, updates.generation(root))
            generation = updates.activate(
                root, record, updates.baseline(root), trial=True
            )
            wait_for(url, 'v2', exact=True)
            wait_healthy(root, generation)
            assert (root / 'current-source').resolve() == source
            assert all(p.poll() is None for p in processes)

            if layout == 'grouped':
                # Recovery must also survive a completely missing application tree.
                shutil.rmtree(source / 'application')
            else:
                (source / 'webconfig/server.py').write_text(
                    'raise RuntimeError("Broken candidate application")\n'
                )
            updates.activate(root, record, updates.baseline(root), trial=True)
            wait_for(url, 'Billy recovery')
            generation = updates.activate(
                root, updates.baseline(root), record, trial=False
            )
            wait_for(url, 'baseline', exact=True)
            wait_healthy(root, generation)
            assert all(p.poll() is None for p in processes)
            assert (root / 'current-source').resolve() == root
            for relative, content in data.items():
                assert (root / relative).read_text() == content
            for entry in ['main.py', 'webconfig/server.py']:
                assert (root / entry).read_bytes() == (ROOT / entry).read_bytes()
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


@pytest.mark.parametrize('entry', ['main.py', 'webconfig/server.py'])
def test_service_launchers_are_importable_without_application_or_dependencies(
    tmp_path, entry
):
    path = tmp_path / entry
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / entry, path)
    result = subprocess.run(
        [
            sys.executable,
            '-I',
            '-S',
            '-c',
            'import runpy, sys; runpy.run_path(sys.argv[1]); '
            'assert not any(n == "core" or n.startswith("core.") for n in sys.modules)',
            str(path),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_service_templates_keep_the_existing_installation_contract():
    for name, entry in [
        ('billy.service', 'main.py'),
        ('billy-webconfig.service', 'webconfig/server.py'),
        ('billy-wifi-setup.service', 'setup/wifi_setup.py'),
    ]:
        content = (ROOT / 'setup/system' / name).read_text()
        assert (
            f'ExecStart=/home/pi/billy-b-assistant/venv/bin/python /home/pi/billy-b-assistant/{entry}'
            in content
        )
        assert (ROOT / entry).is_file()
    content = (ROOT / 'setup/system/billy-wifi-check.service').read_text()
    assert 'ExecStart=/home/pi/billy-b-assistant/setup/wifi_check.sh' in content
    assert (ROOT / 'setup/wifi_check.sh').is_file()


def test_moved_song_manager_keeps_data_and_shipped_examples_separate(
    isolated, monkeypatch, tmp_path
):
    from billy_runtime import paths

    install = tmp_path / 'installation'
    source = tmp_path / 'release'
    monkeypatch.setattr(paths, 'INSTALL_ROOT', install)
    monkeypatch.setattr(paths, 'CODE_ROOT', source)
    module = isolated.load('song_manager')
    manager = module.song_manager
    assert manager.custom_songs_dir == install / 'custom_songs'
    assert manager.state_dir == install / 'state'
    assert manager.example_songs_dir == source / 'sounds/songs'
    assert not (source / 'custom_songs').exists()
