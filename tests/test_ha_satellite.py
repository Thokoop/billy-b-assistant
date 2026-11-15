"""ESPHome-protocol Assist satellite against a fake Home Assistant client.

No Home Assistant, network beyond loopback, audio device or motor is used.
"""

import asyncio
import struct
import sys
from types import ModuleType

import pytest
from conftest import ROOT


@pytest.fixture
def sat(isolated, monkeypatch):
    package = ModuleType('isolated_core.ha_satellite')
    package.__path__ = [str(ROOT / 'application/core/ha_satellite')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    wire = isolated.load('ha_satellite.wire')
    messages = isolated.load('ha_satellite.messages')
    server = isolated.load('ha_satellite.server')
    package.wire, package.messages, package.server = wire, messages, server
    package.logger = isolated.logger
    return package


class FakeHomeAssistant:
    """Speaks the client side of the plaintext ESPHome API."""

    def __init__(self, sat, reader, writer):
        self.sat = sat
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, sat, server, *, hello=True):
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        client = cls(sat, reader, writer)
        if hello:
            await client.send(
                sat.messages.MessageType.HELLO_REQUEST,
                [(1, 'Home Assistant 2026.11'), (2, 1), (3, 14)],
            )
            assert (await client.recv())[0] == sat.messages.MessageType.HELLO_RESPONSE
        return client

    async def send(self, message_type, fields=()):
        wire = self.sat.wire
        self.writer.write(wire.encode_frame(message_type, wire.encode_message(fields)))
        await self.writer.drain()

    async def recv(self, timeout=2.0):
        message_type, payload = await asyncio.wait_for(
            self.sat.wire.read_frame(self.reader), timeout
        )
        return message_type, self.sat.wire.decode_message(payload)

    async def expect(self, message_type, timeout=2.0):
        received, fields = await self.recv(timeout)
        assert received == message_type, (received, fields)
        return fields

    async def subscribe(self, flags=1):
        await self.send(
            self.sat.messages.MessageType.SUBSCRIBE_VOICE_ASSISTANT_REQUEST,
            [(1, True), (2, flags)],
        )

    async def event(self, event_type, **data):
        encode = self.sat.wire.encode_message
        await self.send(
            self.sat.messages.MessageType.VOICE_ASSISTANT_EVENT_RESPONSE,
            [(1, int(event_type))]
            + [(2, encode([(1, key), (2, value)])) for key, value in data.items()],
        )


class Recorder:
    def __init__(self, sat, announce_result=True):
        self.connected = []
        self.events = []
        self.audio = []
        self.timers = []
        self.wake_words = []
        self.announcements = []
        self.announce_result = announce_result

    def connection_changed(self, connected):
        self.connected.append(connected)

    def pipeline_event(self, event):
        self.events.append(event)

    def tts_audio(self, data, end):
        self.audio.append((data, end))

    def timer_event(self, event):
        self.timers.append(event)

    def active_wake_words_changed(self, ids):
        self.wake_words.append(ids)

    async def announce(self, request):
        self.announcements.append(request)
        return self.announce_result


def identity(sat):
    m = sat.messages
    return m.DeviceIdentity(
        name='billy',
        friendly_name='Billy',
        mac_address='02:00:00:00:00:01',
        project_version='9.9.9',
        wake_words=(
            m.WakeWord('hey_billy', 'Hey Billy', ('en',)),
            m.WakeWord('okay_fish', 'Okay Fish', ('en', 'nl')),
        ),
    )


