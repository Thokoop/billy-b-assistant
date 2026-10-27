"""Pairing UI, and what the Bluetooth audio path is currently doing."""

import json
import secrets
import threading

from flask import Blueprint, jsonify, redirect, request

from billy_runtime.bluetooth_status import status_path


bp = Blueprint('bluetooth', __name__)
_controller = None
_lock = threading.Lock()
_csrf = secrets.token_urlsafe(32)


def controller():
    global _controller
    with _lock:
        if _controller is None:
            from core.bluetooth.bluez import BluezController

            _controller = BluezController()
    return _controller


AUDIO_STATUS_PATH = status_path()
PLAYING_STATES = {'media_audio', 'call_audio'}


def audio_status():
    """What the Bluetooth session last reported about the audio path.

    The session runs inside billy.service, a different process from this one,
    so a file in the runtime directory is how it says what it is doing. It is
    written on every transition, including back to idle when a session ends and
    at startup, so a missing or unreadable file means nothing is playing.
    """
    try:
        return json.loads(AUDIO_STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


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
    # Asked of the audio path rather than of systemd: Bluetooth audio is a mode
    # inside billy.service now, so there is no separate unit to be active.
    if request.path in protected and audio_status().get('state') in PLAYING_STATES:
        return jsonify(
            error='Bluetooth audio is in use. Stop playback or end the call first.'
        ), 409
    return None


@bp.app_context_processor
def template_context():
    return {'bluetooth_token': _csrf}


@bp.get('/bluetooth')
def page():
    return redirect('/settings#section-bluetooth')


def status_snapshot():
    """Read the same status for HTTP and live updates, without Flask context."""
    from core.bluetooth.provisioning import setup_status

    setup = setup_status()
    if setup['state'] != 'ready':
        return {'setup': setup, 'available': False}, 200
    try:
        manager = controller()
        result = manager.run(manager.status())
        result['setup'] = setup
        result['audio'] = audio_status()
        return result, 200
    except Exception as exc:
        return {'error': str(exc), 'available': False}, 503


@bp.get('/api/bluetooth')
def get_status():
    result, code = status_snapshot()
    return jsonify(result), code


@bp.post('/api/bluetooth/<action>')
def action(action):
    if not secrets.compare_digest(request.headers.get('X-Bluetooth-Token', ''), _csrf):
        return jsonify(error='Reload System Settings and try again'), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='Expected a JSON object'), 400
    from core.bluetooth.provisioning import setup_status

    if action == 'setup-retry':
        from billy_runtime.updates import queue

        try:
            return jsonify(queue('packages')), 202
        except (ValueError, RuntimeError, OSError) as exc:
            return jsonify(error=str(exc)), 409
    if setup_status()['state'] != 'ready':
        return jsonify(error='Bluetooth setup is not complete'), 409
    try:
        manager = controller()
        if action == 'pair':
            result = manager.run(manager.start_pairing())
        elif action == 'pair-stop':
            result = manager.run(manager.stop_pairing())
        elif action == 'confirm':
            raise ValueError('Confirm pairing on your device; reload System Settings')
        elif action == 'auto-connect':
            result = manager.run(
                manager.set_auto_connect(data.get('address'), data.get('enabled'))
            )
        elif action == 'select':
            result = manager.run(
                manager.device_action(data.get('address'), action), timeout=30
            )
        elif action in {'connect', 'disconnect', 'forget'}:
            result = manager.run(manager.device_action(data.get('address'), action))
        else:
            return jsonify(error='Unknown action'), 404
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 503
