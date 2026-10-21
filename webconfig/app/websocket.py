import json
import subprocess
import threading
import time

from flask import Blueprint


try:
    from .sock_compat import Sock
except ModuleNotFoundError:
    # Older/stale deployments may not include sock_compat yet.
    class Sock:  # type: ignore[override]
        def init_app(self, _app):
            return None

        def route(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator


bp = Blueprint("websocket", __name__)
sock = Sock()


def get_service_status():
    """Get full billy.service status with persona/personality data."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "billy.service"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        status = result.stdout.strip()

        # Get persona and personality data
        try:
            from core.persona_manager import persona_manager
            from core.personality import PERSONALITY

            return {
                "status": status,
                "current_persona": persona_manager.current_persona,
                "current_personality": PERSONALITY.to_dict()
                if hasattr(PERSONALITY, 'to_dict')
                else None,
            }
        except Exception:
            return {"status": status}
    except Exception:
        return {"status": "unknown"}


def get_logs(lines=50):
    """Get recent logs from billy.service."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "billy.service", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout
    except Exception:
        return ""


class _BackgroundStatus:
    """One shared, non-blocking sampler per status section, regardless of tabs."""

    def __init__(self, name, read, interval):
        self.name = name
        self.read = read
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshot = None
        self.refreshing = False
        self.checked_at = float('-inf')

    def refresh(self):
        try:
            snapshot = self.read()
        except Exception as exc:
            snapshot = {'error': str(exc)}
        with self.lock:
            self.snapshot = snapshot
            self.checked_at = time.monotonic()
            self.refreshing = False

    def get(self):
        # D-Bus and audio enumeration can stall. Neither may delay service/log
        # updates or the other sampler. No subscribers means no ongoing work.
        with self.lock:
            if (
                not self.refreshing
                and time.monotonic() - self.checked_at >= self.interval
            ):
                self.refreshing = True
                try:
                    threading.Thread(
                        target=self.refresh,
                        name=f'webconfig-{self.name}-status',
                        daemon=True,
                    ).start()
                except Exception:
                    self.refreshing = False
            return self.snapshot


def _read_bluetooth_status():
    from .routes.bluetooth import status_snapshot

    snapshot, _ = status_snapshot()
    return snapshot


def _read_device_info():
    from .routes.audio import device_info_snapshot

    # The sampler will try again later; do not run the HTTP endpoint's retry
    # loop on every sample when a microphone or speaker is absent.
    snapshot, _ = device_info_snapshot(max_attempts=1)
    return snapshot


_bluetooth_status = _BackgroundStatus('bluetooth', _read_bluetooth_status, 2)
_device_info_status = _BackgroundStatus('device-info', _read_device_info, 5)


@sock.route("/ws")
def websocket_handler(ws):
    """WebSocket endpoint for real-time updates."""
    last_status = None
    last_logs = None

    while True:
        try:
            # Check for status changes
            status = get_service_status()
            for name, sampler in (
                ('bluetooth', _bluetooth_status),
                ('device_info', _device_info_status),
            ):
                snapshot = sampler.get()
                if snapshot is not None:
                    status[name] = snapshot
            if status != last_status:
                ws.send(json.dumps({"type": "status", "data": status}))
                last_status = status

            # Check for new logs
            logs = get_logs(50)
            if logs != last_logs:
                ws.send(json.dumps({"type": "logs", "data": logs}))
                last_logs = logs

            # Sleep to avoid excessive polling
            time.sleep(1)

        except Exception:
            break