async def until(predicate, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, 'condition not reached'
        await asyncio.sleep(0.005)


def run_with_server(sat, scenario, *, listener=None, **kwargs):
    async def main():
        recorder = listener or Recorder(sat)
        server = sat.server.SatelliteServer(
            identity(sat), recorder, host='127.0.0.1', port=0, **kwargs
        )
        await server.start()
        try:
            await scenario(server, recorder)
        finally:
            await server.stop()

    asyncio.run(main())


# ----- wire codec -------------------------------------------------------------


@pytest.mark.parametrize('value', [0, 1, 127, 128, 300, 2**32 - 1, 2**63])
def test_varint_round_trip(sat, value):
    encoded = sat.wire.encode_varint(value)
    assert sat.wire.decode_varint(encoded + b'\xff') == (value, len(encoded))


def test_message_round_trip_omits_defaults_and_keeps_repeats(sat):
    w = sat.wire
    payload = w.encode_message(
        [(1, True), (2, 0), (3, ''), (4, 'héllo'), (5, b'\x00\x01'), (6, 0.5)]
        + [(7, 'a'), (7, 'b'), (8, False), (9, 0.0)]
    )
    fields = w.decode_message(payload)
    assert sorted(fields) == [1, 4, 5, 6, 7]
    assert w.get_bool(fields, 1) is True
    assert w.get_int(fields, 2) == 0
    assert w.get_str(fields, 4) == 'héllo'
    assert w.get_bytes(fields, 5) == b'\x00\x01'
    assert w.get_float(fields, 6) == 0.5
    assert w.get_repeated_str(fields, 7) == ['a', 'b']


def test_message_matches_protobuf_reference_bytes(sat):
    # Reference bytes from protoc for {1: 150, 2: "testing"}.
    assert sat.wire.encode_message([(1, 150), (2, 'testing')]) == bytes.fromhex(
        '089601120774657374696e67'
    )
    assert struct.unpack('<f', sat.wire.encode_message([(3, 1.5)])[1:])[0] == 1.5


@pytest.mark.parametrize(
    'payload', [b'\x08', b'\x12\x05ab', b'\x1d\x00', b'\x0b', b'\x00', b'\xff' * 11]
)
def test_malformed_messages_raise_protocol_error(sat, payload):
    with pytest.raises(sat.wire.ProtocolError):
        sat.wire.decode_message(payload)


def test_encoder_rejects_unsupported_values(sat):
    with pytest.raises(ValueError):
        sat.wire.encode_message([(1, -1)])
    with pytest.raises(TypeError):
        sat.wire.encode_message([(1, object())])


def frame_reader(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_frames_round_trip_and_reject_noise_and_oversize(sat):
    w = sat.wire

    async def scenario():
        frame = w.encode_frame(106, b'x' * 300) + w.encode_frame(7, b'')
        reader = frame_reader(frame)
        assert await w.read_frame(reader) == (106, b'x' * 300)
        assert await w.read_frame(reader) == (7, b'')
        with pytest.raises(asyncio.IncompleteReadError):
            await w.read_frame(reader)

        with pytest.raises(w.ProtocolError, match='Noise'):
            await w.read_frame(frame_reader(b'\x01\x00\x00'))
        with pytest.raises(w.ProtocolError, match='preamble'):
            await w.read_frame(frame_reader(b'\x02'))
        with pytest.raises(w.ProtocolError, match='exceeds'):
            await w.read_frame(frame_reader(w.encode_frame(1, b'y' * 65)), 64)

    asyncio.run(scenario())


# ----- connection handshake ---------------------------------------------------


def test_handshake_device_info_and_housekeeping(sat):
    m = sat.messages
    T = m.MessageType
    w = sat.wire

    async def scenario(server, recorder):
        reader, writer = await asyncio.open_connection('127.0.0.1', server.port)
        ha = FakeHomeAssistant(sat, reader, writer)
        await ha.send(T.HELLO_REQUEST, [(1, 'Home Assistant'), (2, 1), (3, 14)])
        hello = await ha.expect(T.HELLO_RESPONSE)
        assert (w.get_int(hello, 1), w.get_int(hello, 2)) == (1, 10)
        assert w.get_str(hello, 4) == 'billy'

        await ha.send(T.AUTHENTICATION_REQUEST)
        assert w.get_bool(await ha.expect(T.AUTHENTICATION_RESPONSE), 1) is False

        await ha.send(T.DEVICE_INFO_REQUEST)
        info = await ha.expect(T.DEVICE_INFO_RESPONSE)
        assert w.get_str(info, 2) == 'billy'
        assert w.get_str(info, 13) == 'Billy'
        assert w.get_str(info, 3) == '02:00:00:00:00:01'
        assert w.get_str(info, 9) == '9.9.9'
        # Home Assistant shows "<model> by <manufacturer>" from the project name.
        assert w.get_str(info, 8) == 'ThingsFromThom.Billy'
        assert w.get_str(info, 12) == 'ThingsFromThom'
        assert not w.get_bool(info, 1)  # no password
        flags = m.VoiceAssistantFeature(w.get_int(info, 17))
        assert m.VoiceAssistantFeature.VOICE_ASSISTANT in flags
        assert m.VoiceAssistantFeature.API_AUDIO in flags
        assert m.VoiceAssistantFeature.ANNOUNCE in flags

        await ha.send(T.DEVICE_CAPABILITIES_REQUEST)
        capabilities = await ha.expect(T.DEVICE_CAPABILITIES_RESPONSE)
        voice = w.decode_message(w.get_bytes(capabilities, 2))
        assert w.get_int(voice, 1) == int(flags)

        # Subscriptions Billy does not implement are ignored, not fatal.
        for unsupported in (
            T.SUBSCRIBE_STATES_REQUEST,
            T.SUBSCRIBE_LOGS_REQUEST,
            T.SUBSCRIBE_HOMEASSISTANT_SERVICES_REQUEST,
            T.SUBSCRIBE_HOME_ASSISTANT_STATES_REQUEST,
            9999,
        ):
            await ha.send(unsupported)
        await ha.send(T.LIST_ENTITIES_REQUEST)
        await ha.expect(T.LIST_ENTITIES_DONE_RESPONSE)
        await ha.send(T.PING_REQUEST)
        await ha.expect(T.PING_RESPONSE)

        await ha.send(T.DISCONNECT_REQUEST)
        await ha.expect(T.DISCONNECT_RESPONSE)
        assert await reader.read() == b''

    run_with_server(sat, scenario)


def test_first_message_must_be_hello(sat):
    T = sat.messages.MessageType

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server, hello=False)
        await ha.send(T.DEVICE_INFO_REQUEST)
        assert await ha.reader.read() == b''
        assert sat.logger.warning.called

    run_with_server(sat, scenario)


def test_wake_word_configuration(sat):
    T = sat.messages.MessageType
    w = sat.wire

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.send(T.VOICE_ASSISTANT_CONFIGURATION_REQUEST)
        config = await ha.expect(T.VOICE_ASSISTANT_CONFIGURATION_RESPONSE)
        words = [w.decode_message(raw) for raw in w.get_repeated_bytes(config, 1)]
        assert [w.get_str(word, 1) for word in words] == ['hey_billy', 'okay_fish']
        assert w.get_repeated_str(words[1], 3) == ['en', 'nl']
        assert w.get_repeated_str(config, 2) == ['hey_billy']
        assert w.get_int(config, 3) == 1

        await ha.send(
            T.VOICE_ASSISTANT_SET_CONFIGURATION, [(1, 'unknown'), (1, 'okay_fish')]
        )
        await until(lambda: recorder.wake_words)
        assert recorder.wake_words == [['okay_fish']]
        assert server.active_wake_words == ['okay_fish']

    run_with_server(sat, scenario)


# ----- voice pipeline ---------------------------------------------------------


@pytest.mark.parametrize('flags', [1, 4])  # api.proto value, aioesphomeapi value
def test_full_pipeline_run(sat, flags):
    m = sat.messages
    T = m.MessageType
    E = m.VoiceAssistantEvent
    w = sat.wire

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        assert not server.voice_connected
        await ha.subscribe(flags)
        await until(lambda: server.voice_connected)
        assert recorder.connected == [True]

        starting = asyncio.create_task(
            server.start_pipeline(wake_word_phrase='Hey Billy', conversation_id='c1')
        )
        request = await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        assert w.get_bool(request, 1) is True
        assert w.get_str(request, 2) == 'c1'
        assert w.get_int(request, 3) == m.VoiceAssistantRequestFlag.USE_VAD
        assert w.get_str(request, 5) == 'Hey Billy'
        assert not await server.send_audio(b'early')  # not accepted yet

        await ha.send(T.VOICE_ASSISTANT_RESPONSE, [(1, 0)])
        run = await starting
        assert run.mic_open

        await ha.event(E.RUN_START)
        await ha.event(E.STT_START)
        for chunk in (b'\x01\x00' * 160, b'\x02\x00' * 160):
            assert await server.send_audio(chunk)
            audio = await ha.expect(T.VOICE_ASSISTANT_AUDIO)
            assert w.get_bytes(audio, 1) == chunk
        assert not await server.send_audio(b'')

        await ha.event(E.STT_VAD_END)
        await until(lambda: not run.mic_open)
        assert not await server.send_audio(b'late')

        await ha.event(E.STT_END, text='turn on the kitchen light')
        await ha.event(E.INTENT_END, conversation_id='c1', continue_conversation='0')
        await ha.event(E.TTS_START, text='Done!')
        await ha.event(E.TTS_STREAM_START)
        await ha.send(T.VOICE_ASSISTANT_AUDIO, [(1, b'tts-pcm')])
        await ha.send(T.VOICE_ASSISTANT_AUDIO, [(2, True)])
        await ha.event(E.TTS_END, url='http://ha.local/api/tts_proxy/abc.flac')
        await ha.event(E.RUN_END)
        await asyncio.wait_for(run.finished.wait(), 2)
        # Home Assistant 2026.11 streams TTS after RUN_END and closes the
        # stream with TTS_STREAM_END rather than an audio end flag.
        await ha.send(T.VOICE_ASSISTANT_AUDIO, [(1, b'after-run-end')])
        await ha.event(E.TTS_STREAM_END)
        await until(lambda: recorder.events[-1].event_type == E.TTS_STREAM_END)

        assert run.error is None
        events = {event.event_type: event.data for event in recorder.events}
        assert events[E.STT_END] == {'text': 'turn on the kitchen light'}
        assert events[E.TTS_END]['url'].endswith('abc.flac')
        assert recorder.audio == [
            (b'tts-pcm', False),
            (b'', True),
            (b'after-run-end', False),
        ]

        # A new run can start after the previous one finished.
        again = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        assert (await again).mic_open

    run_with_server(sat, scenario)


def test_start_pipeline_without_home_assistant(sat):
    async def scenario(server, recorder):
        with pytest.raises(sat.server.SatelliteUnavailable, match='not connected'):
            await server.start_pipeline()
        assert not await server.send_audio(b'pcm')
        await server.stop_pipeline()  # nothing to stop is harmless

    run_with_server(sat, scenario)


def test_subscription_without_api_audio_is_refused(sat):
    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe(flags=8)  # wake-word audio only
        await ha.send(sat.messages.MessageType.PING_REQUEST)
        await ha.expect(sat.messages.MessageType.PING_RESPONSE)
        assert not server.voice_connected
        assert recorder.connected == []

    run_with_server(sat, scenario)


@pytest.mark.parametrize(
    ('response', 'reason'),
    [([(2, True)], 'refused'), ([(1, 12345)], 'UDP')],
)
def test_refused_pipeline_raises(sat, response, reason):
    T = sat.messages.MessageType

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE, response)
        with pytest.raises(sat.server.SatelliteUnavailable, match=reason):
            await starting
        assert not await server.send_audio(b'pcm')

    run_with_server(sat, scenario)


