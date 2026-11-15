"""Noise encryption of the Home Assistant satellite connection.

The handshake and transport are checked against a vector made with the
independent ``noiseprotocol`` library (which Home Assistant's aioesphomeapi
uses), then the server's key lifecycle over loopback with a test initiator.
"""

import asyncio
import base64
import os
import stat
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import ROOT, definitions
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_ha_satellite import Recorder, until


@pytest.fixture
def sat(isolated, monkeypatch):
    package = ModuleType('isolated_core.ha_satellite')
    package.__path__ = [str(ROOT / 'application/core/ha_satellite')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in (
        'wire',
        'noise',
        'keystore',
        'messages',
        'server',
        'discovery',
        'service',
    ):
        setattr(package, name, isolated.load(f'ha_satellite.{name}'))
    package.logger = isolated.logger
    return package


# Made with noiseprotocol 0.3.1 (aioesphomeapi's Noise library): psk bytes
# 0..31, initiator ephemeral 0x11 * 32, responder ephemeral 0x22 * 32.
PSK = bytes(range(32))
VECTOR = {
    'm1': '7b4e909bbe7ffe44c465a220037d608ee35897d31ef972f07f74892cb0f73f13'
    'd1bc1a38b6793685dc7e2ffdf8ea0933',
    'm2': '0faa684ed28867b97f4a6a2dee5df8ce974e76b7018e3f22a1c4cf2678570f20'
    '7336b95933b321b1ae86673f689f5c9f',
    'c1': '6048e4b82277d73654fbe3bd6b0c66ff1db182cba6ffc785b9344d',
    'c2': 'b12f5798e835e37d9c4637a619b5a35f0f7d20362605d226a79ec07c46aa201d6a619880',
    'c3': '307c59446bfaae281eabb6310c88339b4c9a9a0b27b2',
    'h': '9fbfe40f5a75a9ec6e8f7e6982afebb114835c543b8729c12c266990978dc80e',
}


def pair(noise, psk=PSK, responder_psk=None):
    initiator = noise.NoiseHandshake(
        psk,
        initiator=True,
        ephemeral=X25519PrivateKey.from_private_bytes(bytes([0x11] * 32)),
    )
    responder = noise.NoiseHandshake(
        responder_psk or psk,
        ephemeral=X25519PrivateKey.from_private_bytes(bytes([0x22] * 32)),
    )
    return initiator, responder


def test_handshake_and_transport_match_reference_vector(sat):
    initiator, responder = pair(sat.noise)
    m1 = initiator.write_message_1()
    assert m1.hex() == VECTOR['m1']
    assert responder.read_message_1(m1) == b''
    m2 = responder.write_message_2()
    assert m2.hex() == VECTOR['m2']
    initiator.read_message_2(m2)
    assert initiator.handshake_hash.hex() == responder.handshake_hash.hex()
    assert initiator.handshake_hash.hex() == VECTOR['h']

    i_send, i_receive = initiator.split()
    r_send, r_receive = responder.split()
    c1 = i_send.encrypt(b'hello billy')
    assert c1.hex() == VECTOR['c1']
    assert r_receive.decrypt(c1) == b'hello billy'
    c2 = r_send.encrypt(b'hello home assistant')
    assert c2.hex() == VECTOR['c2']
    assert i_receive.decrypt(c2) == b'hello home assistant'
    c3 = i_send.encrypt(b'second')  # nonce 1
    assert c3.hex() == VECTOR['c3']
    assert r_receive.decrypt(c3) == b'second'


def test_wrong_psk_fails_the_first_message(sat):
    initiator, responder = pair(sat.noise, responder_psk=bytes(32))
    with pytest.raises(sat.noise.NoiseError):
        responder.read_message_1(initiator.write_message_1())


def test_tampered_or_replayed_frames_are_rejected(sat):
    initiator, responder = pair(sat.noise)
    responder.read_message_1(initiator.write_message_1())
    initiator.read_message_2(responder.write_message_2())
    send, _ = initiator.split()
    _, receive = responder.split()
    first = send.encrypt(b'audio')
    tampered = bytes([first[0] ^ 1]) + first[1:]
    with pytest.raises(sat.noise.NoiseError):
        receive.decrypt(tampered)
    assert receive.decrypt(first) == b'audio'  # a failure consumes no nonce
    with pytest.raises(sat.noise.NoiseError):
        receive.decrypt(first)  # replay


@pytest.mark.parametrize(
    'text', ['', 'not base64!!', base64.b64encode(bytes(31)).decode(), 'AAAA']
)
def test_malformed_keys_are_rejected(sat, text):
    with pytest.raises(ValueError):
        sat.noise.decode_psk(text)


def test_key_store_is_private_and_atomic(sat, tmp_path):
    store = sat.keystore.KeyStore(tmp_path / 'state' / 'noise.key')
    assert store.get() is None and store.version() is None
    store.set(PSK)
    assert store.get() == PSK
    assert store.get_text() == base64.b64encode(PSK).decode()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    version = store.version()
    store.set(bytes(reversed(PSK)))
    assert store.version() != version
    store.path.write_text('garbage')
    # A damaged file fails closed: still "a key", one nobody has.
    assert store.get() is not None and store.damaged
    assert store.get_text() is None
    store.clear()
    store.clear()
    assert store.get() is None and store.version() is None


# ----- server over loopback ---------------------------------------------------


class NoiseClient:
    """The initiator side of ESPHome's Noise transport, as aioesphomeapi does it."""

    def __init__(self, sat, reader, writer, psk):
        self.sat, self.reader, self.writer = sat, reader, writer
        self.psk = psk
        self.send_cipher = self.receive_cipher = None

    @classmethod
    async def connect(cls, sat, port, psk):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        client = cls(sat, reader, writer, psk)
        return client, await client.handshake()

    async def handshake(self):
        w = self.sat.wire
        handshake = self.sat.noise.NoiseHandshake(self.psk, initiator=True)
        self.writer.write(
            b'\x01\x00\x00'
            + w.encode_noise_frame(b'\x00' + handshake.write_message_1())
        )
        server_hello = await w.read_noise_frame(self.reader)
        reply = await w.read_noise_frame(self.reader)
        if reply[0] != 0:
            return server_hello, reply[1:].decode()
        handshake.read_message_2(reply[1:])
        self.send_cipher, self.receive_cipher = handshake.split()
        return server_hello, None

    async def send(self, message_type, fields=()):
        w = self.sat.wire
        payload = w.encode_message(fields)
        header = int(message_type).to_bytes(2, 'big') + len(payload).to_bytes(2, 'big')
        self.writer.write(
            w.encode_noise_frame(self.send_cipher.encrypt(header + payload))
        )
        await self.writer.drain()

    async def recv(self, timeout=2.0):
        frame = await asyncio.wait_for(
            self.sat.wire.read_noise_frame(self.reader), timeout
        )
        message = self.receive_cipher.decrypt(frame)
        return int.from_bytes(message[:2], 'big'), self.sat.wire.decode_message(
            message[4:]
        )

    async def expect(self, message_type):
        received, fields = await self.recv()
        assert received == message_type, (received, fields)
        return fields

    async def hello(self):
        T = self.sat.messages.MessageType
        await self.send(T.HELLO_REQUEST, [(1, 'Home Assistant'), (2, 1), (3, 14)])
        return await self.expect(T.HELLO_RESPONSE)

    async def device_info(self):
        T = self.sat.messages.MessageType
        await self.send(T.DEVICE_INFO_REQUEST)
        return await self.expect(T.DEVICE_INFO_RESPONSE)


def identity(sat):
    return sat.messages.DeviceIdentity(
        name='billy', friendly_name='Billy', mac_address='02:00:00:00:00:01'
    )


def run_server(sat, tmp_path, scenario, monkeypatch=None):
    async def main():
        store = sat.keystore.KeyStore(tmp_path / 'noise.key')
        changes = []
        server = sat.server.SatelliteServer(
            identity(sat),
            Recorder(sat),
            host='127.0.0.1',
            port=0,
            key_store=store,
            on_key_changed=lambda: changes.append(store.get()),
        )
        await server.start()
        try:
            await scenario(server, store, changes)
        finally:
            await server.stop()

    asyncio.run(main())


def test_provisioning_then_encrypted_only(sat, tmp_path, monkeypatch):
    m, w = sat.messages, sat.wire
    T = m.MessageType
    new_key = bytes(range(100, 132))
    monkeypatch.setattr(sat.server, 'PLAINTEXT_GRACE_SECONDS', 0.05)

    async def scenario(server, store, changes):
        # Unprovisioned: plaintext works and offers the zero-key setup.
        from test_ha_satellite import FakeHomeAssistant

        plain = await FakeHomeAssistant.connect(sat, server)
        await plain.send(T.DEVICE_INFO_REQUEST)
        info = await plain.expect(T.DEVICE_INFO_RESPONSE)
        assert w.get_bool(info, 19) and w.get_bool(info, 26)
        # A key over plaintext would be readable on the network: refused.
        await plain.send(
            T.NOISE_ENCRYPTION_SET_KEY_REQUEST, [(1, base64.b64encode(new_key))]
        )
        assert not w.get_bool(
            await plain.expect(T.NOISE_ENCRYPTION_SET_KEY_RESPONSE), 1
        )
        assert store.get() is None

        # Zero-key session: name and MAC in the server hello, may only set a key.
        setup, (server_hello, error) = await NoiseClient.connect(
            sat, server.port, bytes(32)
        )
        assert error is None
        assert server_hello == b'\x01billy\x00020000000001\x00'
        await setup.hello()
        await setup.send(T.SUBSCRIBE_VOICE_ASSISTANT_REQUEST, [(1, True), (2, 4)])
        await setup.send(T.PING_REQUEST)
        await setup.expect(T.PING_RESPONSE)
        assert not server.voice_connected  # ignored on a setup session
        await setup.send(T.NOISE_ENCRYPTION_SET_KEY_REQUEST, [(1, b'AAAA')])
        assert not w.get_bool(
            await setup.expect(T.NOISE_ENCRYPTION_SET_KEY_RESPONSE), 1
        )
        await setup.send(
            T.NOISE_ENCRYPTION_SET_KEY_REQUEST, [(1, base64.b64encode(new_key))]
        )
        assert w.get_bool(await setup.expect(T.NOISE_ENCRYPTION_SET_KEY_RESPONSE), 1)
        assert store.get() == new_key and changes == [new_key]

        # The open plaintext session ends after the grace period.
        assert await plain.reader.read() == b''

        # Plaintext now gets a Noise frame ("requires encryption") and is closed.
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        writer.write(w.encode_frame(T.HELLO_REQUEST, b''))
        assert (await reader.read(1)) == b'\x01'
        assert await reader.read() is not None
        # The zero key and other keys fail with ESPHome's MAC failure text.
        for psk in (bytes(32), bytes(range(1, 33))):
            _, (_, error) = await NoiseClient.connect(sat, server.port, psk)
            assert error == 'Handshake MAC failure'

        # The real key: full service, including voice.
        client, (_, error) = await NoiseClient.connect(sat, server.port, new_key)
        assert error is None
        hello = await client.hello()
        assert w.get_str(hello, 4) == 'billy'
        info = await client.device_info()
        assert w.get_bool(info, 19) and not w.get_bool(info, 26)
        await client.send(T.SUBSCRIBE_VOICE_ASSISTANT_REQUEST, [(1, True), (2, 4)])
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await client.expect(T.VOICE_ASSISTANT_REQUEST)
        await client.send(T.VOICE_ASSISTANT_RESPONSE)
        await starting
        assert await server.send_audio(b'\x01\x02' * 512)
        audio = await client.expect(T.VOICE_ASSISTANT_AUDIO)
        assert w.get_bytes(audio, 1) == b'\x01\x02' * 512

    run_server(sat, tmp_path, scenario)


def test_reset_closes_keyed_sessions_and_reopens_setup(sat, tmp_path):
    T = sat.messages.MessageType
    key = bytes(range(32, 64))

    async def scenario(server, store, changes):
        store.set(key)
        client, (_, error) = await NoiseClient.connect(sat, server.port, key)
        assert error is None
        await client.hello()
        assert server.enforce_key() == 0  # nothing stale yet
        store.clear()
        assert server.enforce_key() == 1
        assert await client.reader.read() == b''
        setup, (_, error) = await NoiseClient.connect(sat, server.port, bytes(32))
        assert error is None
        await setup.hello()
        info = await setup.device_info()
        assert sat.wire.get_bool(info, 26)
        assert server.enforce_key() == 0  # setup sessions survive with no key
        await setup.send(T.PING_REQUEST)
        await setup.expect(T.PING_RESPONSE)

    run_server(sat, tmp_path, scenario)


def test_malformed_handshake_and_garbage_frames_close_the_session(sat, tmp_path):
    w = sat.wire

    async def scenario(server, store, changes):
        store.set(PSK)
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        writer.write(b'\x01\x00\x00' + w.encode_noise_frame(b'\x07junk'))
        await w.read_noise_frame(reader)  # server hello
        assert await reader.read() == b''

        client, (_, error) = await NoiseClient.connect(sat, server.port, PSK)
        assert error is None
        client.writer.write(w.encode_noise_frame(b'\x00' * 40))  # fails the AEAD tag
        assert await client.reader.read() == b''

    run_server(sat, tmp_path, scenario)


def test_without_key_store_noise_is_not_offered(sat):
    async def main():
        server = sat.server.SatelliteServer(
            identity(sat), Recorder(sat), host='127.0.0.1', port=0
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
            writer.write(b'\x01\x00\x00')
            assert await reader.read() == b''
            assert not server.encryption_required
        finally:
            await server.stop()

    asyncio.run(main())


# ----- discovery and service --------------------------------------------------


@pytest.mark.parametrize(
    ('state', 'record'),
    [
        (None, None),
        ('supported', b'api_encryption_supported=Noise_NNpsk0_25519_ChaChaPoly_SHA256'),
        ('required', b'api_encryption=Noise_NNpsk0_25519_ChaChaPoly_SHA256'),
    ],
)
def test_txt_records_announce_encryption_like_esphome(sat, state, record):
    records = sat.discovery.txt_records(identity(sat), 'Billy', state)
    encryption = [r for r in records if r.startswith(b'api_encryption')]
    assert encryption == ([record] if record else [])


def test_avahi_txt_update_marshals(sat):
    from dbus_next import Message, MessageType

    sent = []

    class Bus:
        async def call(self, message):
            message._marshall()
            sent.append(message)
            return Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)

    avahi = sat.discovery._Avahi(Bus())
    asyncio.run(avahi.update_txt('/G', 'billy', [b'api_encryption=x']))
    assert sent[0].member == 'UpdateServiceTxt'
    assert sent[0].signature == 'iiusssaay'


def test_service_applies_a_reset_and_republishes(sat, tmp_path):
    published = []

    class Advertiser:
        def __init__(self, identity, port, encryption=None):
            published.append(('start', encryption))

        async def start(self):
            return True

        async def update_encryption(self, encryption):
            published.append(('update', encryption))
            return True

        async def stop(self):
            pass

    store = sat.keystore.KeyStore(tmp_path / 'noise.key')
    store.set(PSK)
    service = sat.service.SatelliteService(
        identity(sat),
        host='127.0.0.1',
        port=0,
        advertiser_factory=Advertiser,
        key_store=store,
        key_poll_seconds=0.05,
    )
    assert service.start()
    try:
        assert service.encryption_state == 'required'
        store.clear()

        async def wait():
            await until(lambda: ('update', 'supported') in published)

        asyncio.run(wait())
    finally:
        service.stop()
    assert published[0] == ('start', 'required')
    assert service.encryption_state == 'supported'


def test_start_service_uses_the_installation_key_store(sat, monkeypatch, tmp_path):
    created = {}

    class Service:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.identity = SimpleNamespace(name='billy')
            self.bound_port = 6053

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(sat.service, 'SatelliteService', Service)
    monkeypatch.setattr(sat.service, '_service', None)
    config = sys.modules['isolated_core.config']
    config.REALTIME_AI_PROVIDER = 'home-assistant'
    config.HA_SATELLITE_PORT = 6053
    monkeypatch.setattr(
        sat.keystore, 'default_path', lambda: tmp_path / 'state' / 'noise.key'
    )
    sat.service.start_service()
    assert created['key_store'].path == tmp_path / 'state' / 'noise.key'
    sat.service.stop_service()


def test_device_identity_names_the_host_and_maker(sat, monkeypatch):
    monkeypatch.setattr(
        sat.service.os, 'uname', lambda: SimpleNamespace(nodename='billyphone.lan')
    )
    monkeypatch.setattr(sat.service, 'billy_version', lambda: 'v2.8.0')
    identity = sat.service.default_identity()
    assert identity.name == 'billyphone'
    assert identity.model == identity.friendly_name
    assert identity.manufacturer == 'ThingsFromThom'
    assert identity.project_name == f'ThingsFromThom.{identity.model}'
    assert identity.project_version == 'v2.8.0'
    # A dot would split Home Assistant's manufacturer.model parsing.
    dotted = sat.messages.DeviceIdentity('b', 'B', '02:00:00:00:00:01', model='a.b')
    assert dotted.project_name == 'ThingsFromThom.a-b'


def test_own_wake_word_is_advertised_only_when_listening(sat, monkeypatch):
    from core.wakeword import wake_word_phrase

    monkeypatch.setitem(
        sys.modules,
        'isolated_core.wakeword',
        SimpleNamespace(wake_word_phrase=wake_word_phrase),
    )
    config = sys.modules['isolated_core.config']
    config.WAKE_WORD_ENABLED = True
    config.WAKE_WORD_BACKEND = 'openwakeword'
    config.WAKE_WORD_OPENWAKEWORD_MODEL_PATH = 'hey-billy.onnx'
    config.WAKE_WORD_PORCUPINE_KEYWORD_PATH = 'hey-billy.ppn'
    assert sat.service.local_wake_words() == (
        sat.messages.WakeWord('hey_billy', 'Hey Billy'),
    )
    config.WAKE_WORD_ENABLED = False
    assert sat.service.local_wake_words() == ()


def test_billy_version_never_breaks_startup(sat, monkeypatch):
    import billy_runtime.updates as updates

    monkeypatch.setattr(
        updates, 'selection', Mock(side_effect=OSError('no release state'))
    )
    assert sat.service.billy_version() == ''


@pytest.mark.parametrize(
    ('provider', 'clip'),
    [('home-assistant', False), ('gpt-live', True), ('openai', True)],
)
def test_home_assistant_mode_skips_the_wake_up_clip(provider, clip):
    namespace = {'config': SimpleNamespace(REALTIME_AI_PROVIDER=provider)}
    button = definitions(
        'application/core/button.py', ['plays_wake_up_clip'], namespace
    )
    assert button.plays_wake_up_clip() is clip


@pytest.mark.parametrize(
    ('backend', 'porcupine', 'onnx', 'phrase'),
    [
        ('openwakeword', 'hey-billy.ppn', 'hey-billy.onnx', 'Hey Billy'),
        ('openwakeword', '', 'models/hey_jarvis_v0.1.onnx', 'Hey Jarvis'),
        ('porcupine', 'hey-billy_en_raspberry-pi_v3_0_0.ppn', '', 'Hey Billy'),
        ('porcupine', '', '', 'Wake word'),
    ],
)
def test_wake_word_phrase_from_the_configured_model(backend, porcupine, onnx, phrase):
    from core.wakeword import wake_word_phrase

    settings = SimpleNamespace(
        WAKE_WORD_BACKEND=backend,
        WAKE_WORD_PORCUPINE_KEYWORD_PATH=porcupine,
        WAKE_WORD_OPENWAKEWORD_MODEL_PATH=onnx,
    )
    assert wake_word_phrase(settings) == phrase


# ----- web UI -----------------------------------------------------------------


@pytest.fixture
def key_routes(sat, tmp_path):
    from unittest.mock import Mock

    from conftest import definitions
    from flask import Blueprint, Flask, jsonify, request

    store = sat.keystore.KeyStore(tmp_path / 'noise.key')
    bp = Blueprint('ha_keys', __name__)
    definitions(
        'application/webconfig/app/routes/system.py',
        [
            '_ha_satellite_key_state',
            'ha_satellite_encryption',
            'ha_satellite_encryption_key',
            'ha_satellite_encryption_reset',
        ],
        {
            'bp': bp,
            'jsonify': jsonify,
            'request': request,
            'logger': Mock(),
            '_ha_satellite_keys': lambda: store,
        },
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client(), store


def test_key_routes_report_reveal_and_reset(key_routes):
    client, store = key_routes
    assert client.get('/ha-satellite/encryption').json == {'state': 'none'}
    response = client.post('/ha-satellite/encryption/key', json={})
    assert response.status_code == 404
    store.set(PSK)
    assert client.get('/ha-satellite/encryption').json == {'state': 'set'}
    response = client.post('/ha-satellite/encryption/key', json={})
    assert response.json == {'key': base64.b64encode(PSK).decode()}
    assert response.headers['Cache-Control'] == 'no-store'
    # Only JSON posts: a cross-site form cannot reveal or reset the key.
    assert client.post('/ha-satellite/encryption/key', data='x').status_code == 415
    assert client.post('/ha-satellite/encryption/reset', data='x').status_code == 415
    assert store.get() == PSK
    assert client.post('/ha-satellite/encryption/reset', json={}).json == {
        'state': 'none'
    }
    assert store.get() is None
    store.path.write_text('damaged')
    assert client.get('/ha-satellite/encryption').json == {'state': 'damaged'}


def test_settings_show_encryption_without_saving_the_key():
    from flask import Flask, render_template

    app = Flask(__name__, template_folder=str(ROOT / 'application/webconfig/templates'))
    with app.test_request_context('/settings'):
        html = render_template(
            'components/settings-form.html',
            config={'REALTIME_AI_PROVIDER': 'home-assistant'},
            settings_category='voice',
        )
    panel = html.split('id="ha-satellite-encryption"', 1)[1].split(
        '</div>\n            </div>', 1
    )[0]
    assert (
        'id="ha-satellite-key-show"' in panel and 'id="ha-satellite-key-reset"' in panel
    )
    import re

    key_input = re.search(r'<input[^>]*id="ha-satellite-key"[^>]*>', panel)[0]
    assert ' name=' not in key_input and 'readonly' in key_input
    assert 'not encrypted yet' not in html


def section(html, section_id):
    """The markup of one settings category panel."""
    start = html.index(f'id="{section_id}"')
    following = html.find('data-settings-panel', html.index('>', start))
    return html[start : following if following != -1 else len(html)]


@pytest.mark.parametrize('provider', ['home-assistant', 'gpt-live'])
def test_satellite_settings_live_in_the_home_assistant_category(provider):
    from flask import Flask, render_template

    app = Flask(__name__, template_folder=str(ROOT / 'application/webconfig/templates'))
    with app.test_request_context('/settings/home-assistant'):
        html = render_template(
            'components/settings-form.html',
            config={'REALTIME_AI_PROVIDER': provider, 'HA_SATELLITE_PORT': '6053'},
            settings_category='home-assistant',
        )
    voice, ha_panel = section(html, 'section-voice'), section(html, 'section-ha')
    for element in ('HA_SATELLITE_PORT', 'ha-satellite-encryption', 'ha-assist-setup'):
        assert f'id="{element}"' in ha_panel
        assert f'id="{element}"' not in voice
    assert 'href="/settings/home-assistant"' in voice
    # Port and encryption only matter in Home Assistant mode; the port field
    # is still saved in other modes (one form, global save).
    hidden = 'hidden style="display:none"'
    satellite = ha_panel.split('id="ha-satellite-settings"', 1)[1].split('>', 1)[0]
    assert (hidden in satellite) is (provider != 'home-assistant')
    assert html.count('name="HA_SATELLITE_PORT"') == 1
    # Its own card in a sections panel, like Voice & AI (cards never nest).
    panel_tag = html[html.rindex('<div', 0, html.index('id="section-ha"')) :]
    assert 'settings-card--sections' in panel_tag.split('>', 1)[0]
    card_tag = ha_panel.split('id="ha-satellite-settings"', 1)[0].rsplit('<', 1)[1]
    assert card_tag.startswith('section class="card ')
