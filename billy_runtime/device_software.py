"""Device software: the code of an extension device, delivered as one file.

The built-in device (the fish) needs none. Other hardware runs Billy with its
device software installed in ``<install root>/private/<name>`` (core.devices
loads it from there). Software updates replace the release code, never that
folder.

A package is a ``.tar.gz`` holding exactly one folder, ``<name>/``: a Python
package with an ``__init__.py`` and a ``device.json``::

    {"name": "<name>", "title": "Shown name", "version": "1.2.0",
     "core": "2.8.0"}

``core`` is the oldest Billy version the device software works with. One
device per Billy: a package for a different device than the installed one is
refused. Installing keeps the replaced version in
``<install root>/private/.previous/<name>``.
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
import uuid
from pathlib import Path

from .paths import DEVICE_NAME, devices_root, installed_devices


MANIFEST = 'device.json'
MAX_FILES = 5000
MAX_UNPACKED_BYTES = 256 * 1024**2
VERSION = re.compile(r'\d+\.\d+\.\d+[0-9A-Za-z.+-]*')


def _numbers(version):
    """(2, 8, 0) for "2.8.0", "v2.8.0-beta.3"; None when it has no numbers."""
    parts = re.findall(r'\d+', str(version or ''))[:3]
    return (
        tuple(int(part) for part in parts) + (0,) * (3 - len(parts)) if parts else None
    )


def _manifest(data, name=None):
    if not isinstance(data, dict):
        raise ValueError('device.json is not valid')
    found = str(data.get('name') or '')
    version = str(data.get('version') or '')
    if not DEVICE_NAME.fullmatch(found) or found.lower() == 'fish':
        raise ValueError('device.json has no valid device name')
    if name is not None and found != name:
        raise ValueError('device.json does not match the package folder')
    if not VERSION.fullmatch(version):
        raise ValueError('device.json has no valid version')
    core = str(data.get('core') or '')
    if core and _numbers(core) is None:
        raise ValueError('device.json has no valid Billy version')
    return {
        'name': found,
        'title': str(data.get('title') or found.replace('_', ' ').title())[:60],
        'version': version,
        'core': core,
    }


def installed(root=None):
    """The installed device software ({name, title, version}), or None."""
    names = installed_devices(root)
    if not names:
        return None
    name = names[0]
    try:
        data = json.loads((devices_root(root) / name / MANIFEST).read_text())
        return _manifest(data, name)
    except (OSError, ValueError):
        # Installed by hand, without a manifest.
        return {'name': name, 'title': name.title(), 'version': '', 'core': ''}


def _members(bundle):
    """The package folder's name and members; rejects anything unsafe."""
    members = bundle.getmembers()
    if not members or len(members) > MAX_FILES:
        raise ValueError('This is not a device software file')
    tops = set()
    total = 0
    for member in members:
        parts = Path(member.name).parts
        if (
            not parts
            or member.name.startswith('/')
            or '..' in parts
            or not (member.isdir() or member.isfile())
        ):
            raise ValueError('The device software file contains unsafe entries')
        tops.add(parts[0])
        total += member.size
    if len(tops) != 1 or total > MAX_UNPACKED_BYTES:
        raise ValueError('This is not a device software file')
    return tops.pop(), members


def read(path):
    """The manifest of a package file; raises ValueError if it is not one."""
    try:
        with tarfile.open(path, 'r:gz') as bundle:
            name, members = _members(bundle)
            names = {member.name.rstrip('/') for member in members}
            if f'{name}/__init__.py' not in names or f'{name}/{MANIFEST}' not in names:
                raise ValueError('This is not a device software file')
            with bundle.extractfile(f'{name}/{MANIFEST}') as source:
                data = json.loads(source.read(64 * 1024))
    except (tarfile.TarError, OSError, EOFError, UnicodeError) as exc:
        raise ValueError('This is not a device software file') from exc
    except json.JSONDecodeError as exc:
        raise ValueError('device.json is not valid') from exc
    return _manifest(data, name)


def check(manifest, root=None, core_version=None):
    """Raise ValueError when this Billy cannot take the package."""
    other = [name for name in installed_devices(root) if name != manifest['name']]
    if other:
        raise ValueError(
            f'This Billy already has device software for {other[0]}; '
            'it can have one device at a time'
        )
    needed, running = _numbers(manifest['core']), _numbers(core_version)
    # A development checkout has no version: nothing to compare.
    if needed and running and running < needed:
        raise ValueError(
            f"{manifest['title']} {manifest['version']} needs Billy "
            f"{manifest['core']} or newer. Update Billy first"
        )


def _extract(path, destination):
    with tarfile.open(path, 'r:gz') as bundle:
        _, members = _members(bundle)
        for member in members:
            target = destination / member.name
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('The device software file contains unsafe entries')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open('xb') as copy:
                shutil.copyfileobj(source, copy)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def install(path, root=None, core_version=None):
    """Install the package file at ``path``; returns its manifest.

    The new folder is unpacked next to the old one and swapped in with
    renames, so the installed device software is never half replaced.
    """
    manifest = read(path)
    check(manifest, root, core_version)
    base = devices_root(root)
    base.mkdir(parents=True, exist_ok=True)
    staging = base / f'.incoming-{uuid.uuid4().hex}'
    staging.mkdir()
    try:
        _extract(path, staging)
        target = base / manifest['name']
        previous = base / '.previous' / manifest['name']
        if target.exists():
            previous.parent.mkdir(exist_ok=True)
            if previous.exists():
                shutil.rmtree(previous)
            target.rename(previous)
        try:
            (staging / manifest['name']).rename(target)
        except OSError:
            if previous.exists() and not target.exists():
                previous.rename(target)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return manifest