def test_unanswered_pipeline_times_out(sat):
    T = sat.messages.MessageType

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        with pytest.raises(sat.server.SatelliteUnavailable, match='did not answer'):
            await server.start_pipeline()
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        # A late answer for the abandoned run must not reopen the microphone.
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        await ha.send(T.PING_REQUEST)
        await ha.expect(T.PING_RESPONSE)
        assert not await server.send_audio(b'pcm')

    run_with_server(sat, scenario, response_timeout=0.1)


def test_pipeline_error_event_ends_run(sat):
    T = sat.messages.MessageType
    E = sat.messages.VoiceAssistantEvent

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        run = await starting
        await ha.event(E.ERROR, code='stt-no-text-recognized', message='No text')
        await asyncio.wait_for(run.finished.wait(), 2)
        assert run.error == 'No text'
        assert not await server.send_audio(b'pcm')

    run_with_server(sat, scenario)


def test_billy_stops_its_run(sat):
    T = sat.messages.MessageType
    w = sat.wire

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        run = await starting

        await server.stop_pipeline()
        stop = await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        assert w.get_bool(stop, 1) is False
        assert run.finished.is_set()
        assert not await server.send_audio(b'pcm')
        await server.stop_pipeline()  # idempotent

    run_with_server(sat, scenario)


