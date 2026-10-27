"""Unified Wi-Fi setup against a simulated NetworkManager, without a Pi."""

import ast
import contextlib
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import ROOT, definitions


SYSTEM = 'application/webconfig/app/routes/system.py'
HOTSPOT = 'Billy-Onboarding-Hotspot'
HELPERS = [
    '_is_wifi_onboarding_active',
    '_is_legacy_wifi_hotspot_onboarding',
    '_is_unified_wifi_hotspot_onboarding',
    '_set_wifi_onboarding_active',
    '_split_nmcli_terse_line',
    '_list_active_wifi_connections',
    '_get_active_wifi_connection',
    '_delete_wifi_connection',
    '_set_wifi_autoconnect',
    '_wifi_activation_command',
    '_run_wifi_activation',
    '_is_wifi_connection_active',
    '_activate_wifi_connection',
    '_cleanup_test_wifi_connections',
    '_set_wifi_country',
    '_stop_wifi_onboarding_services',
    '_restore_unified_wifi_onboarding_hotspot',
    '_friendly_wifi_error_message',
    '_wifi_connect',
    '_request_reboot',
    '_saved_wifi_connections',
    '_save_wifi_for_reboot',
    '_finish_wifi_save_and_reboot',
    'wifi_save',
    '_wifi_save',
]


def constants(*names):
    """Module-level literals exactly as system.py defines them."""
    tree = ast.parse((ROOT / SYSTEM).read_text())
    values = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
        ):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    assert set(values) == set(names)
    return values


class NetworkManager:
    """Joining or re-joining a network takes `join_seconds`, like brcmfmac
    leaving AP mode; NetworkManager completes it even if nmcli is killed."""

    def __init__(self, join_seconds, refuse=None):
        self.join_seconds = join_seconds
        self.refuse = refuse
        self.hang = None
        self.profiles = {HOTSPOT: {'ssid': 'Billy_Bassistant', 'autoconnect': 'no'}}
        self.active = HOTSPOT
        self.commands = []

    def __call__(self, args, *, check=False, timeout=20.0):
        self.commands.append(args)
        if self.hang and self.hang(args):
            raise subprocess.TimeoutExpired(args, timeout)
        argv = [a for a in args if a != 'sudo']
        if argv[:2] == ['nmcli', '--wait']:
            argv = ['nmcli', *argv[3:]]
        out = ''
        if argv[:1] != ['nmcli']:
            pass
        elif argv[1:] == [
            '-t',
            '-f',
            'NAME,TYPE,DEVICE',
            'connection',
            'show',
            '--active',
        ]:
            out = f'{self.active}:802-11-wireless:wlan0\n' if self.active else ''
        elif argv[1:] == ['-t', '-f', 'NAME', 'connection', 'show']:
            out = ''.join(f'{name}\n' for name in self.profiles)
        elif argv[1:] == ['-t', '-f', 'NAME,TYPE', 'connection', 'show']:
            out = ''.join(f'{name}:802-11-wireless\n' for name in self.profiles)
        elif argv[1:3] == ['connection', 'add']:
            name = argv[argv.index('con-name') + 1]
            self.profiles[name] = {
                'ssid': argv[argv.index('ssid') + 1],
                'autoconnect': 'yes',
            }
        elif argv[1:3] == ['connection', 'delete']:
            self.profiles.pop(argv[3], None)
            if self.active == argv[3]:
                self.active = None
        elif argv[1:3] == ['connection', 'modify']:
            name, changes = argv[3], dict(zip(argv[4::2], argv[5::2]))
            profile = self.profiles[name]
            if 'connection.id' in changes:
                self.profiles[changes['connection.id']] = self.profiles.pop(name)
                if self.active == name:
                    self.active = changes['connection.id']
            if 'connection.autoconnect' in changes:
                profile['autoconnect'] = changes['connection.autoconnect']
            if 'connection.autoconnect-priority' in changes:
                profile['priority'] = int(changes['connection.autoconnect-priority'])
        elif argv[1:3] == ['connection', 'up']:
            if self.profiles.get(argv[3], {}).get('ssid') == self.refuse:
                self.active = None
                return subprocess.CompletedProcess(
                    args,
                    4,
                    '',
                    'Error: Connection activation failed: Secrets were required',
                )
            self.active = argv[3]
            if timeout < self.join_seconds:
                raise subprocess.TimeoutExpired(args, timeout)
        elif argv[1:3] == ['connection', 'down']:
            if self.active != argv[3]:
                return subprocess.CompletedProcess(
                    args, 10, '', 'not an active connection'
                )
            self.active = None
        elif argv[1:3] == ['device', 'disconnect']:
            self.active = None
        return subprocess.CompletedProcess(args, 0, out, '')


