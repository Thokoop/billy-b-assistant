import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbus_next import Variant
from dbus_next.errors import DBusError
from flask import Flask


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[2] / relative
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bluez = load_module('bluez_test', 'application/core/bluetooth/bluez.py')
transport = load_module('transport_test', 'application/core/bluetooth/transport.py')
BluezController, PairingAgent = bluez.BluezController, bluez.PairingAgent
PCMFrames, bluetooth_nodes = transport.PCMFrames, transport.bluetooth_nodes
node_kind = transport.node_kind
command, select_route = transport.command, transport.select_route


def test_pcm_preserves_partial_samples_and_frame_boundaries():
    framer = PCMFrames(8)
    assert framer.feed(b'123') == []
    assert framer.feed(b'456789abcdef') == [b'12345678']
    assert framer.feed(b'g') == [b'9abcdefg']
    assert not framer.pending


def test_never_route_microphone_to_different_phone():
    nodes = [
        {'name': 'one', 'kind': 'Audio/Source', 'device': 'A'},
        {'name': 'two', 'kind': 'Audio/Sink', 'device': 'B'},
    ]
    with pytest.raises(RuntimeError, match='other phones'):
        select_route(nodes)
    nodes[1]['device'] = 'A'
    assert select_route(nodes) == ('one', 'two')
    assert select_route(nodes[:1]) == ('one', None)
    assert select_route([]) is None


def test_duplicate_sources_are_not_guessed():
    with pytest.raises(RuntimeError, match='Ambiguous'):
        select_route([
            {'name': n, 'kind': 'Audio/Source', 'device': 'A'}
            for n in ['first', 'second']
        ])


def test_discovery_excludes_local_mic_and_binds_device_identity():
    objects = [
        {
            'id': 10,
            'type': 'PipeWire:Interface:Device',
            'info': {'props': {'api.bluez5.address': 'AA:BB:CC:DD:EE:FF'}},
        },
        {
            'id': 11,
            'type': 'PipeWire:Interface:Node',
            'info': {
                'props': {
                    'node.name': 'bluez_input.phone',
                    'device.id': 10,
                    'media.class': 'Audio/Source',
                }
            },
        },
        {
            'id': 12,
            'type': 'PipeWire:Interface:Node',
            'info': {
                'props': {
                    'node.name': 'alsa_input.mic',
                    'device.id': 9,
                    'media.class': 'Audio/Source',
                }
            },
        },
    ]
    nodes = bluetooth_nodes(objects)
    assert len(nodes) == 1
    assert nodes[0]['device'] == 'AA:BB:CC:DD:EE:FF'
    # pw-cat has no --raw option (PipeWire 1.2.7 rejects it and exits); what it
    # writes to stdout for '-' is raw PCM already.
    assert '--raw' not in command(nodes[0]['name'], record=True)
    assert '--target=bluez_input.phone' in command(nodes[0]['name'], record=True)


def test_hfp_stream_classes_are_recognised():
    """HFP exposes Stream/* where A2DP exposes Audio/*; calls need both."""
    objects = [
        {
            'id': 72,
            'type': 'PipeWire:Interface:Device',
            'info': {'props': {'api.bluez5.address': 'AA:BB:CC:DD:EE:FF'}},
        },
        {
            'id': 80,
            'type': 'PipeWire:Interface:Node',
            'info': {
                'props': {
                    'node.name': 'bluez_input.AA_BB_CC_DD_EE_FF.0',
                    'device.id': 72,
                    'media.class': 'Stream/Output/Audio',
                }
            },
        },
        {
            'id': 81,
            'type': 'PipeWire:Interface:Node',
            'info': {
                'props': {
                    'node.name': 'bluez_output.AA_BB_CC_DD_EE_FF.1',
                    'device.id': 72,
                    'media.class': 'Stream/Input/Audio',
                }
            },
        },
    ]
    nodes = bluetooth_nodes(objects)
    assert {node['kind'] for node in nodes} == {'Audio/Source', 'Audio/Sink'}
    assert select_route(nodes) == (
        'bluez_input.AA_BB_CC_DD_EE_FF.0',
        'bluez_output.AA_BB_CC_DD_EE_FF.1',
    )