def test_end_of_a_stopped_run_does_not_end_the_next_one(sat):
    """Button pressed twice in a row: Billy stops a run Home Assistant still
    has open and starts the next before the old run's end arrives."""
    T = sat.messages.MessageType
    E = sat.messages.VoiceAssistantEvent
    w = sat.wire

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        first = await starting
        await ha.event(E.RUN_START)
        await until(lambda: first.started)
        await server.stop_pipeline()
        assert w.get_bool(await ha.expect(T.VOICE_ASSISTANT_REQUEST), 1) is False

        starting = asyncio.create_task(server.start_pipeline())
        assert w.get_bool(await ha.expect(T.VOICE_ASSISTANT_REQUEST), 1) is True
        # The old run ends only now, ahead of the answer to the new request.
        await ha.event(E.ERROR, code='stt-no-text-recognized', message='No text')
        await ha.event(E.RUN_END)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        second = await asyncio.wait_for(starting, 2)
        assert second.mic_open and not second.finished.is_set()
        await ha.event(E.RUN_START)
        await until(lambda: second.started)
        assert await server.send_audio(b'pcm')
        # The new run's own end still ends it.
        await ha.event(E.RUN_END)
        await asyncio.wait_for(second.finished.wait(), 2)
        assert second.error is None

    run_with_server(sat, scenario)


