"""Compatibility helpers for optional flask_sock support."""

from __future__ import annotations


try:
    from flask_sock import Sock as _Sock
except Exception:
    _Sock = None


class _DisabledSock:
    """No-op fallback when flask_sock is unavailable."""

    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        # Keep startup resilient; websocket endpoints remain unavailable.
        return None

    def route(self, *_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


Sock = _Sock or _DisabledSock
SOCK_AVAILABLE = _Sock is not None
