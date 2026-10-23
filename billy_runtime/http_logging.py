"""HTTP diagnostics without importing Billy's hardware-dependent core.

Health and log polling must not generate their own successful access records.
Other successful requests are verbose; HTTP failures retain their severity.
"""

import os

from werkzeug.serving import WSGIRequestHandler


class BillyRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        if 200 <= status < 400 and self.path.partition('?')[0] in {
            '/__runtime_health',
            '/logs',
        }:
            return
        level = 'error' if status >= 500 else 'warning' if status >= 400 else 'info'
        self.log(level, '"%s" %s %s', self.requestline, code, size)

    def log(self, type, message, *args):
        levels = {'ERROR': 0, 'WARNING': 1, 'INFO': 2, 'VERBOSE': 3}
        current = levels.get(os.environ.get('LOG_LEVEL', 'INFO').upper(), 2)
        required = {'error': 0, 'warning': 1}.get(type, 3)
        if required <= current:
            super().log(type, message, *args)