def test_run_home_assistant_ends_at_once_is_reported_as_such(sat):
    """Home Assistant's setup wizard ends a run straight away (RUN_END, no
    RUN_START) before it answers the request."""
    T = sat.messages.MessageType
    E = sat.messages.VoiceAssistantEvent

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.event(E.RUN_END)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        with pytest.raises(sat.server.RunEndedEarly):
            await asyncio.wait_for(starting, 2)

    run_with_server(sat, scenario)


def test_stale_end_window_expires(sat, monkeypatch):
    """An old run whose end never came cannot swallow a later run's end."""
    T = sat.messages.MessageType
    E = sat.messages.VoiceAssistantEvent
    monkeypatch.setattr(sat.server, 'STALE_END_SECONDS', 0.05)

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        await starting
        await ha.event(E.RUN_START)
        await until(lambda: server._ha_run_open)
        await server.stop_pipeline()
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await asyncio.sleep(0.1)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.event(E.RUN_END)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        with pytest.raises(sat.server.RunEndedEarly):
            await asyncio.wait_for(starting, 2)

    run_with_server(sat, scenario)


def test_disconnect_mid_run_finishes_run(sat):
    T = sat.messages.MessageType

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        await ha.send(T.VOICE_ASSISTANT_RESPONSE)
        run = await starting

        ha.writer.close()
        await asyncio.wait_for(run.finished.wait(), 2)
        assert run.error == 'Home Assistant disconnected'
        assert recorder.connected == [True, False]
        assert not server.voice_connected

    run_with_server(sat, scenario)


def test_disconnect_while_waiting_for_acceptance(sat):
    T = sat.messages.MessageType

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        starting = asyncio.create_task(server.start_pipeline())
        await ha.expect(T.VOICE_ASSISTANT_REQUEST)
        ha.writer.close()
        with pytest.raises(sat.server.SatelliteUnavailable):
            await starting

    run_with_server(sat, scenario)


def test_newer_subscription_replaces_older_connection(sat):
    async def scenario(server, recorder):
        first = await FakeHomeAssistant.connect(sat, server)
        await first.subscribe()
        await until(lambda: server.voice_connected)
        second = await FakeHomeAssistant.connect(sat, server)
        await second.subscribe()
        await until(lambda: len(recorder.connected) == 2)
        assert await first.reader.read() == b''
        assert server.voice_connected
        assert recorder.connected == [True, True]

    run_with_server(sat, scenario)


