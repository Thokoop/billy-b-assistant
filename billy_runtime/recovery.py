"""Revision-aware startup recovery and last-known-good configuration snapshots."""

import contextlib
import json
import math
import os
import time
from pathlib import Path

from .configuration import (
    ROOT,
    ConfigError,
    example_defaults,
    revision,
    validate_bootstrap,
)
from .storage import atomic_write, runtime_directory, transaction


SAFE_DEFAULTS = 'FLASK_PORT=80\nMOCKFISH=true\nWAKE_WORD_ENABLED=false\nBLUETOOTH_AUTO_SWITCH=false\n'


class Recovery:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.env = self.root / '.env'
        self.directory = self.root / 'state' / 'recovery'
        self.runtime = runtime_directory(self.root)

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise PermissionError('Unsafe recovery directory')
        if self.directory.stat().st_mode & 0o777 != 0o700:
            os.chmod(self.directory, 0o700)
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        if self.runtime.is_symlink() or self.runtime.stat().st_uid != os.getuid():
            raise PermissionError('Unsafe runtime directory')
        if self.runtime.stat().st_mode & 0o777 != 0o700:
            os.chmod(self.runtime, 0o700)

    def read_state(self):
        try:
            state = json.loads((self.directory / 'state.json').read_text())
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def state(self, **updates):
        self.initialize()
        with transaction(self.env):
            state = self.read_state()
            state.update(updates)
            atomic_write(self.directory / 'state.json', json.dumps(state))
            return state

    def backups(self):
        result = []
        for path in self.directory.glob('good-*.json'):
            try:
                meta = json.loads(path.read_text())
                digest = meta['revision']
                if path.name != f'good-{digest}.json' or len(digest) != 64:
                    continue
                content = (self.directory / f'good-{digest}.env').read_bytes()
                if revision(content) != digest:
                    continue
                validate_bootstrap(content.decode('utf-8'), complete=True)
                validated_at = float(meta['validated_at'])
                if not math.isfinite(validated_at) or validated_at <= 0:
                    continue
                result.append((validated_at, digest, content))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(result, reverse=True)

    def notice(self):
        """Public live status only; never include configuration or access codes."""
        state = self.read_state()
        try:
            runtime_error = (self.directory / 'runtime-error').read_text()
        except FileNotFoundError:
            runtime_error = ''
        except (OSError, UnicodeError):
            return {
                'active': True,
                'kind': 'runtime',
                'message': 'Unable to read recovery status.',
            }
        active = bool(runtime_error) or state.get('mode') in {
            'restored',
            'setup',
            'probation',
        }
        # A worker failure is not evidence of invalid .env syntax. Configuration
        # recovery and probation use different actions in the normal interface.
        if runtime_error:
            kind = (
                'configuration'
                if runtime_error.startswith('Configuration needs attention: ')
                else 'runtime'
            )
            message = runtime_error
        elif state.get('mode') in {'restored', 'setup'}:
            kind = 'configuration'
            message = state.get('reason') or 'Configuration needs attention.'
        elif active:
            kind = 'startup'
            message = 'Checking Billy and the web interface after startup.'
        else:
            kind, message = None, ''
        return {'active': active, 'kind': kind, 'message': message}

    def environment_files(self):
        """Allowlisted editor sources; contents are fetched only on selection."""
        if self.directory.is_symlink():
            raise PermissionError('Unsafe recovery directory')
        files = [{'name': '.env', 'kind': 'current', 'saved_at': None}]
        for saved_at, digest, _ in self.backups():
            path = self.directory / f'good-{digest}.env'
            if not path.is_symlink():
                files.append({
                    'name': path.name,
                    'kind': 'backup',
                    'saved_at': saved_at,
                })
        for path in sorted(self.directory.glob('corrupt-*.env'), reverse=True):
            if path.is_file() and not path.is_symlink():
                files.append({
                    'name': path.name,
                    'kind': 'original',
                    'saved_at': path.stat().st_mtime,
                })
        return files

    def environment_content(self, source='.env'):
        """Return selected bytes with the CURRENT file revision for safe saving."""
        if self.env.is_symlink():
            raise PermissionError('Unsafe configuration file')
        current = self.env.read_bytes() if self.env.exists() else b''
        if source == '.env':
            return current, revision(current)
        if source not in {item['name'] for item in self.environment_files()}:
            raise ValueError('Unknown configuration source')
        return (self.directory / source).read_bytes(), revision(current)

    def _quarantine(self, content):
        name = f'corrupt-{time.time_ns()}-{revision(content)[:12]}.env'
        atomic_write(self.directory / name, content)
        # Keep bounded evidence. Good snapshots are managed independently.
        for old in sorted(self.directory.glob('corrupt-*.env'))[:-10]:
            old.unlink()
        return name

    def prepare(self):
        """Return immutable loaded bytes and whether hardware startup is permitted."""
        self.initialize()
        with transaction(self.env):
            try:
                content = self.env.read_bytes()
                validate_bootstrap(content.decode('utf-8'), complete=True)
            except (OSError, UnicodeError, ConfigError) as exc:
                # A bad live file must be preserved before replacing it.
                original = self.env.read_bytes() if self.env.exists() else b''
                name = self._quarantine(original)
                backups = self.backups()
                if backups:
                    content = backups[0][2]
                    mode = 'restored'
                else:
                    content = example_defaults(self.root / '.env.example').encode()
                    previous_port = self.read_state().get('listen_port')
                    if isinstance(previous_port, int) and 1 <= previous_port <= 65535:
                        # Keep the known listen port even without a good core snapshot.
                        import re

                        content = re.sub(
                            rb'^FLASK_PORT=.*$',
                            f'FLASK_PORT={previous_port}'.encode(),
                            content,
                            flags=re.M,
                        )
                    mode = 'setup'
                atomic_write(self.env, content)
                self.state(
                    mode=mode,
                    reason=(
                        f'Invalid configuration: {exc}; original preserved'
                        if isinstance(exc, ConfigError)
                        else f'Invalid configuration ({type(exc).__name__}); original preserved'
                    ),
                    original=name,
                    restored_at=time.time(),
                    needs_setup=(mode == 'setup'),
                )
            state = self.read_state()
            return content, not state.get('needs_setup', False)

    def approve_setup(self):
        self.state(
            needs_setup=False, mode='probation', failures={}, rollback_used=False
        )

    def report(
        self, role, identity, content, *, healthy, effective=None, software=None
    ):
        """Called by the supervisor only after useful worker progress / HTTP response."""
        self.initialize()
        with transaction(self.env):
            path = self.runtime / f'{role}.json'
            now = time.time()
            old = {}
            with contextlib.suppress(OSError, ValueError):
                old = json.loads(path.read_text())
            digest = revision(content)
            same = (
                old.get('identity') == identity
                and old.get('revision') == digest
                and old.get('software') == software
                and old.get('healthy')
                and healthy
            )
            report = {
                'identity': identity,
                'revision': digest,
                'effective': effective,
                'software': software,
                'healthy': healthy,
                'seen': now,
                'since': old['since'] if same else now,
            }
            atomic_write(path, json.dumps(report))
            self._promote(content, now)

    def _promote(self, content, now, probation=30):
        state = self.read_state()
        if state.get('needs_setup'):
            return
        digest = revision(content)
        try:
            if self.env.read_bytes() != content:
                return  # Never promote bytes that changed after the workers loaded them.
            reports = [
                json.loads((self.runtime / f'{role}.json').read_text())
                for role in ('core', 'web')
            ]
            if any(
                not r['healthy']
                or r['revision'] != digest
                or now - r['seen'] > 8
                or now - r['since'] < probation
                for r in reports
            ):
                return
            if reports[0].get('software') != reports[1].get('software'):
                return
            if not reports[0].get('effective') or reports[0].get(
                'effective'
            ) != reports[1].get('effective'):
                return  # Different inherited environment overrides are not one configuration.
            validate_bootstrap(content.decode(), complete=True)
        except (OSError, ValueError, KeyError, UnicodeError):
            return
        (self.directory / 'runtime-error').unlink(missing_ok=True)
        if state.get('good') == digest and (
            state.get('mode') != 'healthy'
            or state.get('rollback_used')
            or state.get('failures')
        ):
            # Restoring an already validated revision still needs to finish
            # recovery after both workers pass probation. Preserve the original
            # and reason as history, but stop showing an active recovery notice.
            self.state(mode='healthy', rollback_used=False, failures={})
        if state.get('good') != digest:
            atomic_write(self.directory / f'good-{digest}.env', content)
            atomic_write(
                self.directory / f'good-{digest}.json',
                json.dumps({'revision': digest, 'validated_at': now}),
            )
            self.state(good=digest, mode='healthy', failures={}, rollback_used=False)
            for _, old, _ in self.backups()[5:]:
                (self.directory / f'good-{old}.json').unlink(missing_ok=True)
                (self.directory / f'good-{old}.env').unlink(missing_ok=True)

    def failed(self, role, content):
        """One bounded rollback trial for a new, unapproved revision only."""
        with transaction(self.env):
            state = self.read_state()
            digest = revision(content)
            failures = state.get('failures', {})
            key = f'{role}:{digest}'
            failures[key] = failures.get(key, 0) + 1
            self.state(failures=dict(list(failures.items())[-10:]))
            if (
                failures[key] < 2
                or state.get('rollback_used')
                or state.get('good') == digest
                or any(backup[1] == digest for backup in self.backups())
                or state.get('needs_setup')
            ):
                return False
            # Do not undo an edit made while this worker was still running.
            if not self.env.exists() or self.env.read_bytes() != content:
                return False
            backups = [b for b in self.backups() if b[1] != digest]
            if not backups:
                return False
            original = self._quarantine(content)
            atomic_write(self.env, backups[0][2])
            self.state(
                rollback_used=True,
                mode='restored',
                original=original,
                reason=f'{role} failed twice with an unapproved configuration',
                restored_at=time.time(),
            )
            return True
