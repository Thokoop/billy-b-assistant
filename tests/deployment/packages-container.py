"""Real APT integration test. ONLY run in a disposable root Docker container.

Uses locally built test packages; never changes Billy's installed prerequisites.
Systemd/Bluetooth hardware hooks are replaced, package resolution/apply/repair are real.
"""

import functools
import hashlib
import http.server
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from billy_runtime import system_packages as packages  # noqa: E402


def main():
    if (
        not Path('/.dockerenv').exists()
        or os.getuid() != 0
        or os.environ.get('BILLY_PACKAGE_TEST_CONTAINER') != '1'
    ):
        raise SystemExit(
            'Disposable root container and BILLY_PACKAGE_TEST_CONTAINER=1 required.'
        )
    assert platform.freedesktop_os_release()['VERSION_CODENAME'] in {
        'bookworm',
        'trixie',
    }
    assert packages.run(['dpkg', '--print-architecture']).strip() == 'arm64'
    name = 'billy-ci-transaction-probe'
    subprocess.run(
        ['dpkg-query', '-W', name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert name not in packages.inventory(), 'Use a fresh disposable container.'
    with tempfile.TemporaryDirectory(prefix='billy-apt-test-') as temporary:
        base = Path(temporary)
        base.chmod(0o755)
        repo = base / 'repository'
        repo.mkdir()
        root = base / 'installed'
        root.mkdir()
        source = base / 'source'
        (source / 'dependencies').mkdir(parents=True)
        config = root / '.env'
        config.write_text('EXISTING_CONFIG=preserved\n')
        (root / 'web').mkdir()
        (root / 'web/index.html').write_text('Recovery stays available')
        server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            functools.partial(
                http.server.SimpleHTTPRequestHandler, directory=str(root / 'web')
            ),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/'

        def reachable():
            with urllib.request.urlopen(url, timeout=2) as response:
                assert response.read() == b'Recovery stays available'
            assert config.read_text() == 'EXISTING_CONFIG=preserved\n'

        gate = base / 'allow-configure'

        def publish(version, fail=False):
            build = base / ('build-' + version)
            (build / 'DEBIAN').mkdir(parents=True)
            (build / 'DEBIAN/control').write_text(
                f'Package: {name}\nVersion: {version}\nArchitecture: all\nMaintainer: Billy CI <ci@example.invalid>\nDescription: Disposable package transaction test\n'
            )
            if fail:
                postinst = build / 'DEBIAN/postinst'
                postinst.write_text(f'#!/bin/sh\n[ -f "{gate}" ] || exit 17\nexit 0\n')
                postinst.chmod(0o755)
            deb = repo / f'{name}_{version}_all.deb'
            subprocess.run(
                ['dpkg-deb', '--build', str(build), str(deb)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            fields = subprocess.check_output(
                ['dpkg-deb', '--field', str(deb)], text=True
            )
            (repo / 'Packages').write_text(
                fields
                + f'Filename: ./{deb.name}\nSize: {deb.stat().st_size}\nSHA256: {hashlib.sha256(deb.read_bytes()).hexdigest()}\n\n'
            )

        apt_source = Path('/etc/apt/sources.list.d/billy-ci-transaction.list')
        repo_server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0),
            functools.partial(
                http.server.SimpleHTTPRequestHandler, directory=str(repo)
            ),
        )
        threading.Thread(target=repo_server.serve_forever, daemon=True).start()
        apt_source.write_text(
            f'deb [trusted=yes] http://127.0.0.1:{repo_server.server_port}/ ./\n'
        )
        manifest = {
            'schema': 1,
            'platforms': [packages.host()],
            'runtime': [name],
            'build': [],
            'bluetooth': [],
            'onboarding': [],
        }

        def save_manifest():
            (source / 'dependencies/system-packages.json').write_text(
                json.dumps(manifest)
            )

        calls = []
        real_run = packages.run
        real_subprocess_run = subprocess.run

        def command(args, **kwargs):
            calls.append(args)
            reachable()
            # Containers run as root and have no systemd. Only these boundaries
            # are replaced; apt-get, dpkg-query and dpkg --configure are real.
            if args[0] == 'systemctl':
                return ''
            kwargs['mutate'] = False
            return real_run(args, **kwargs)

        def systemd_probe(args, **kwargs):
            if args[0] == 'systemctl':
                return subprocess.CompletedProcess(args, 0)
            return real_subprocess_run(args, **kwargs)

        try:
            publish('1.0')
            save_manifest()
            with (
                patch.object(packages, 'run', command),
                patch.object(packages.subprocess, 'run', systemd_probe),
                patch.object(packages, 'bluetooth_ready', return_value=True),
                patch.object(packages, 'configure_bluetooth'),
            ):
                packages.ensure(source, root, print)
                assert packages.inventory()[name] == {
                    'version': '1.0',
                    'status': 'installed',
                }
                assert packages.pending_transaction(root)['state'] == 'complete'
                assert packages.check(source, root)['state'] == 'ready'
                calls.clear()
                packages.ensure(source, root, print)
                assert not any(c[0] == 'apt-get' for c in calls), (
                    'Healthy installation must not run apt.'
                )
                publish('2.0', fail=True)
                manifest['migrations'] = [
                    {
                        'id': 'probe-v2',
                        'distribution': packages.host()['distribution'],
                        'compatible_with_previous': True,
                        'packages': {name: '2.0'},
                    }
                ]
                save_manifest()
                try:
                    packages.ensure(source, root, print)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('Post-install failure was not detected.')
                assert packages.pending_transaction(root)['state'] == 'interrupted'
                assert packages.inventory()[name]['status'] == 'half-configured'
                assert [c for c in calls if c[0] == 'systemctl'][-1] == [
                    'systemctl',
                    'start',
                    '--no-block',
                    'billy.service',
                ]
                reachable()
                gate.touch()
                packages.ensure(source, root, print, repair=True)
                assert packages.inventory()[name] == {
                    'version': '2.0',
                    'status': 'installed',
                }
                assert packages.check(source, root)['state'] == 'ready'
                assert packages.pending_transaction(root)['state'] == 'resolved'
                reachable()
            print(
                'PASS: real APT install, exact upgrade, interrupted dpkg repair, no-op, preserved config and independent HTTP server.'
            )
        finally:
            apt_source.unlink(missing_ok=True)
            repo_server.shutdown()
            repo_server.server_close()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