def test_node_direction_must_agree_with_media_class():
    """Never hand the phone's own ear to pw-cat as if it were a microphone."""
    assert node_kind('bluez_input.x.0', 'Stream/Input/Audio') is None
    assert node_kind('bluez_output.x.1', 'Stream/Output/Audio') is None
    assert node_kind('alsa_input.mic', 'Audio/Source') is None
    assert node_kind('bluez_input.x.2', 'Audio/Source') == 'Audio/Source'
    assert node_kind('bluez_output.x.3', 'Audio/Sink') == 'Audio/Sink'


def make_controller():
    manager = BluezController.__new__(BluezController)
    manager.loop = asyncio.get_running_loop()
    manager.pending = None
    manager.pairing_lock = asyncio.Lock()
    manager.timer = None
    manager.deadline = manager.loop.time() + 60
    manager.objects = AsyncMock(
        return_value={
            '/phone': {'org.bluez.Device1': {'Alias': Variant('s', 'Test phone')}}
        }
    )
    manager.set_property = AsyncMock()
    return manager


def test_pairing_confirms_on_phone_and_only_displays_code_locally():
    async def scenario():
        manager = make_controller()
        await manager.confirm('/phone', '000042')
        assert manager.pending['passkey'] == '000042'
        assert manager.pending['device'] == '/phone'
        manager.set_property.assert_not_awaited()  # no premature trust
        with pytest.raises(DBusError):
            await manager.confirm('/another', '111111')

    asyncio.run(scenario())


def test_closed_pairing_does_not_accept_or_trust_device():
    async def scenario():
        manager = make_controller()
        manager.deadline = 0
        with pytest.raises(DBusError):
            await manager.confirm('/phone', None)
        manager.set_property.assert_not_awaited()
        assert manager.pending is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    'paired,connected,closes',
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_pairing_closes_only_after_success(paired, connected, closes):
    async def scenario():
        manager = make_controller()
        manager.objects.return_value['/phone']['org.bluez.Device1'].update({
            'Paired': Variant('b', paired),
            'Connected': Variant('b', connected),
        })
        manager.adapter = AsyncMock(return_value='/adapter')
        await manager.confirm('/phone', '000042')
        await manager.finish_connected_pairing()
        assert (manager.deadline == 0) == closes
        if closes:
            assert manager.pending is None
            assert manager.set_property.await_count == 3
            manager.set_property.assert_any_await(
                '/phone', 'org.bluez.Device1', 'Trusted', 'b', True
            )
            manager.set_property.assert_any_await(
                '/adapter', 'org.bluez.Adapter1', 'Discoverable', 'b', False
            )
            manager.set_property.assert_any_await(
                '/adapter', 'org.bluez.Adapter1', 'Pairable', 'b', False
            )
        else:
            manager.set_property.assert_not_awaited()

    asyncio.run(scenario())


def test_stop_cancels_unfinished_pairing_and_timer():
    async def scenario():
        manager = make_controller()
        manager.adapter = AsyncMock(return_value='/adapter')
        device = SimpleNamespace(call_cancel_pairing=AsyncMock())
        manager.interface = AsyncMock(return_value=device)
        manager.timer = asyncio.create_task(asyncio.sleep(120))
        timer = manager.timer
        await manager.confirm('/phone', '000042')
        await manager.stop_pairing()
        await asyncio.sleep(0)
        device.call_cancel_pairing.assert_awaited_once()
        assert timer.cancelled()
        assert manager.pending is None
        assert manager.deadline == 0

    asyncio.run(scenario())


def test_connected_old_phone_does_not_close_another_pairing_request():
    async def scenario():
        manager = make_controller()
        await manager.confirm('/phone', '000042')
        manager.objects.return_value['/old'] = {
            'org.bluez.Device1': {
                'Paired': Variant('b', True),
                'Connected': Variant('b', True),
            }
        }
        await manager.finish_connected_pairing()
        assert manager.pending is not None
        manager.set_property.assert_not_awaited()

    asyncio.run(scenario())


