"""Service supervisor with an independent recovery server on the same listen socket.

Existing unit ExecStart paths keep working. No systemd unit migration required.
"""

import contextlib
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

from . import updates
from .configuration import (
    BOOTSTRAP_KEYS,
    ROOT,
    ConfigError,
    revision,
    validate_bootstrap,
)
from .legacy_dependencies import LegacyDependencyError, ensure_for_worker
from .recovery import SAFE_DEFAULTS, Recovery
from .storage import atomic_write


START_TIMEOUT = 60
CORE_TIMEOUT = 45
PROBE_INTERVAL = 2
_current = []


def stop(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def _shutdown(*_):
    for child in list(_current):
        stop(child)
    raise SystemExit(0)


def launch(entry, mode, content, listener=None, *, root=ROOT):
    environment = dict(os.environ)
    python, working = sys.executable, ROOT
    software = None
    standard = entry == root / 'main.py' or entry == root / 'webconfig/server.py'
    if standard:
        environment['BILLY_INSTALL_ROOT'] = str(root)
        for key in ('BILLY_RELEASE_ID', 'BILLY_RELEASE_VERSION'):
            environment.pop(key, None)
        if mode != 'recovery':
            updates.sync_source_link(root)
            selected = updates.state(root)
            record = selected.get('current') or updates.baseline(root)
            source, candidate_python = updates.resolve(record, root)
            software = f"{selected.get('generation', 'baseline')}:{record['id']}"
            if record['id'] != 'baseline':
                python, working = str(candidate_python), source
                entry = source / (
                    'main.py' if mode == 'core' else 'webconfig/server.py'
                )
                environment.update(
                    BILLY_RELEASE_ID=record['id'],
                    BILLY_RELEASE_VERSION=record['version'],
                )
    read_fd, write_fd = os.pipe()
    gate_read, gate_write = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    config = tempfile.TemporaryFile()  # noqa: SIM115 - closed after descriptor inheritance
    config.write(content)
    config.seek(0)
    environment.update(
        BILLY_PROGRESS_FD=str(write_fd),
        BILLY_START_FD=str(gate_read),
        BILLY_CONFIG_FD=str(config.fileno()),
        BILLY_WORKER_MODE=mode,
    )
    descriptors = [write_fd, gate_read, config.fileno()]
    if listener:
        descriptors.append(listener.fileno())
        environment['BILLY_LISTEN_FD'] = str(listener.fileno())
        environment['BILLY_LISTEN_PORT'] = str(listener.getsockname()[1])
    try:
        child = subprocess.Popen(
            [python, str(entry), '--billy-worker'],
            env=environment,
            pass_fds=descriptors,
            cwd=working,
        )
    except BaseException:
        os.close(read_fd)
        os.close(gate_write)
        raise
    finally:
        os.close(write_fd)
        os.close(gate_read)
        config.close()
    child.billy_software = software
    _current.append(child)
    return child, read_fd, gate_write


def worker_environment():
    """Load exactly the bytes chosen by the supervisor, before any core import."""
    fd = int(os.environ.pop('BILLY_CONFIG_FD'))
    with os.fdopen(fd, 'rb') as stream:
        content = stream.read()
    from .configuration import apply_environment
    from .worker import notify

    recovery = os.environ['BILLY_WORKER_MODE'] == 'recovery'
    try:
        values = apply_environment(
            content.decode(),
            override=os.environ['BILLY_WORKER_MODE'] != 'core',
            validator=validate_bootstrap if recovery else None,
        )
    except ConfigError as exc:
        # Only key/line diagnostics, never configuration values or credentials.
        notify(f'config-error:{exc}')
        raise

    # Preserve legacy precedence: core honors inherited environment; web uses .env.
    os.environ['BILLY_CONFIG_LOADED'] = '1'
    os.environ['BILLY_APPLIED_REVISION'] = revision(content)
    keys = set(values) | BOOTSTRAP_KEYS
    if not recovery:
        from .configuration_schema import ENVIRONMENT_KEYS

        keys |= ENVIRONMENT_KEYS
    effective = {key: os.environ.get(key) for key in keys}
    notify('environment:' + revision(json.dumps(effective, sort_keys=True)))
    return content


def serve(app, *, wait_for_start=True):
    from werkzeug.serving import make_server

    from .http_logging import BillyRequestHandler
    from .worker import notify

    fd = int(os.environ['BILLY_LISTEN_FD'])
    port = int(os.environ['BILLY_LISTEN_PORT'])
    if wait_for_start:
        notify('ready')
        with os.fdopen(int(os.environ['BILLY_START_FD']), 'rb') as gate:
            if gate.read(1) != b'1':
                return
    server = make_server(
        '0.0.0.0',
        port,
        app,
        threaded=True,
        fd=fd,
        request_handler=BillyRequestHandler,
    )
    server.serve_forever()


def monitor(entry, role, root=ROOT):
    store = Recovery(root)
    managed = entry == root / 'main.py' or entry == root / 'webconfig/server.py'
    active_software = updates.generation(root) if managed else None
    resume_checked = 0
    resumer = None

    def resume_update():
        try:
            updates.resume(root)
        except Exception as exc:
            print(f'Updater resume unavailable ({type(exc).__name__})', flush=True)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    listener = None
    fallback = None
    retries = 0
    retry_seen = store.read_state().get("retry")
    try:
        content, allowed = store.prepare()
    except Exception as exc:
        print(f'Bootstrap failed ({type(exc).__name__}); recovery only', flush=True)
        content, allowed = SAFE_DEFAULTS.encode(), False
    if role == 'web':
        values = validate_bootstrap(content.decode())
        port = int(values.get('FLASK_PORT', '80'))
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('0.0.0.0', port))
        listener.listen(128)
        with contextlib.suppress(Exception):
            store.state(listen_port=port)

    def start_recovery():
        nonlocal fallback
        if role != 'web' or (fallback and fallback.poll() is None):
            return
        if fallback in _current:
            _current.remove(fallback)
        fallback, fd, gate = launch(entry, 'recovery', content, listener, root=root)
        os.close(fd)
        os.close(gate)

    effective = None

    def report(identity, healthy):
        try:
            store.report(
                role,
                identity,
                content,
                healthy=healthy,
                effective=effective,
                software=active_software,
            )
        except Exception as exc:
            print(f'Recovery metadata unavailable ({type(exc).__name__})', flush=True)

    try:
        start_recovery()
        while True:
            if managed and role == 'web' and time.monotonic() - resume_checked > 30:
                resume_checked = time.monotonic()
                if resumer is None or not resumer.is_alive():
                    resumer = threading.Thread(target=resume_update, daemon=True)
                    resumer.start()
            changed_software = managed and updates.generation(root) != active_software
            if changed_software:
                retries = 0
                active_software = updates.generation(root)
            if not allowed or retries >= 3:
                # Keep a repair surface, not an automatic crash loop. User retry
                # or an edited config starts a new bounded attempt sequence.
                start_recovery()
                time.sleep(1)
                state = store.read_state()
                try:
                    changed = store.env.read_bytes() != content
                except OSError:
                    changed = False
                requested_retry = state.get('retry') if role == 'web' else None
                if requested_retry != retry_seen or changed:
                    retry_seen = requested_retry
                    retries = 0
                    try:
                        content, allowed = store.prepare()
                    except Exception:
                        allowed = False
                continue
            try:
                content, allowed = store.prepare()
            except Exception:
                allowed = False
                continue
            if not allowed:
                continue
            try:
                # Migrate old baseline dependencies before worker deadlines.
                # Web/recovery and selected release environments are excluded.
                ensure_for_worker(root, role, managed=managed)
                child, fd, gate = launch(entry, role, content, listener, root=root)
                active_software = child.billy_software
            except LegacyDependencyError as exc:
                atomic_write(store.directory / 'runtime-error', str(exc))
                print(str(exc), flush=True)
                # No repeated package installs in an automatic crash loop.
                retries = 3
                continue
            except (OSError, ValueError) as exc:
                print(f'Worker launch failed ({type(exc).__name__})', flush=True)
                retries += 1
                start_recovery()
                time.sleep(min(2**retries, 8))
                continue
            identity = uuid.uuid4().hex
            effective = None
            report(identity, False)
            started = last_progress = time.monotonic()
            last_probe = 0
            ready = False
            active_loops = {}
            probe_failures = 0
            reason = 'Worker exited before becoming healthy'
            configuration_error = None
            operator_retry = False
            with selectors.DefaultSelector() as selector:
                selector.register(fd, selectors.EVENT_READ)
                try:
                    while child.poll() is None:
                        for _, _event in selector.select(timeout=0.25):
                            chunk = os.read(fd, 8192)
                            if not chunk:
                                continue
                            for message in chunk.decode().splitlines():
                                if message.startswith('environment:'):
                                    effective = message.partition(':')[2]
                                elif message.startswith('config-error:'):
                                    configuration_error = message.partition(':')[2]
                                elif message == 'ready':
                                    ready = True
                                    reason = 'Worker exited unexpectedly'
                                    last_progress = time.monotonic()
                                    if role == 'web':
                                        stop(fallback)
                                        if fallback in _current:
                                            _current.remove(fallback)
                                        fallback = None
                                        os.write(gate, b'1')
                                elif message == 'progress':
                                    last_progress = time.monotonic()
                                elif message.startswith((
                                    'loop-start:',
                                    'loop-progress:',
                                )):
                                    active_loops[message.partition(':')[2]] = (
                                        time.monotonic()
                                    )
                                elif message.startswith('loop-end:'):
                                    active_loops.pop(message.partition(':')[2], None)
                        now = time.monotonic()
                        if managed and updates.generation(root) != active_software:
                            operator_retry = True
                            break
                        if managed and role == 'web' and now - resume_checked > 30:
                            resume_checked = now
                            if resumer is None or not resumer.is_alive():
                                resumer = threading.Thread(
                                    target=resume_update, daemon=True
                                )
                                resumer.start()
                        if not ready and now - started > START_TIMEOUT:
                            reason = 'Startup timed out; use recovery to inspect configuration'
                            break
                        if (
                            ready
                            and role == 'core'
                            and (
                                now - last_progress > CORE_TIMEOUT
                                or any(
                                    now - seen > CORE_TIMEOUT
                                    for seen in active_loops.values()
                                )
                            )
                        ):
                            reason = 'Core stopped making progress'
                            break
                        if ready and now - last_probe >= PROBE_INTERVAL:
                            last_probe = now
                            requested_retry = (
                                store.read_state().get('retry')
                                if role == 'web'
                                else None
                            )
                            if requested_retry != retry_seen:
                                retry_seen = requested_retry
                                operator_retry = True
                                break
                            healthy = now - last_progress < 8 and all(
                                now - seen < 8 for seen in active_loops.values()
                            )
                            if role == 'web':
                                try:
                                    # No DNS/network/cloud dependency in the watchdog.
                                    with urllib.request.urlopen(
                                        f'http://127.0.0.1:{port}/__runtime_health',
                                        timeout=2,
                                    ) as response:
                                        result = json.load(response)
                                        healthy = result.get('revision') == revision(
                                            content
                                        )
                                        probe_failures = 0
                                except Exception:
                                    healthy = False
                                    probe_failures += 1
                                if probe_failures >= 3:
                                    reason = 'Web interface stopped responding'
                                    break
                            report(identity, healthy)
                            if healthy and now - started > 90:
                                retries = 0
                finally:
                    stop(child)
                    with contextlib.suppress(BlockingIOError):
                        for message in os.read(fd, 8192).decode().splitlines():
                            if message.startswith('config-error:'):
                                configuration_error = message.partition(':')[2]
                    os.close(fd)
                    os.close(gate)
                    _current.remove(child)
            report(identity, False)
            if operator_retry:
                retries = 0
                start_recovery()
                continue
            retries += 1
            try:
                failure = (
                    f'Configuration needs attention: {configuration_error}'
                    if configuration_error
                    else f'{role}: {reason}'
                )
                atomic_write(store.directory / 'runtime-error', failure)
                rolled_back = (
                    False
                    if managed and updates.state(root).get("trial")
                    else store.failed(role, content)
                )
                if rolled_back:
                    retries = 0
                    content, allowed = store.prepare()
            except Exception as exc:
                print(
                    f'Automatic recovery unavailable ({type(exc).__name__})', flush=True
                )
            start_recovery()
            time.sleep(min(2**retries, 8))
    finally:
        for child in list(_current):
            stop(child)
        if listener:
            listener.close()
