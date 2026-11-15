"""Conversations answered by Home Assistant through Billy's satellite connection.

Hardware, the playback worker and Home Assistant are replaced by fakes; the
service test uses the real satellite server over loopback.
"""

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from conftest import ROOT
from test_ha_satellite import FakeHomeAssistant, until


@pytest.fixture
def ha(isolated, monkeypatch):
    package = ModuleType('isolated_core.ha_satellite')
    package.__path__ = [str(ROOT / 'application/core/ha_satellite')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in ('wire', 'messages', 'server', 'audio_bridge', 'session', 'service'):
        setattr(package, name, isolated.load(f'ha_satellite.{name}'))
    package.logger = isolated.logger
    package.config = isolated.config
    return package


def event(ha, kind, **data):
    return ha.messages.VoiceAssistantEventResponse(int(kind), data)


class FakeHardware:
    mic_rate = 16000

    def __init__(self):
        self.callback = None
        self.states = []
        self.played = []
        self.errors = []
        self.mic_starts = 0
        self.mic_stops = 0
        self.playback_stops = 0
        self.waited = 0
        self.clip_done = True

    def start_mic(self, callback):
        self.callback = callback
        self.mic_starts += 1

    def stop_mic(self):
        self.callback = None
        self.mic_stops += 1

    def play(self, pcm):
        self.played.append(pcm)

    def speaking_started(self):
        pass

    def stop_playback(self):
        self.playback_stops += 1

    async def wait_playback(self, cancelled):
        self.waited += 1

    def wake_up_clip_done(self):
        return self.clip_done

    def set_state(self, state):
        self.states.append(state)

    async def error_sound(self, message):
        self.errors.append(message)

    def speak(self, blocks=1):
        for _ in range(blocks):
            self.callback(np.full((640, 1), 1000, np.int16), 640, None, None)


class FakeService:
    def __init__(self, connected=True):
        self.voice_connected = connected
        self.session = None
        self.runs = []
        self.phrases = []
        self.audio = []
        self.stops = 0
        self.fail = None

    def attach(self, session):
        self.session = session

    def detach(self, session):
        if self.session is session:
            self.session = None

    async def start_pipeline(self, *, conversation_id='', wake_word_phrase=''):
        if self.fail:
            raise self.fail
        self.phrases.append(wake_word_phrase)
        run = SimpleNamespace(mic_open=True)
        self.runs.append((conversation_id, run))
        return run

    async def send_audio(self, pcm):
        self.audio.append(pcm)
        return True

    async def stop_pipeline(self):
        self.stops += 1


def make_session(ha, service=None, hardware=None):
    return ha.session.HaAssistSession(
        interrupt_event=threading.Event(),
        service=service or FakeService(),
        hardware=hardware or FakeHardware(),
    )


async def listen(ha, session, service, hw, *, run_index=0):
    """Wait until Billy streams the microphone, then end the utterance."""
    await until(lambda: len(service.runs) > run_index and hw.callback)
    hw.speak(3)
    await until(lambda: len(service.audio) >= 3)
    service.runs[run_index][1].mic_open = False
    hw.speak()  # wakes the sender so it sees the closed microphone
    await until(lambda: hw.callback is None)


def answer(ha, session, *, text='It is 10:30', keep_going=False, tts=b'\x10\x00' * 512):
    E = ha.messages.VoiceAssistantEvent
    session.deliver_event(event(ha, E.RUN_START))
    session.deliver_event(event(ha, E.STT_END, text='what time is it'))
    session.deliver_event(
        event(
            ha,
            E.INTENT_END,
            conversation_id='conv-1',
            continue_conversation='1' if keep_going else '0',
        )
    )
    session.deliver_event(event(ha, E.TTS_START, text=text))
    session.deliver_event(event(ha, E.TTS_STREAM_START))
    session.deliver_event(event(ha, E.TTS_END, url='http://ha/api/tts_proxy/x.wav'))
    session.deliver_event(event(ha, E.RUN_END))
    session.deliver_tts_audio(tts)
    session.deliver_event(event(ha, E.TTS_STREAM_END))


# ----- audio bridges ----------------------------------------------------------


def test_stream_resampler_is_continuous_across_chunks(ha):
    rng = np.random.default_rng(1)
    signal = rng.integers(-20000, 20000, 1601).astype('<i2').tobytes()
    whole = ha.audio_bridge.StreamResampler(16000, 24000).process(signal)
    chunked = ha.audio_bridge.StreamResampler(16000, 24000)
    pieces = [signal[i : i + 333] for i in range(0, len(signal), 333)]  # odd sizes
    assert b''.join(chunked.process(piece) for piece in pieces) == whole
    out = np.frombuffer(whole, '<i2')
    assert len(out) == (1600 * 3) // 2 + 1
    assert out[0] == np.frombuffer(signal, '<i2')[0]
    assert out[3] == np.frombuffer(signal, '<i2')[2]  # 3 out samples per 2 in


def test_mic_blocks_become_16k_mono(ha):
    bridge = ha.audio_bridge
    stereo = np.zeros((1920, 2), np.int16)  # 40 ms at 48 kHz
    stereo[:, 0] = 1200
    stereo[:, 1] = -32000  # second channel is ignored
    pcm = np.frombuffer(bridge.mic_to_assist_pcm(stereo, 48000), '<i2')
    assert len(pcm) == 640
    assert abs(int(np.median(pcm)) - 1200) <= 2
    mono = np.arange(640, dtype=np.int16)
    assert bridge.mic_to_assist_pcm(mono, 16000) == mono.astype('<i2').tobytes()


# ----- session ----------------------------------------------------------------


def test_one_question_and_answer(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        assert session.mic_manager.mic_running is False
        answer(ha, session)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert [conversation for conversation, _ in service.runs] == ['']
    assert all(len(chunk) == 1280 for chunk in service.audio)  # 640 samples
    assert hw.mic_starts == 1 and hw.mic_stops >= 1
    played = b''.join(hw.played)
    # 512 samples at 16 kHz -> 24 kHz; the last fraction waits for more input
    assert len(played) // 2 == 767
    assert hw.states == ['listening', 'speaking', 'idle']
    assert hw.waited == 1
    assert hw.errors == []
    assert service.session is None
    assert session.conversation_id == 'conv-1'
    assert not session.session_active.is_set()


def test_continue_conversation_listens_again_in_same_conversation(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)
    E = ha.messages.VoiceAssistantEvent

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        answer(ha, session, keep_going=True)
        await listen(ha, session, service, hw, run_index=1)
        session.deliver_event(event(ha, E.RUN_START))
        session.deliver_event(event(ha, E.ERROR, code='stt-no-text-recognized'))
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert [conversation for conversation, _ in service.runs] == ['', 'conv-1']
    assert hw.errors == []  # silence after a follow-up is not an error
    assert hw.states[-1] == 'idle'


def test_wake_word_phrase_goes_with_the_first_run_only(ha):
    service, hw = FakeService(), FakeHardware()
    session = ha.session.HaAssistSession(
        interrupt_event=threading.Event(),
        service=service,
        hardware=hw,
        wake_word_phrase='Hey Billy',
    )
    E = ha.messages.VoiceAssistantEvent

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        answer(ha, session, keep_going=True)
        await listen(ha, session, service, hw, run_index=1)
        session.deliver_event(event(ha, E.RUN_START))
        session.deliver_event(event(ha, E.ERROR, code='stt-no-text-recognized'))
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    # Home Assistant's setup wizard waits for it; follow-ups were not woken.
    assert service.phrases == ['Hey Billy', '']


def test_dory_mode_does_not_continue(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)
    session.run_mode = 'dory'

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        answer(ha, session, keep_going=True)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert len(service.runs) == 1


def test_home_assistant_not_connected(ha):
    service, hw = FakeService(connected=False), FakeHardware()
    session = make_session(ha, service, hw)
    asyncio.run(asyncio.wait_for(session.start(), 2))
    assert hw.errors == ['Home Assistant is not connected']
    assert service.runs == [] and hw.mic_starts == 0
    assert not session.session_active.is_set()


def test_refused_pipeline_plays_error(ha):
    service, hw = FakeService(), FakeHardware()
    service.fail = ha.server.SatelliteUnavailable('pipeline refused')
    session = make_session(ha, service, hw)
    asyncio.run(asyncio.wait_for(session.start(), 2))
    assert hw.errors == ['pipeline refused']
    assert hw.mic_starts == 0
    assert hw.states == ['idle']


def test_pipeline_error_plays_error_sound(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)
    E = ha.messages.VoiceAssistantEvent

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        session.deliver_event(event(ha, E.RUN_START))
        session.deliver_event(
            event(ha, E.ERROR, code='intent-failed', message='Agent failed')
        )
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert hw.errors == ['Agent failed']
    assert service.stops == 0  # Home Assistant already ended the run


def test_stop_while_listening_closes_mic_and_run(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)

    async def scenario():
        task = asyncio.create_task(session.start())
        await until(lambda: hw.callback)
        await asyncio.wait_for(session.stop_session(), 2)
        assert task.done()

    asyncio.run(scenario())
    assert hw.callback is None
    assert service.stops == 1
    assert hw.playback_stops >= 1
    assert hw.states[-1] == 'idle'
    assert session.interrupt_event.is_set()
    assert service.session is None


def test_request_stop_from_takeover_is_bounded(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)

    async def scenario():
        task = asyncio.create_task(session.start())
        await until(lambda: hw.callback)
        await session.request_stop()
        await session._close_ws(timeout=0.5)  # repeated stop is harmless
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert service.stops == 1
    assert hw.callback is None


def test_button_interrupt_listens_again_and_drops_old_audio(ha):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)
    E = ha.messages.VoiceAssistantEvent

    async def scenario():
        task = asyncio.create_task(session.start())
        await listen(ha, session, service, hw)
        session.deliver_event(event(ha, E.RUN_START))
        session.deliver_event(event(ha, E.TTS_STREAM_START))
        session.deliver_tts_audio(b'\x01\x00' * 512)
        await until(lambda: session.is_assistant_turn())
        await session.interrupt_to_user_turn(source='button')
        assert not session.is_assistant_turn()

        await until(lambda: len(service.runs) == 2 and hw.callback)
        played = len(hw.played)
        # The interrupted run keeps streaming briefly: none of it may play.
        session.deliver_tts_audio(b'\x02\x00' * 512)
        session.deliver_event(event(ha, E.TTS_STREAM_END))
        session.deliver_event(event(ha, E.RUN_END))
        assert len(hw.played) == played
        await listen(ha, session, service, hw, run_index=1)
        answer(ha, session)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert service.stops == 1  # the interrupted run
    assert hw.playback_stops >= 1
    assert len(service.runs) == 2


def test_listening_is_bounded(ha, monkeypatch):
    service, hw = FakeService(), FakeHardware()
    session = make_session(ha, service, hw)
    monkeypatch.setattr(ha.session, 'MAX_LISTEN_SECONDS', 0.2)
    monkeypatch.setattr(ha.session, 'EVENT_TIMEOUT', 0.2)

    async def scenario():
        task = asyncio.create_task(session.start())
        await until(lambda: hw.callback)
        for _ in range(20):
            if hw.callback:
                hw.speak()
            await asyncio.sleep(0.02)
        await asyncio.wait_for(task, 3)

    asyncio.run(scenario())
    assert service.stops == 1
    assert hw.callback is None


def test_waits_for_wake_up_clip_before_listening(ha):
    service, hw = FakeService(), FakeHardware()
    hw.clip_done = False
    session = make_session(ha, service, hw)

    async def scenario():
        task = asyncio.create_task(session.start())
        await asyncio.sleep(0.1)
        assert service.runs == []
        hw.clip_done = True
        await listen(ha, session, service, hw)
        answer(ha, session)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_opening_options_are_accepted_and_ignored(ha):
    session = ha.session.HaAssistSession(
        interrupt_event=threading.Event(),
        kickoff_text='hello',
        kickoff_kind='literal',
        autofollowup='never',
    )
    assert session.last_activity and session.run_mode is None


# ----- service over loopback --------------------------------------------------


def identity(ha):
    return ha.messages.DeviceIdentity(
        name='billy', friendly_name='Billy', mac_address='02:00:00:00:00:01'
    )


def test_service_routes_a_real_turn_and_announcements(ha):
    m = ha.messages
    T, E = m.MessageType, m.VoiceAssistantEvent
    announced = []

    async def announcer(urls, text):
        announced.append((urls, text))
        return True

    ha.config.HA_HOST = 'http://ha.example:8123'
    service = ha.service.SatelliteService(
        identity(ha), host='127.0.0.1', port=0, advertise=False, announcer=announcer
    )
    assert service.start()
    hw = FakeHardware()
    session = ha.session.HaAssistSession(
        interrupt_event=threading.Event(), service=service, hardware=hw
    )

    async def home_assistant():
        client = await FakeHomeAssistant.connect(
            ha, SimpleNamespace(port=service.bound_port)
        )
        await client.subscribe(4)  # aioesphomeapi's API-audio flag
        await until(lambda: service.voice_connected)

        await client.send(
            T.VOICE_ASSISTANT_ANNOUNCE_REQUEST,
            [(1, '/api/tts_proxy/a.mp3'), (2, 'Dinner'), (3, 'http://x/chime.mp3')],
        )
        finished = await client.expect(T.VOICE_ASSISTANT_ANNOUNCE_FINISHED)
        assert ha.wire.get_bool(finished, 1) is True

        session_thread = threading.Thread(target=lambda: asyncio.run(session.start()))
        session_thread.start()
        request = await client.expect(T.VOICE_ASSISTANT_REQUEST)
        assert ha.wire.get_bool(request, 1) is True
        await client.send(T.VOICE_ASSISTANT_RESPONSE)
        await client.event(E.RUN_START)
        await until(lambda: hw.callback)
        hw.callback(np.zeros((640, 1), np.int16), 640, None, None)
        audio = await client.expect(T.VOICE_ASSISTANT_AUDIO)
        assert len(ha.wire.get_bytes(audio, 1)) == 1280
        await client.event(E.STT_VAD_END)
        await client.event(
            E.INTENT_END, conversation_id='c9', continue_conversation='0'
        )
        await client.event(E.TTS_STREAM_START)
        await client.event(E.RUN_END)
        await client.send(T.VOICE_ASSISTANT_AUDIO, [(1, b'\x05\x00' * 512)])
        await client.event(E.TTS_STREAM_END)
        await asyncio.to_thread(session_thread.join, 5)
        assert not session_thread.is_alive()

    try:
        asyncio.run(home_assistant())
    finally:
        service.stop()
        service.stop()  # idempotent
    assert announced == [
        (['http://x/chime.mp3', 'http://ha.example:8123/api/tts_proxy/a.mp3'], 'Dinner')
    ]
    assert len(b''.join(hw.played)) // 2 == 767
    assert hw.states == ['listening', 'speaking', 'idle']
    assert session.conversation_id == 'c9'
    assert service.session is None


def test_service_absolute_url_falls_back_to_peer(ha):
    ha.config.HA_HOST = ''
    service = ha.service.SatelliteService(identity(ha), advertise=False)
    assert service.absolute_url('http://a/b.mp3') == 'http://a/b.mp3'
    assert service.absolute_url('/x.mp3') == 'http://homeassistant.local:8123/x.mp3'
    ha.config.HA_HOST = '192.168.1.5:8123'
    assert service.absolute_url('/x.mp3') == 'http://192.168.1.5:8123/x.mp3'


def test_start_service_only_for_home_assistant_provider(ha, monkeypatch):
    ha.config.REALTIME_AI_PROVIDER = 'openai'
    assert ha.service.start_service() is None
    assert ha.service.get_service() is None
    ha.service.stop_service()  # nothing to stop


@pytest.mark.parametrize(
    ('hostname', 'expected'),
    [('Billy.local', 'billy'), ('Big Mouth_Bass', 'big-mouth-bass'), ('---', 'billy')],
)
def test_device_name_follows_hostname(ha, hostname, expected):
    assert ha.service.device_name(hostname) == expected


# ----- announcements a device rings for (the phone) ----------------------------


def announce_request(ha, *, start_conversation=False):
    return ha.messages.VoiceAssistantAnnounceRequest(
        media_id='/api/tts_proxy/hello.mp3',
        text='Dinner is ready',
        preannounce_media_id='',
        start_conversation=start_conversation,
    )


def fake_decode(monkeypatch, ha, played_urls):
    async def decode(url, put, *, cancelled=lambda: False):
        played_urls.append(url)
        put(b'\x01\x00' * 240)
        return 480

    monkeypatch.setattr(ha.service, 'decode_to_playback', decode)


def test_without_a_ringing_device_billy_announces_at_once(ha):
    """The fish: no device takes the announcement, so it plays right away."""
    announced = []

    async def announcer(urls, text):
        announced.append((urls, text))
        return True

    ha.config.HA_HOST = 'http://ha.example:8123'
    service = ha.service.SatelliteService(
        identity(ha), advertise=False, announcer=announcer, ringer=lambda opening: False
    )
    assert asyncio.run(service.announce(announce_request(ha))) is True
    assert announced == [
        (['http://ha.example:8123/api/tts_proxy/hello.mp3'], 'Dinner is ready')
    ]


@pytest.mark.parametrize('start_conversation', [False, True])
def test_phone_rings_and_plays_the_announcement_when_answered(
    ha, monkeypatch, start_conversation
):
    played_urls, openings = [], []
    fake_decode(monkeypatch, ha, played_urls)
    ha.config.HA_HOST = 'http://ha.example:8123'

    async def announcer(urls, text):
        raise AssertionError('a ringing device must not announce at once')

    service = ha.service.SatelliteService(
        identity(ha),
        advertise=False,
        announcer=announcer,
        ringer=lambda opening: openings.append(opening) or True,
    )
    fake = FakeService()
    hw = FakeHardware()

    def lift_handset():
        # What the phone does on pickup: start a session with the opening.
        opening = openings[0]
        session = ha.session.HaAssistSession(
            interrupt_event=threading.Event(),
            service=fake,
            hardware=hw,
            announcement=opening['announcement'],
        )

        async def conversation():
            task = asyncio.create_task(session.start())
            if start_conversation:
                await listen(ha, session, fake, hw)
                answer(ha, session)
            await asyncio.wait_for(task, 2)

        asyncio.run(conversation())

    async def scenario():
        announce = asyncio.create_task(
            service.announce(
                announce_request(ha, start_conversation=start_conversation)
            )
        )
        await until(lambda: openings)
        opening = openings[0]
        assert opening['text'] == 'Dinner is ready'
        assert opening['interactive'] is start_conversation
        assert not announce.done()  # waits for the handset
        await asyncio.to_thread(lift_handset)
        return await asyncio.wait_for(announce, 2)

    assert asyncio.run(scenario()) is True
    assert played_urls == ['http://ha.example:8123/api/tts_proxy/hello.mp3']
    assert hw.states[0] == 'speaking'
    # A plain announcement ends the call; start_conversation listens on.
    assert len(fake.runs) == (1 if start_conversation else 0)


def test_unanswered_ring_reports_failure_and_cannot_play_late(ha):
    ring = ha.service.RingAnnouncement(['http://x/a.mp3'], 'Hi', False)
    assert ring.wait(answer_timeout=0.01) is False
    assert ring.claim() is False  # a late pickup does not play it


def test_announcement_already_playing_is_waited_for(ha):
    ring = ha.service.RingAnnouncement(['http://x/a.mp3'], 'Hi', False)
    assert ring.claim()
    threading.Timer(0.05, ring.finish, args=(True,)).start()
    assert ring.wait(answer_timeout=0.01, play_timeout=2) is True
    assert ring.expire() is False


def test_failed_or_disconnected_session_reports_failure(ha, monkeypatch):
    ring = ha.service.RingAnnouncement(['http://x/a.mp3'], 'Hi', False)
    session = ha.session.HaAssistSession(
        interrupt_event=threading.Event(),
        service=FakeService(connected=False),
        hardware=FakeHardware(),
        announcement=ring,
    )
    asyncio.run(session.start())
    assert ring.wait(answer_timeout=0.01) is False


def test_ringer_errors_fall_back_to_announcing(ha):
    def broken(opening):
        raise RuntimeError('device gone')

    service = ha.service.SatelliteService(identity(ha), advertise=False, ringer=broken)
    ring = ha.service.RingAnnouncement([], 'Hi', False)
    assert service._ring(ring) is False


@pytest.mark.parametrize(
    ('provider', 'passed'), [('home-assistant', True), ('openai', False)]
)
def test_opening_carries_the_announcement_only_in_home_assistant_mode(provider, passed):
    from conftest import definitions

    namespace = {'config': SimpleNamespace(REALTIME_AI_PROVIDER=provider)}
    button = definitions(
        'application/core/button.py',
        ['plays_wake_up_clip', '_opening_session_options'],
        namespace,
    )
    marker = object()
    options = button._opening_session_options({
        'text': 'Dinner is ready',
        'interactive': False,
        'announcement': marker,
    })
    assert options['kickoff_text'] == 'Dinner is ready'
    assert (options.get('announcement') is marker) is passed
    # Billy's own sessions never receive it.
    assert 'announcement' not in button._opening_session_options({'text': 'Hi'})


def test_run_ended_by_home_assistant_at_once_plays_no_error_sound(ha):
    service, hw = FakeService(), FakeHardware()
    service.fail = ha.server.RunEndedEarly(ha.server.RUN_ENDED_EARLY)
    session = make_session(ha, service, hw)
    asyncio.run(asyncio.wait_for(session.start(), 2))
    assert hw.errors == []
    assert hw.mic_starts == 0
    # Its setup wizard taking a wake-word run is the step succeeding.
    woken = ha.session.HaAssistSession(
        service=service, hardware=FakeHardware(), wake_word_phrase='Hey Billy'
    )
    asyncio.run(asyncio.wait_for(woken.start(), 2))
    assert woken.hardware.errors == []
    service.fail = ha.server.SatelliteUnavailable('pipeline refused')
    session = make_session(ha, service, FakeHardware())
    asyncio.run(asyncio.wait_for(session.start(), 2))
    assert session.hardware.errors == ['pipeline refused']