def test_monitor_closes_pairing_without_ui_polling():
    async def scenario():
        manager = make_controller()
        manager.adapter = AsyncMock(return_value='/adapter')
        manager.objects.return_value['/phone']['org.bluez.Device1'].update({
            'Paired': Variant('b', True),
            'Connected': Variant('b', True),
        })
        manager.timer = asyncio.create_task(manager.expire())
        await asyncio.wait_for(manager.timer, 2)
        assert manager.deadline == 0

    asyncio.run(scenario())


def test_agent_has_correct_dbus_signatures():
    methods = {m.name: m for m in PairingAgent(None).introspect().methods}
    assert methods['RequestConfirmation'].in_signature == 'ou'
    assert methods['AuthorizeService'].in_signature == 'os'


@pytest.fixture
def web(monkeypatch):
    import sys

    monkeypatch.setitem(
        sys.modules,
        'core.bluetooth.provisioning',
        load_module(
            'provisioning_routes_test', 'application/core/bluetooth/provisioning.py'
        ),
    )
    path = Path(__file__).parents[2] / 'application/webconfig/app/routes/bluetooth.py'
    spec = importlib.util.spec_from_file_location('bluetooth_routes_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Normal route tests need a completed installation and no system D-Bus.
    # Individual setup tests override this fixture explicitly below.
    monkeypatch.setattr(
        sys.modules['core.bluetooth.provisioning'],
        'setup_status',
        lambda: {'state': 'ready'},
    )
    monkeypatch.setattr(
        module,
        'controller',
        lambda: SimpleNamespace(status=lambda: None, run=lambda _: {'devices': []}),
    )
    app = Flask(
        __name__,
        template_folder=str(
            Path(__file__).parents[2] / 'application/webconfig/templates'
        ),
    )
    app.register_blueprint(module.bp)
    return app.test_client(), module


def test_control_requires_page_token_and_valid_json(web):
    client, module = web
    assert client.post('/api/bluetooth/pair').status_code == 403
    headers = {'X-Bluetooth-Token': module._csrf}
    assert (
        client.post(
            '/api/bluetooth/pair', json=['invalid'], headers=headers
        ).status_code
        == 400
    )


def test_service_controls_are_gone(web):
    """The UI can no longer stop billy.service, which is where sessions live."""
    client, module = web
    headers = {'X-Bluetooth-Token': module._csrf}
    for action in ('test-start', 'test-stop'):
        response = client.post(f'/api/bluetooth/{action}', json={}, headers=headers)
        assert response.status_code == 404


def test_audio_status_is_read_from_the_session_not_from_systemd(web, monkeypatch):
    """Bluetooth audio is a mode inside billy.service, so there is no unit to poll."""
    client, module = web
    monkeypatch.setattr(
        module, 'audio_status', lambda: {'state': 'call_audio', 'aec': True}
    )
    monkeypatch.setattr(
        module,
        'controller',
        lambda: SimpleNamespace(status=lambda: None, run=lambda _c: {'devices': []}),
    )
    response = client.get('/api/bluetooth')
    assert response.json['audio']['state'] == 'call_audio'
    assert 'service' not in response.json


def test_audio_tests_are_refused_while_bluetooth_owns_the_hardware(web, monkeypatch):
    client, module = web
    monkeypatch.setattr(module, 'audio_status', lambda: {'state': 'call_audio'})
    assert client.post('/speaker-test').status_code == 409
    monkeypatch.setattr(module, 'audio_status', lambda: {'state': 'idle'})
    assert client.post('/speaker-test').status_code != 409


def test_old_pairing_page_redirects_to_system_settings(web):
    client, _ = web
    response = client.get('/bluetooth')
    assert response.status_code == 302
    assert response.headers['Location'] == '/settings#section-bluetooth'


def test_settings_section_renders_with_controls(web):
    from flask import render_template

    client, module = web
    with client.application.test_request_context():
        html = render_template('components/bluetooth-settings.html')
    assert 'Pair new device' in html
    assert 'Stop pairing' in html
    assert 'data-action="test-stop"' not in html
    assert module._csrf in html
    assert 'section-bluetooth' in html
    # Pairing is an action on the device list, not a separate step above it.
    assert html.index('id="bt-devices"') < html.index('data-action="pair"')
    # Stopping pairing and the hint only make sense once pairing is open, so
    # they start hidden and the script reveals them.
    assert 'id="bt-pair-stop"' in html
    assert html.count('hidden') >= 3


def test_aec_reference_matches_actual_speaker_pcm():
    import ast
    import time
    from unittest.mock import Mock

    import numpy as np

    tree = ast.parse(
        (Path(__file__).parents[2] / 'application/core/audio.py').read_text()
    )
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'render_external_pcm'
    )
    namespace = {
        'np': np,
        'time': time,
        'PLAYBACK_VOLUME': 0.5,
        'CHUNK_MS': 40,
        'voice_band': lambda samples: samples,
        'flap_from_pcm_chunk': Mock(),
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]),
            'application/core/audio.py',
            'exec',
        ),
        namespace,
    )
    speaker, aec = Mock(), Mock()
    namespace['render_external_pcm'](
        speaker, np.array([1000, -2000], dtype=np.int16), aec
    )
    written = speaker.write.call_args.args[0]
    reference = aec.feed_render.call_args.args[0]
    np.testing.assert_array_equal(written[:, 0], reference)
    np.testing.assert_array_equal(written[:, 1], reference)
    np.testing.assert_array_equal(reference, [500, -1000])
    assert namespace['flap_from_pcm_chunk'].call_args.kwargs['sample_rate'] == 48000
    # CHUNK_MS, not the frame length, so flap duration is not clamped shorter
    # than the song path's.
    assert namespace['flap_from_pcm_chunk'].call_args.kwargs['chunk_ms'] == 40