@pytest.fixture
def wifi(tmp_path):
    def build(join_seconds=30, refuse=None, after_client=False):
        nm = NetworkManager(join_seconds, refuse)
        rebooted = []
        threads = []
        flag = tmp_path / '.wifi_onboarding_active'
        flag.touch()
        body = {}
        namespace = {
            **constants(
                'WIFI_TEST_PREFIX',
                'WIFI_UNIFIED_HOTSPOT_CON_NAME',
                'WIFI_UNIFIED_HOTSPOT_SSID',
                'WIFI_ACTIVATION_WAIT_SECONDS',
            ),
            'WIFI_ONBOARDING_FLAG': flag,
            'WIFI_CAPTIVE_PORTAL_DNSMASQ_CONF': '/etc/dnsmasq.d/x.conf',
            'WIFI_NM_CAPTIVE_PORTAL_DNSMASQ_CONF': '/etc/NetworkManager/x.conf',
            'ENV_PATH': str(tmp_path / '.env'),
            'Path': Path,
            'subprocess': subprocess,
            'uuid': uuid,
            'time': SimpleNamespace(sleep=lambda _: None, monotonic=lambda: 0.0),
            'contextlib': contextlib,
            'setup_follows_client': lambda: after_client,
            'threading': SimpleNamespace(
                Thread=lambda target, args=(), **_: SimpleNamespace(
                    start=lambda: threads.append((target, args))
                )
            ),
            'logger': Mock(),
            'set_env_key': Mock(),
            'core_config': SimpleNamespace(
                WIFI_ONBOARDING_MODE='unified', WIFI_COUNTRY='NL'
            ),
            'request': SimpleNamespace(get_json=lambda silent=True: body),
            'jsonify': lambda payload: payload,
            'bp': SimpleNamespace(route=lambda *a, **k: lambda fn: fn),
            '_run_wifi_command': nm,
        }
        routes = definitions(SYSTEM, HELPERS, namespace)
        namespace['_request_reboot'] = lambda: rebooted.append(nm.active)

        def save(**data):
            body.clear()
            body.update(data)
            result = routes.wifi_save()
            return result if isinstance(result, tuple) else (result, 200)

        def finish():
            """Run what the save left for after the answer was sent."""
            while threads:
                target, args = threads.pop(0)
                target(*args)

        return SimpleNamespace(
            nm=nm, flag=flag, save=save, finish=finish, rebooted=rebooted
        )

    return build


def test_slow_join_from_the_setup_hotspot_finishes_setup_instead_of_reopening_it(wifi):
    w = wifi(join_seconds=30)
    response, status = w.save(ssid='Home', password='secret', country='NL')
    assert status == 200 and response['ok'], response
    assert w.nm.active == 'Home'
    assert w.nm.profiles['Home'] == {
        'ssid': 'Home',
        'autoconnect': 'yes',
        'priority': 100,
    }
    assert not w.flag.exists()
    assert not any(name.startswith('billy-test-') for name in w.nm.profiles)
    # The joined network is not dropped and re-joined just to rename it.
    ups = [c for c in w.nm.commands if 'up' in c]
    assert len(ups) == 1
    assert ['sudo', 'nmcli', 'connection', 'up', HOTSPOT] not in w.nm.commands


def test_setup_hotspot_never_gains_autoconnect(wifi):
    w = wifi(join_seconds=1)
    w.nm.profiles[HOTSPOT]['autoconnect'] = 'yes'  # left behind by an older save
    response, _ = w.save(ssid='Home', password='secret', country='NL')
    assert response['ok']
    assert w.nm.profiles[HOTSPOT]['autoconnect'] == 'no'