def test_announcement_and_timer_events(sat):
    m = sat.messages
    T = m.MessageType
    w = sat.wire

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await ha.send(
            T.VOICE_ASSISTANT_ANNOUNCE_REQUEST,
            [(1, 'http://ha.local/doorbell.mp3'), (2, 'Someone is at the door')],
        )
        finished = await ha.expect(T.VOICE_ASSISTANT_ANNOUNCE_FINISHED)
        assert w.get_bool(finished, 1) is True
        request = recorder.announcements[0]
        assert request.media_id.endswith('doorbell.mp3')
        assert request.text == 'Someone is at the door'
        assert not request.start_conversation

        await ha.send(
            T.VOICE_ASSISTANT_TIMER_EVENT_RESPONSE,
            [(1, m.VoiceAssistantTimerEvent.FINISHED), (2, 't1'), (3, 'Pasta')]
            + [(4, 600), (6, False)],
        )
        await until(lambda: recorder.timers)
        timer = recorder.timers[0]
        assert (timer.event_type, timer.name, timer.total_seconds) == (3, 'Pasta', 600)

    run_with_server(sat, scenario)


def test_failing_listener_reports_failure_without_dropping_home_assistant(sat):
    T = sat.messages.MessageType
    w = sat.wire

    class Broken(Recorder):
        def pipeline_event(self, event):
            raise RuntimeError('listener bug')

        async def announce(self, request):
            raise RuntimeError('player bug')

    async def scenario(server, recorder):
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        await ha.event(sat.messages.VoiceAssistantEvent.RUN_START)
        await ha.send(T.VOICE_ASSISTANT_ANNOUNCE_REQUEST, [(2, 'hi')])
        finished = await ha.expect(T.VOICE_ASSISTANT_ANNOUNCE_FINISHED)
        assert w.get_bool(finished, 1) is False
        await ha.send(T.PING_REQUEST)
        await ha.expect(T.PING_RESPONSE)
        assert server.voice_connected
        assert sat.logger.exception.call_count == 2

    run_with_server(sat, scenario, listener=Broken(sat))


def test_stop_disconnects_clients_and_is_idempotent(sat):
    T = sat.messages.MessageType

    async def main():
        server = sat.server.SatelliteServer(
            identity(sat), Recorder(sat), host='127.0.0.1', port=0
        )
        await server.start()
        await server.start()  # second start keeps the same listener
        ha = await FakeHomeAssistant.connect(sat, server)
        await ha.subscribe()
        await until(lambda: server.voice_connected)
        await asyncio.wait_for(server.stop(), 3)
        assert (await ha.recv())[0] == T.DISCONNECT_REQUEST
        assert await ha.reader.read() == b''
        assert not server.voice_connected
        await server.stop()
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1', server.port)

    asyncio.run(main())


def test_package_import_has_no_side_effects():
    import subprocess

    code = (
        'import sys, asyncio; '
        f'sys.path.insert(0, {str(ROOT / "application")!r}); '
        'import core.ha_satellite; '
        'assert "core.ha_satellite.server" not in sys.modules; '
        'assert "core.config" not in sys.modules'
    )
    subprocess.run([sys.executable, '-c', code], check=True, timeout=30)


# ----- mDNS discovery ---------------------------------------------------------


@pytest.fixture
def discovery(sat, isolated):
    module = isolated.load('ha_satellite.discovery')
    sat.discovery = module
    return module


class FakeAvahi:
    def __init__(self, states, taken=()):
        self.states = list(states)
        self.taken = set(taken)
        self.calls = []
        self.disconnected = False

    async def entry_group_new(self):
        self.calls.append(('new',))
        return '/Client1/EntryGroup1'

    async def add_service(self, group, name, port, txt):
        self.calls.append(('add', name, port, txt))

    async def commit(self, group):
        self.calls.append(('commit',))

    async def state(self, group):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def alternative_service_name(self, name):
        return f'{name} #2'

    async def reset(self, group):
        self.calls.append(('reset',))

    async def free(self, group):
        self.calls.append(('free', group))

    def disconnect(self):
        self.disconnected = True