def test_voice_band_keeps_speech_and_drops_bass():
    """The flap signal stands in for a vocals stem, so music must not drive it."""
    import ast

    import numpy as np
    from scipy.signal import butter, sosfilt

    tree = ast.parse(
        (Path(__file__).parents[2] / 'application/core/audio.py').read_text()
    )
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'voice_band'
    )
    namespace = {
        'np': np,
        'butter': butter,
        'sosfilt': sosfilt,
        'VOICE_BAND_HZ': (300.0, 3400.0),
        '_voice_band_sos': None,
        '_voice_band_state': None,
    }
    exec(
        compile(
            ast.Module(body=[function], type_ignores=[]),
            'application/core/audio.py',
            'exec',
        ),
        namespace,
    )

    def rms(samples):
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

    def tone(hz):
        t = np.arange(48000) / 48000.0
        return (8000 * np.sin(2 * np.pi * hz * t)).astype(np.int16)

    def filtered_rms(samples):
        namespace['_voice_band_state'] = None
        return rms(namespace['voice_band'](samples))

    speech, bass, cymbal = tone(1000), tone(60), tone(8000)
    speech_out = filtered_rms(speech)

    assert speech_out > 0.7 * rms(speech)
    assert filtered_rms(bass) < 0.1 * speech_out
    assert filtered_rms(cymbal) < 0.1 * speech_out

    # Filter state carries across frames: 20 ms chunks must equal one long pass.
    namespace['_voice_band_state'] = None
    whole = namespace['voice_band'](speech)
    namespace['_voice_band_state'] = None
    chunked = np.concatenate([
        namespace['voice_band'](speech[i : i + 960]) for i in range(0, len(speech), 960)
    ])
    np.testing.assert_array_equal(whole, chunked)


def test_setup_status_does_not_touch_bluez_while_installing(web, monkeypatch):
    import sys
    from unittest.mock import Mock

    client, module = web
    setup = sys.modules['core.bluetooth.provisioning']
    monkeypatch.setattr(
        setup,
        'setup_status',
        lambda: {'state': 'installing', 'message': 'Installing Bluetooth support'},
    )
    controller = Mock(side_effect=AssertionError('BlueZ must not be accessed yet'))
    monkeypatch.setattr(module, 'controller', controller)
    response = client.get('/api/bluetooth')
    assert response.status_code == 200
    assert response.json['setup']['state'] == 'installing'
    controller.assert_not_called()
    response = client.post(
        '/api/bluetooth/pair',
        json={},
        headers={'X-Bluetooth-Token': module._csrf},
    )
    assert response.status_code == 409