def test_a_network_that_cannot_be_joined_reopens_the_setup_hotspot(wifi):
    w = wifi(join_seconds=1, refuse='Home')
    response, status = w.save(ssid='Home', password='wrong', country='NL')
    assert status == 400
    assert 'password' in response['error']
    assert w.flag.exists()
    assert w.nm.active == HOTSPOT
    assert 'Home' not in w.nm.profiles
    assert w.nm.profiles[HOTSPOT]['autoconnect'] == 'no'


def test_an_error_after_billy_joined_still_finishes_setup(wifi):
    w = wifi(join_seconds=1)
    joined = []
    # Fail on the first housekeeping call once the home network is up.
    w.nm.hang = lambda args: (
        w.nm.active == 'Home' and 'iw' in args and not joined.append(1)
    )
    response, status = w.save(ssid='Home', password='secret', country='NL')
    assert status == 200 and response['ok'], response
    assert response['warnings'], 'the error is reported, not hidden'
    assert w.nm.active == 'Home'
    assert w.nm.profiles['Home']['autoconnect'] == 'yes'
    assert not w.flag.exists()


def assert_saved_for_the_next_boot(w, response, status):
    assert status == 200 and response['ok'] and response['rebooting'], response
    # Nothing is joined now; the chip only joins after a reboot.
    assert not [c for c in w.nm.commands if 'up' in c]
    assert not w.rebooted
    w.finish()
    assert w.rebooted
    assert w.nm.profiles['Home'] == {
        'ssid': 'Home',
        'autoconnect': 'yes',
        'priority': 100,
    }
    assert w.nm.profiles[HOTSPOT]['autoconnect'] == 'no'
    assert not any(name.startswith('billy-') for name in w.nm.profiles)


def test_saving_after_the_setup_button_reboots_into_the_network(wifi):
    """Home network -> 10-second button hotspot -> home network: the router
    turns Billy away (status 16) until he reboots."""
    w = wifi(join_seconds=1, after_client=True)
    w.nm.profiles['Home'] = {'ssid': 'Home', 'autoconnect': 'yes', 'priority': 100}
    response, status = w.save(ssid='Home', password='secret-pass', country='NL')
    assert_saved_for_the_next_boot(w, response, status)


def test_switching_networks_reboots_and_keeps_the_old_one_as_fallback(wifi):
    w = wifi(join_seconds=1)
    w.nm.profiles['Old'] = {'ssid': 'Old', 'autoconnect': 'yes', 'priority': 100}
    w.nm.active = 'Old'
    w.flag.unlink()
    response, status = w.save(ssid='Home', password='secret-pass', country='NL')
    # Billy stays on the old network until the answer reached the browser.
    assert w.nm.active == 'Old'
    assert_saved_for_the_next_boot(w, response, status)
    assert w.nm.profiles['Old'] == {'ssid': 'Old', 'autoconnect': 'yes', 'priority': 0}


def test_resaving_the_current_network_replaces_its_profile(wifi):
    w = wifi(join_seconds=1)
    w.nm.profiles['Home'] = {'ssid': 'Home', 'autoconnect': 'yes', 'priority': 100}
    w.nm.active = 'Home'
    w.flag.unlink()
    response, status = w.save(ssid='Home', password='new-secret', country='NL')
    assert w.nm.active == 'Home'
    assert_saved_for_the_next_boot(w, response, status)
    adds = [c for c in w.nm.commands if c[2:4] == ['connection', 'add']]
    assert adds[0][adds[0].index('wifi-sec.psk') + 1] == 'new-secret'


def test_a_password_that_cannot_be_right_is_refused_before_rebooting(wifi):
    w = wifi(join_seconds=1, after_client=True)
    response, status = w.save(ssid='Home', password='short', country='NL')
    assert status == 400 and 'password' in response['error']
    w.finish()
    assert not w.rebooted
    assert w.nm.active == HOTSPOT
    assert set(w.nm.profiles) == {HOTSPOT}


def test_a_hotspot_opened_at_boot_still_joins_without_rebooting(wifi):
    w = wifi(join_seconds=1)
    response, status = w.save(ssid='Home', password='secret-pass', country='NL')
    assert status == 200 and response['ok'] and not response.get('rebooting')
    w.finish()
    assert not w.rebooted
    assert w.nm.active == 'Home'
