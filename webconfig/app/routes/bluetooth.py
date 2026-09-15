"""Basic pairing UI and explicit prototype-service controls."""

import json
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, jsonify, redirect, request


bp = Blueprint('bluetooth', __name__)
_controller = None
_lock = threading.Lock()
_service_lock = threading.Lock()
_csrf = secrets.token_urlsafe(32)


def controller():
    global _controller
    with _lock:
        if _controller is None:
            from core.bluetooth.bluez import BluezController

            _controller = BluezController()
    return _controller


def service_state(name):
    result = subprocess.run(
        ['systemctl', 'is-active', name], capture_output=True, text=True, timeout=3
    )
    return result.stdout.strip()


@bp.before_app_request
def guard_audio_tests():
    protected = {
        '/mic-record/start',
        '/mic-record/play',
        '/speaker-test',
        '/mic-check',
        '/test-motor',
        '/wakeup/play',
    }
    if (
        request.path in protected
        and Path('/etc/systemd/system/billy-bluetooth.service').exists()
    ):
        try:
            if service_state('billy-bluetooth.service') in {'active', 'activating'}:
                return jsonify(
                    error='Bluetooth audio is in use. Return to AI before testing.'
                ), 409
        except (OSError, subprocess.TimeoutExpired):
            return jsonify(error='Cannot verify audio ownership; try again.'), 503
    return None


@bp.app_context_processor
def template_context():
    return {'bluetooth_token': _csrf}


@bp.get('/bluetooth')
def page():
    return redirect('/settings#section-bluetooth')


@bp.get('/api/bluetooth')
def get_status():
    from core.bluetooth.provisioning import setup_status

    setup = setup_status()
    if setup['state'] != 'ready':
        return jsonify(setup=setup, available=False)
    try:
        manager = controller()
        result = manager.run(manager.status())
        result['setup'] = setup
        result['service'] = service_state('billy-bluetooth.service')
        path = Path(f'/run/user/{os.getuid()}/billy-bluetooth.json')
        if path.exists():
            result['audio'] = json.loads(path.read_text())
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=str(exc), available=False), 503


@bp.post('/api/bluetooth/<action>')
def action(action):
    if not secrets.compare_digest(request.headers.get('X-Bluetooth-Token', ''), _csrf):
        return jsonify(error='Reload System Settings and try again'), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='Expected a JSON object'), 400
    from core.bluetooth.provisioning import setup_status, start_setup

    if action == 'setup-retry':
        start_setup(force=True)
        return jsonify(ok=True), 202
    if action != 'test-stop' and setup_status()['state'] != 'ready':
        return jsonify(error='Bluetooth setup is not complete'), 409
    try:
        if action in {'test-start', 'test-stop'}:
            with _service_lock:
                if action == 'test-start':
                    audio_routes = sys.modules.get('webconfig.app.routes.audio')
                    if audio_routes:
                        recording = getattr(audio_routes, 'mic_recording_thread', None)
                        if getattr(audio_routes, 'mic_check_running', False) or (
                            recording and recording.is_alive()
                        ):
                            return jsonify(
                                error='Stop the microphone test before starting Bluetooth.'
                            ), 409
                operation = 'start' if action == 'test-start' else 'stop'
                subprocess.run(
                    ['sudo', '-n', 'systemctl', operation, 'billy-bluetooth.service'],
                    check=True,
                    timeout=20,
                    capture_output=True,
                    text=True,
                )
                if action == 'test-stop':
                    subprocess.run(
                        ['sudo', '-n', 'systemctl', 'start', 'billy.service'],
                        check=True,
                        timeout=20,
                        capture_output=True,
                        text=True,
                    )
            return jsonify(ok=True)
        manager = controller()
        if action == 'pair':
            result = manager.run(manager.start_pairing())
        elif action == 'pair-stop':
            result = manager.run(manager.stop_pairing())
        elif action == 'confirm':
            if type(data.get('accepted')) is not bool:
                raise ValueError('Expected an accept/reject choice')
            result = manager.run(manager.respond(data.get('id'), data['accepted']))
        elif action in {'connect', 'disconnect', 'forget'}:
            result = manager.run(manager.device_action(data.get('address'), action))
        else:
            return jsonify(error='Unknown action'), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except subprocess.CalledProcessError as exc:
        return jsonify(error=exc.stderr.strip() or 'Service control failed'), 503
    except Exception as exc:
        return jsonify(error=str(exc)), 503