def test_setup_retry_is_authenticated_and_runs_in_background(web, monkeypatch):
    from unittest.mock import Mock

    client, module = web
    from billy_runtime import updates

    start = Mock(return_value={'job': {'state': 'queued'}})
    monkeypatch.setattr(updates, 'queue', start)
    assert client.post('/api/bluetooth/setup-retry', json={}).status_code == 403
    start.assert_not_called()
    response = client.post(
        '/api/bluetooth/setup-retry',
        json={},
        headers={'X-Bluetooth-Token': module._csrf},
    )
    assert response.status_code == 202
    start.assert_called_once_with('packages')


@pytest.mark.parametrize(
    'paired,window,target,uuid,allowed',
    [
        (True, True, '/phone', '0000110a-0000-1000-8000-00805f9b34fb', True),
        (False, True, '/phone', '0000110a-0000-1000-8000-00805f9b34fb', False),
        (True, False, '/phone', '0000110a-0000-1000-8000-00805f9b34fb', False),
        (True, True, '/other', '0000110a-0000-1000-8000-00805f9b34fb', False),
        (True, True, '/phone', 'unsupported-service', False),
    ],
)
def test_automatic_pairing_authorizes_only_selected_paired_audio_device(
    paired, window, target, uuid, allowed
):
    async def scenario():
        manager = make_controller()
        manager.pending = {'device': target}
        if not window:
            manager.deadline = 0
        manager.objects.return_value['/phone']['org.bluez.Device1']['Paired'] = Variant(
            'b', paired
        )
        # Invoke the service coroutine behind dbus-next's exported method.
        method = next(
            m
            for m in PairingAgent(manager).introspect().methods
            if m.name == 'AuthorizeService'
        )
        assert method.in_signature == 'os'
        import inspect

        function = inspect.unwrap(PairingAgent.AuthorizeService)
        if allowed:
            await function(PairingAgent(manager), '/phone', uuid)
        else:
            with pytest.raises(DBusError):
                await function(PairingAgent(manager), '/phone', uuid)

    asyncio.run(scenario())


def test_auto_connect_endpoint_requires_token_and_persists_boolean(
    web, monkeypatch, tmp_path
):
    client, module = web
    monkeypatch.setattr(
        bluez.bluetooth_preferences, 'PATH', tmp_path / 'bluetooth.json'
    )
    manager = BluezController.__new__(BluezController)
    address = 'AA:BB:CC:DD:EE:FF'
    manager.objects = AsyncMock(
        return_value={
            '/phone': {
                'org.bluez.Device1': {
                    'Address': Variant('s', address),
                    'Paired': Variant('b', True),
                }
            }
        }
    )
    manager.run = asyncio.run
    monkeypatch.setattr(module, 'controller', lambda: manager)
    data = {'address': address, 'enabled': True}
    assert client.post('/api/bluetooth/auto-connect', json=data).status_code == 403
    headers = {'X-Bluetooth-Token': module._csrf}
    assert (
        client.post(
            '/api/bluetooth/auto-connect', json=data, headers=headers
        ).status_code
        == 200
    )
    assert bluez.bluetooth_preferences.selected() == [address]
    data['enabled'] = 'false'
    assert (
        client.post(
            '/api/bluetooth/auto-connect', json=data, headers=headers
        ).status_code
        == 400
    )
    assert bluez.bluetooth_preferences.selected() == [address]
    data['enabled'] = False
    assert (
        client.post(
            '/api/bluetooth/auto-connect', json=data, headers=headers
        ).status_code
        == 200
    )
    assert bluez.bluetooth_preferences.selected() == []


def test_select_route_requires_token_and_allows_time_for_teardown_and_connect(
    web, monkeypatch
):
    client, module = web
    calls = []

    def run(operation, *, timeout):
        calls.append((operation, timeout))
        return {'ok': True}

    manager = SimpleNamespace(
        device_action=lambda address, action: (address, action), run=run
    )
    monkeypatch.setattr(module, 'controller', lambda: manager)
    assert (
        client.post('/api/bluetooth/select', json={'address': 'AA'}).status_code == 403
    )
    assert calls == []
    response = client.post(
        '/api/bluetooth/select',
        json={'address': 'AA'},
        headers={'X-Bluetooth-Token': module._csrf},
    )
    assert response.status_code == 200
    assert calls == [(('AA', 'select'), 30)]
