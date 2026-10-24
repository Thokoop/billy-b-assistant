import json
import os
import subprocess
import threading
import time

from flask import Blueprint

from billy_runtime.worker import read_activity

from .journal import SOURCES, read_logs


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
                "billy_state": read_activity(),
                "current_persona": persona_manager.current_persona,
                "current_personality": PERSONALITY.to_dict()
                if hasattr(PERSONALITY, 'to_dict')
                else None,
            }
        except Exception:
            return {"status": status, "billy_state": read_activity()}
    except Exception:
        return {"status": "unknown"}


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


def _read_recovery_status():
    from billy_runtime.recovery import Recovery

    return Recovery().notice()


_recovery_status = _BackgroundStatus('recovery', _read_recovery_status, 2)
_bluetooth_status = _BackgroundStatus('bluetooth', _read_bluetooth_status, 2)
_device_info_status = _BackgroundStatus('device-info', _read_device_info, 5)


# A slow journal read never blocks connection/recovery status. Multiple tabs
# share one read per source per second; closed panels request no samples.
_log_status = {
    source: _BackgroundStatus(f"logs-{source}", lambda s=source: read_logs(s), 1)
    for source in SOURCES
}


@sock.route("/ws")
def websocket_handler(ws):
    """WebSocket endpoint for real-time updates."""
    last_status = None
    last_logs = None
    log_source = "billy"  # Older clients do not send subscriptions.

    while True:
        try:
            # Non-blocking, one control message per tick. Invalid source names
            # cannot become journal arguments or terminate status updates.
            control = ws.receive(timeout=0)
            if control:
                try:
                    message = json.loads(control)
                    if (
                        isinstance(message, dict)
                        and message.get("type") == "logs_subscribe"
                    ):
                        source = message.get("source")
                        if source is None or (
                            isinstance(source, str) and source in SOURCES
                        ):
                            if source != log_source:
                                last_logs = None
                            log_source = source
                except (ValueError, TypeError):
                    pass

            # Check for status changes
            status = get_service_status()
            status['live_captions'] = live_captions_enabled()
            for name, sampler in (
                ('recovery', _recovery_status),
                ('bluetooth', _bluetooth_status),
                ('device_info', _device_info_status),
            ):
                snapshot = sampler.get()
                if snapshot is not None:
                    status[name] = snapshot
            if status != last_status:
                ws.send(json.dumps({"type": "status", "data": status}))
                last_status = status

            if log_source is not None:
                snapshot = _log_status[log_source].get()
                if snapshot is not None and snapshot != last_logs:
                    ws.send(
                        json.dumps({
                            "type": "logs",
                            "source": log_source,
                            "data": snapshot.get("logs", ""),
                            "error": snapshot.get("error", ""),
                        })
                    )
                    last_logs = snapshot

            # Sleep to avoid excessive polling
            time.sleep(1)

        except Exception:
            break


def live_captions_enabled():
    return os.getenv('REALTIME_AI_PROVIDER', '').strip().lower() == 'gpt-live'


@sock.route('/ws/live-captions')
def live_captions_handler(ws):
    from core.providers.live_transcript import read_live_transcript

    previous = object()
    include_transcript = False
    transcript_sent = None
    while True:
        try:
            command = ws.receive(timeout=0)  # Detect a closed subscriber promptly.
            if isinstance(command, str) and len(command) <= 128:
                try:
                    view = json.loads(command).get('view')
                except (ValueError, AttributeError):
                    view = None
                if view in ('captions', 'transcript'):
                    requested = view == 'transcript'
                    if requested != include_transcript:
                        include_transcript = requested
                        transcript_sent = None
            snapshot = read_live_transcript() if live_captions_enabled() else None
            if snapshot is None:
                transcript_sent = None
            elif include_transcript:
                revision = (snapshot['session'], snapshot.get('revision'))
                if revision != transcript_sent:
                    snapshot = read_live_transcript(include_transcript=True)
                    if snapshot and 'transcript' in snapshot:
                        transcript_sent = (
                            snapshot['session'],
                            snapshot.get('revision'),
                        )
            if snapshot != previous:
                ws.send(json.dumps(snapshot))
                previous = snapshot
            time.sleep(0.2)
        except Exception:
            break