def advertiser(sat, discovery, avahi, **kwargs):
    async def connect():
        if isinstance(avahi, Exception):
            raise avahi
        return avahi

    return discovery.AvahiAdvertiser(
        identity(sat), 6053, connect=connect, poll_interval=0, **kwargs
    )


def test_txt_records_match_esphome_discovery(sat, discovery):
    records = discovery.txt_records(identity(sat), '2026.11.0')
    txt = dict(record.decode().split('=', 1) for record in records)
    assert txt['mac'] == '020000000001'  # Home Assistant requires this key
    assert txt['friendly_name'] == 'Billy'
    assert txt['version'] == '2026.11.0'
    assert txt['project_version'] == '9.9.9'
    assert txt['project_name'] == 'ThingsFromThom.Billy'
    assert all(len(record) <= 255 for record in records)
    with pytest.raises(ValueError):
        discovery.mac_txt('not-a-mac')


def test_advertiser_publishes_and_withdraws(sat, discovery):
    D = discovery
    avahi = FakeAvahi([D.ENTRY_GROUP_REGISTERING, D.ENTRY_GROUP_ESTABLISHED])
    adv = advertiser(sat, discovery, avahi)

    async def scenario():
        assert await adv.start()
        assert adv.published
        assert await adv.start()  # already published
        add = [call for call in avahi.calls if call[0] == 'add']
        assert len(add) == 1
        assert add[0][1:3] == ('billy', 6053)
        assert b'mac=020000000001' in add[0][3]
        await adv.stop()
        await adv.stop()

    asyncio.run(scenario())
    assert ('free', '/Client1/EntryGroup1') in avahi.calls
    assert avahi.disconnected
    assert not adv.published


def test_advertiser_renames_on_collision(sat, discovery):
    D = discovery
    avahi = FakeAvahi([D.ENTRY_GROUP_COLLISION, D.ENTRY_GROUP_ESTABLISHED])
    adv = advertiser(sat, discovery, avahi)
    assert asyncio.run(adv.start())
    names = [call[1] for call in avahi.calls if call[0] == 'add']
    assert names == ['billy', 'billy #2']
    assert ('reset',) in avahi.calls
    assert adv.service_name == 'billy #2'


@pytest.mark.parametrize(
    'avahi',
    [
        OSError('no system bus'),
        FakeAvahi([4]),  # ENTRY_GROUP_FAILURE
        FakeAvahi([3]),  # collisions forever
        FakeAvahi([1]),  # registering forever -> timeout
    ],
)
def test_advertiser_failure_is_reported_not_raised(sat, discovery, avahi):
    adv = advertiser(sat, discovery, avahi, timeout=0.2)
    assert asyncio.run(adv.start()) is False
    assert not adv.published
    assert sat.logger.warning.called
    if isinstance(avahi, FakeAvahi):
        assert avahi.disconnected


def test_avahi_dbus_messages_marshal(sat, discovery):
    from dbus_next import Message, MessageType

    sent = []

    class Bus:
        async def call(self, message):
            message._marshall()  # raises when body and signature disagree
            sent.append(message)
            message.serial = len(sent)  # the real bus assigns one
            if message.member == 'Commit':
                return Message.new_error(message, 'org.freedesktop.Avahi.Fail', 'x')
            body = {'EntryGroupNew': ['/G'], 'GetState': [2]}
            signature = {'EntryGroupNew': 'o', 'GetState': 'i'}
            return Message(
                message_type=MessageType.METHOD_RETURN,
                reply_serial=1,
                signature=signature.get(message.member, ''),
                body=body.get(message.member, []),
            )

    avahi = discovery._Avahi(Bus())

    async def scenario():
        assert await avahi.entry_group_new() == '/G'
        await avahi.add_service('/G', 'billy', 6053, [b'mac=020000000001'])
        assert await avahi.state('/G') == 2
        with pytest.raises(discovery.DiscoveryError, match='Commit'):
            await avahi.commit('/G')

    asyncio.run(scenario())
    add = sent[1]
    assert add.destination == 'org.freedesktop.Avahi'
    assert add.signature == 'iiussssqaay'
    assert add.body[4] == '_esphomelib._tcp'
