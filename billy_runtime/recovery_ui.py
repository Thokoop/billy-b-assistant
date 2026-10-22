"""Small recovery web UI; no core, audio, GPIO or profile-manager imports."""

import configparser
import contextlib
import hmac
import ipaddress
import json
import secrets
import subprocess
from urllib.parse import urlsplit

from flask import Flask, jsonify, redirect, render_template_string, request, session

from . import updates
from .configuration import ConfigError, ConflictError, revision, save_env, validate
from .profiles import profile_lock
from .storage import atomic_write, transaction


PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Billy recovery</title><style>
body{font:17px system-ui;background:#103747;color:#edf5f7;max-width:950px;margin:40px auto;padding:20px}a{color:#67e8b0}button,input,select,textarea{font:inherit;padding:10px;border-radius:8px;border:1px solid #79929c}button{cursor:pointer;background:#36cc98;color:#082a23;margin:8px 4px 8px 0}textarea{box-sizing:border-box;width:100%;height:45vh;font:14px monospace}section{background:#174353;padding:20px;border-radius:12px;margin:20px 0}.error{color:#ffc9ab}small{color:#b7cbd3}</style>
<h1>Billy recovery</h1><p>The web interface is available even when Billy cannot start.</p>
<p>{{ status.get('reason', 'Use this page to repair configuration or retry startup.') }}</p><p>State: {{ status.get('mode', 'starting') }}. {{ runtime }}</p>
{% if message %}<p class="error" role="status">{{ message }}</p>{% endif %}
<section><h2>Software recovery</h2><p id="software-progress" role="status">{{ software.get('job', {}).get('message', '') }}</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button name="action" value="reinstall">Reinstall current version</button><button name="action" value="rollback" {% if not software.get('rollback_available') %}disabled{% endif %}>Restore previous version</button></form><small>Settings, profiles and memories are preserved.</small></section>
<section><h2>System components</h2><p id="package-status" role="status">{{ software.get('packages', {}).get('message', 'Checking system components…') }}</p><p id="package-details"></p><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button id="package-repair" name="action" value="packages" hidden disabled>Repair system components</button></form><small>Installs required components and repairs Billy’s Bluetooth setup. Billy may pause briefly. Keep the power connected.</small></section>
<script>async function softwareProgress(){try{const r=await fetch('/software/status',{cache:'no-store',signal:AbortSignal.timeout(4000)});const s=await r.json();document.getElementById('software-progress').textContent=s.job?.message||'';const p=s.packages||{};document.getElementById('package-status').textContent=p.message||'Checking system components…';document.getElementById('package-details').textContent=[...(p.missing||[]),...(p.damaged||[]),...(p.updates||[])].join(', ');const busy=['queued','preparing','installing_packages','activating','validating','rolling_back'].includes(s.job?.state);document.getElementById('package-repair').hidden=!p.repairable;document.getElementById('package-repair').disabled=busy||!p.repairable;}catch(e){}setTimeout(softwareProgress,2000);}softwareProgress();</script>
<form method="get"><label>Open file <select name="file">{% for name in files %}<option {% if name==selected %}selected{% endif %}>{{ name }}</option>{% endfor %}</select></label><button>Open</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="file" value="{{ selected }}"><input type="hidden" name="revision" value="{{ digest }}"><textarea name="content" spellcheck="false" aria-label="Configuration">{{ content }}</textarea><p><small>Backups and damaged originals can be opened here. Saving an environment backup or original repairs the active .env. Other files are saved separately.</small></p><button name="action" value="save">Validate and save</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><button name="action" value="retry">Retry web interface</button><button name="action" value="core">Restart Billy</button></form>
<p>After retrying, <a href="/settings">open the normal settings</a>. A restart is needed to activate repaired settings.</p>
{% if auto_return %}<script>
async function checkReady(){try{const r=await fetch('/health',{cache:'no-store',signal:AbortSignal.timeout(2000)});const s=await r.json();if(s.status==='ok'&&!s.recovery){location.replace('/settings');return;}}catch(e){}setTimeout(checkReady,2000);}setTimeout(checkReady,2000);
</script>{% endif %}</html>'''


def create_recovery_app(store):
    app = Flask('billy-recovery', static_folder=None)
    # Match the normal local web UI: no separate login or SSH-only access code.
    # An in-memory signing key keeps CSRF sessions usable even on read-only disk.
    # After a worker restart, reopening the page establishes a fresh session.
    app.secret_key = secrets.token_bytes(32)
    app.config.update(
        MAX_CONTENT_LENGTH=1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_NAME='billy_recovery',
    )

    @app.before_request
    def same_origin():
        # Links from onboarding documentation may navigate here; cross-site
        # mutations and explicit cross-origin fetches must still be rejected.
        if (
            request.method == 'POST'
            and request.headers.get('Sec-Fetch-Site') == 'cross-site'
        ):
            return 'Open Billy recovery directly in your browser.', 403
        origin = request.headers.get('Origin')
        if origin:
            try:
                source = urlsplit(origin)
                target = urlsplit(request.host_url)
                matches = (
                    source.scheme == target.scheme
                    and source.hostname == target.hostname
                    and (source.port or (443 if source.scheme == 'https' else 80))
                    == (target.port or (443 if target.scheme == 'https' else 80))
                )
            except ValueError:
                matches = False
            if not matches:
                return 'Open Billy recovery directly in your browser.', 403
        return None

    def files():
        choices = {'.env': store.env, 'persona.ini': store.root / 'persona.ini'}
        for name in ('profiles', 'personas'):
            pattern = '*.ini' if name == 'profiles' else '*/persona.ini'
            for path in (store.root / name).glob(pattern):
                if not path.is_symlink() and path.resolve().is_relative_to(
                    store.root.resolve()
                ):
                    choices[str(path.relative_to(store.root))] = path
        for pattern in ('good-*.env', 'corrupt-*.env'):
            for path in store.directory.glob(pattern):
                if not path.is_symlink():
                    choices[path.name] = path
        return {k: p for k, p in choices.items() if not p.is_symlink()}

    @app.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/software/status')
    def software_status():
        return jsonify(updates.status(store.root))

    @app.get('/health')
    def health():
        return jsonify(status='recovery', recovery=True)

    @app.get('/<path:path>')
    def captive_portal(path):
        # The forced Wi-Fi hotspot survives independently of the normal UI.
        # Captive probes use foreign hostnames, so redirect to Billy's actual
        # WLAN address instead of leaving the editor on a probe domain.
        if (
            path.startswith(('static/', 'api/'))
            or not (store.root / 'setup' / '.wifi_onboarding_active').exists()
        ):
            return '', 404
        host = None
        try:
            result = subprocess.run(
                ['ip', '-4', '-j', 'address', 'show', 'dev', 'wlan0'],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
            for interface in json.loads(result.stdout):
                for address in interface.get('addr_info', []):
                    ip = ipaddress.ip_address(address['local'])
                    if ip.version == 4 and not ip.is_loopback and not ip.is_unspecified:
                        host = str(ip)
                        break
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        # Direct access still works when the address lookup is unavailable.
        host = host or 'billy.local'
        port = store.read_state().get('listen_port', 80)
        authority = host if port == 80 else f'{host}:{port}'
        return redirect(f'http://{authority}/repair/', code=302)

    @app.route('/', methods=['GET', 'POST'])
    @app.route('/recovery', methods=['GET', 'POST'])
    @app.route('/settings', methods=['GET', 'POST'])
    @app.route('/repair/', methods=['GET', 'POST'])
    def page():
        message = ''
        saved = False
        if request.method == 'POST':
            csrf = session.get('csrf')
            if not csrf or not hmac.compare_digest(
                request.form.get('csrf', '').encode(), csrf.encode()
            ):
                return 'Invalid request; reload the recovery page.', 403
        else:
            session.setdefault('csrf', secrets.token_urlsafe(32))
        choices = files()
        selected = request.values.get('file', '.env')
        path = choices.get(selected)
        digest = ''
        content = ''
        if not path:
            return 'Unknown file', 404
        action = request.form.get('action') if request.method == 'POST' else None
        try:
            if action == 'save':
                content = request.form.get('content', '')
                expected = request.form.get('revision')
                if not expected:
                    raise ConflictError('Reload the file before saving')
                if selected == '.env' or path.parent == store.directory:
                    # Editor revision always refers to the active target, even
                    # while viewing a backup. Prevent overwriting a concurrent save.
                    save_env(store.env, content, expected=expected)
                    store.approve_setup()
                else:
                    parser = configparser.ConfigParser(interpolation=None)
                    parser.read_string(content)
                    if not parser.sections():
                        raise ConfigError('INI file must have at least one section')
                    with profile_lock(store.root), transaction(path):
                        if (
                            revision(path.read_bytes() if path.exists() else b'')
                            != expected
                        ):
                            raise ConflictError('File changed; reopen it before saving')
                        atomic_write(path, content)
                saved = True
                message = (
                    'Saved. Retry the web interface and restart Billy to activate.'
                )
            elif action == 'retry':
                validate(store.env.read_text(), complete=True)
                store.state(retry=secrets.token_hex(16))
                message = 'Startup requested. Open settings in a few seconds.'
            elif action == 'core':
                validate(store.env.read_text(), complete=True)
                store.approve_setup()
                result = subprocess.run(
                    ['sudo', '-n', 'systemctl', 'restart', 'billy.service'],
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
                message = (
                    'Billy restart requested.'
                    if result.returncode == 0
                    else 'Restart failed; check service permissions and journal.'
                )
            elif action in {'reinstall', 'rollback', 'packages'}:
                updates.queue(action, root=store.root)
                message = 'Software operation started; progress appears above.'
        except (
            OSError,
            ValueError,
            RuntimeError,
            configparser.Error,
            subprocess.SubprocessError,
        ) as exc:
            message = (
                str(exc)
                if isinstance(exc, ConfigError)
                or (
                    action in {'reinstall', 'rollback', 'packages'}
                    and isinstance(exc, (ValueError, RuntimeError))
                )
                else f'Operation failed ({type(exc).__name__}); original file retained where possible.'
            )
        if action == 'save' and not saved:
            content = request.form.get('content', '')
            digest = request.form.get('revision', '')
        else:
            try:
                content = (
                    path.read_bytes().decode('utf-8', errors='replace')
                    if path.exists()
                    else ''
                )
                target = store.env if path.parent == store.directory else path
                digest = revision(target.read_bytes() if target.exists() else b'')
            except OSError:
                message = 'Cannot read this file; check filesystem permissions.'
        runtime = ''
        with contextlib.suppress(OSError):
            runtime = (store.directory / 'runtime-error').read_text()
        return render_template_string(
            PAGE,
            files=choices,
            selected=selected,
            content=content,
            digest=digest,
            message=message,
            csrf=session.get('csrf'),
            status=store.read_state(),
            software=updates.status(store.root),
            runtime=runtime,
            auto_return=not bool(request.script_root),
        )

    return app
