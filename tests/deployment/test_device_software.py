"""Installing device software from a package file."""

import io
import json
import tarfile

import pytest

from billy_runtime import device_software


def package(tmp_path, files=None, *, manifest=None, name='demo'):
    files = dict(files or {})
    if manifest is not False:
        files.setdefault(
            f'{name}/device.json',
            json.dumps(
                manifest
                or {'name': name, 'title': 'Demo', 'version': '1.2.0', 'core': '2.8.0'}
            ),
        )
    files.setdefault(f'{name}/__init__.py', 'DEVICE = None\n')
    path = tmp_path / 'demo.tar.gz'
    with tarfile.open(path, 'w:gz') as bundle:
        for member, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(member)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return path


def test_installs_and_keeps_the_previous_version(tmp_path):
    root = tmp_path / 'root'
    first = package(tmp_path, {'demo/old.py': 'old\n'})
    assert device_software.install(first, root, '2.8.0')['version'] == '1.2.0'
    assert (root / 'private/demo/old.py').read_text() == 'old\n'

    second = package(
        tmp_path,
        {'demo/new.py': 'new\n'},
        manifest={'name': 'demo', 'version': '1.3.0'},
    )
    device_software.install(second, root, '2.8.0')
    assert (root / 'private/demo/new.py').exists()
    assert not (root / 'private/demo/old.py').exists()
    assert (root / 'private/.previous/demo/old.py').exists()
    assert device_software.installed(root) == {
        'name': 'demo',
        'title': 'Demo',
        'version': '1.3.0',
        'core': '',
    }
    # Only the installed device; no staging folders left behind.
    assert sorted(p.name for p in (root / 'private').iterdir()) == [
        '.previous',
        'demo',
    ]


@pytest.mark.parametrize(
    'files',
    [{'demo/../evil.py': 'x'}, {'/abs.py': 'x'}, {'other/x.py': 'x'}],
)
def test_unsafe_or_mixed_packages_are_refused(tmp_path, files):
    with pytest.raises(ValueError):
        device_software.install(package(tmp_path, files), tmp_path / 'root')
    assert not (tmp_path / 'root/private/demo').exists()


def test_links_are_refused(tmp_path):
    path = package(tmp_path)
    with tarfile.open(path, 'r:gz') as src:
        members = [(m, src.extractfile(m).read() if m.isfile() else None) for m in src]
    with tarfile.open(path, 'w:gz') as bundle:
        for member, data in members:
            bundle.addfile(member, io.BytesIO(data) if data is not None else None)
        link = tarfile.TarInfo('demo/link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        bundle.addfile(link)
    with pytest.raises(ValueError, match='unsafe'):
        device_software.install(path, tmp_path / 'root')


@pytest.mark.parametrize(
    'manifest, message',
    [
        (False, 'not a device software file'),
        ({'name': 'other', 'version': '1.0.0'}, 'does not match'),
        ({'name': 'demo', 'version': 'one'}, 'valid version'),
        ({'name': 'fish', 'version': '1.0.0'}, 'device name'),
    ],
)
def test_manifest_is_required_and_checked(tmp_path, manifest, message):
    name = 'fish' if manifest and manifest.get('name') == 'fish' else 'demo'
    path = package(tmp_path, manifest=manifest, name=name)
    with pytest.raises(ValueError, match=message):
        device_software.install(path, tmp_path / 'root')


def test_not_a_package_at_all(tmp_path):
    path = tmp_path / 'song.mp3'
    path.write_bytes(b'ID3 not a tar')
    with pytest.raises(ValueError, match='not a device software file'):
        device_software.read(path)


def test_needs_a_new_enough_billy(tmp_path):
    path = package(tmp_path)
    with pytest.raises(ValueError, match='needs Billy 2.8.0 or newer'):
        device_software.install(path, tmp_path / 'root', 'v2.7.4')
    # Pre-releases of that version and development checkouts are fine.
    device_software.install(path, tmp_path / 'root', 'v2.8.0-beta.3')
    device_software.install(path, tmp_path / 'root', 'unknown')


def test_one_device_per_billy(tmp_path):
    root = tmp_path / 'root'
    (root / 'private/other').mkdir(parents=True)
    (root / 'private/other/__init__.py').write_text('')
    with pytest.raises(ValueError, match='already has device software for other'):
        device_software.install(package(tmp_path), root)


def test_nothing_installed_is_the_fish(tmp_path):
    assert device_software.installed(tmp_path) is None
    (tmp_path / 'private/handmade').mkdir(parents=True)
    (tmp_path / 'private/handmade/__init__.py').write_text('')
    assert device_software.installed(tmp_path)['title'] == 'Handmade'


def device_app(root, version='v2.8.0'):
    import uuid
    from pathlib import Path
    from unittest.mock import Mock

    from conftest import definitions
    from flask import Blueprint, Flask, jsonify, request

    app = Flask('test-device-software')
    bp = Blueprint('software', __name__)
    definitions(
        'application/webconfig/app/routes/system.py',
        ['device_software_status', 'install_device_software'],
        {
            'bp': bp,
            'jsonify': jsonify,
            'request': request,
            'PROJECT_ROOT': root,
            'Path': Path,
            'uuid': uuid,
            'logger': Mock(),
            'get_current_version': lambda: version,
            'DEVICE_SOFTWARE_MAX_BYTES': 64 * 1024**2,
        },
    )
    app.register_blueprint(bp)
    return app.test_client()


def test_upload_route_installs_and_reports(tmp_path):
    root = tmp_path / 'root'
    client = device_app(root)
    assert client.get('/software/device').json == {'installed': None}
    with package(tmp_path).open('rb') as file:
        data = {'file': (file, 'demo.tar.gz')}
        # Like every software change: only from Billy's own page.
        assert client.post('/software/device', data=data).status_code == 403
    with package(tmp_path).open('rb') as file:
        response = client.post(
            '/software/device',
            data={'file': (file, 'demo.tar.gz')},
            headers={'X-Billy-Update': '1'},
        )
    assert response.status_code == 200
    assert response.json['installed']['version'] == '1.2.0'
    assert client.get('/software/device').json['installed']['title'] == 'Demo'
    # The upload itself is not kept.
    assert not list((root / 'state').glob('device-software-*'))


def test_upload_route_explains_a_refusal(tmp_path):
    client = device_app(tmp_path / 'root', version='v2.7.0')
    with package(tmp_path).open('rb') as file:
        response = client.post(
            '/software/device',
            data={'file': (file, 'demo.tar.gz')},
            headers={'X-Billy-Update': '1'},
        )
    assert response.status_code == 400
    assert (
        response.json['error']
        == 'Demo 1.2.0 needs Billy 2.8.0 or newer. Update Billy first.'
    )
