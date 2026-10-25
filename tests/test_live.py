"""GPT-Live contract tests using real provider/session code and offline sockets."""

import asyncio
import base64
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest


class Socket:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps(await self.inbox.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.recv()

    async def close(self):
        self.closed = True


@pytest.fixture
def live(isolated, audio_module, monkeypatch):
    isolated.config.MIC_TIMEOUT_SECONDS = 10
    isolated.config.MIC_TIMEOUT_TAIL_FLAP = False
    isolated.config.SILENCE_THRESHOLD = 500
    monkeypatch.setitem(
        sys.modules, 'isolated_core.mic', SimpleNamespace(MicManager=Mock())
    )
    package = ModuleType('isolated_core.session')
    package.__path__ = [str(Path(__file__).parents[1] / 'application/core/session')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    provider = importlib.import_module('isolated_core.providers.live_provider')
    isolated.load('session.mic_manager_wrapper')
    module = isolated.load('session.live_session')
    handler = isolated.load('session.audio_handler').AudioHandler
    monkeypatch.setattr(audio_module, 'aec_heard_audio_ms', lambda *_: 0)
    monkeypatch.setattr(audio_module, 'stop_playback', Mock())
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.bluetooth',
        SimpleNamespace(mode=SimpleNamespace(active=lambda: False)),
    )
    audio_module.playback_done_event.set()
    s = SimpleNamespace(
        ws=Socket(),
        loop=None,
        session_active=asyncio.Event(),
        state=SimpleNamespace(assistant_speaking=False, response_active=False),
        last_activity=[time.time()],
        interrupt_event=asyncio.Event(),
        _set_speaking_state=Mock(),
        _set_listening_state=Mock(),
        stop_session=AsyncMock(),
        realtime_ai_provider=provider.LiveProvider('test'),
        function_handler=SimpleNamespace(handle=AsyncMock()),
    )
    s.session_active.set()
    s.audio_handler = handler(s)
    engine = module.LiveSession(s)
    s._ws_send_json = engine.application_event
    return SimpleNamespace(
        provider=provider,
        module=module,
        engine=engine,
        session=s,
        audio=audio_module,
        isolated=isolated,
    )


def test_live_configuration_is_not_realtime(live):
    tools = [
        {
            'type': 'function',
            'name': 'web_search',
            'description': 'Look up current facts',
            'parameters': {'type': 'object', 'properties': {}},
        },
        {'type': 'function', 'name': 'conversation_state'},
    ]
    instructions = '# Role & Objective\nSpeak like Billy.\n---\n# Tools\nKeep confirmations.\n---\n# Current User Context\nThom'
    p = live.provider.LiveProvider('key', backend_model='gpt-6-luna')
    cfg = p.session_config(instructions, tools, voice='vesper')
    assert p._get_websocket_uri() == 'wss://api.openai.com/v1/live/sessions'
    assert cfg['model'] == 'gpt-live-1'
    assert cfg['audio'] == {
        'format': {'type': 'audio/pcm', 'rate': 24000},
        'output': {'voice': 'vesper'},
    }
    assert 'Keep confirmations.' not in cfg['instructions']
    assert 'Speak like Billy.' in cfg['instructions'] and 'Thom' in cfg['instructions']
    backend = cfg['delegation']['responses']
    assert 'Keep confirmations.' in backend['instructions']
    assert backend['parallel_tool_calls'] is False
    assert [t['name'] for t in backend['tools']] == ['web_search']
    assert backend['tools'][0]['strict'] is False
    assert 'turn_detection' not in json.dumps(cfg)


def test_connect_waits_for_session_started_and_cleans_up_errors(live):
    async def run():
        p = live.provider.LiveProvider('key')
        ws = Socket()
        p._connect_websocket = AsyncMock(return_value=ws)
        task = asyncio.create_task(p.connect('Billy', []))
        await asyncio.sleep(0)
        assert [m['type'] for m in ws.sent] == ['session.start']
        assert not task.done()
        await ws.inbox.put({'type': 'session.started', 'session': {'id': 'live1'}})
        assert await task is ws
        ws2 = Socket()
        p._connect_websocket.return_value = ws2
        await ws2.inbox.put({'type': 'error', 'error': {'message': 'No access'}})
        with pytest.raises(RuntimeError, match='No access'):
            await p.connect('Billy', [])
        assert ws2.closed
        with pytest.raises(ValueError, match='text-only'):
            await p.connect('Billy', [], text_only_mode=True)

    asyncio.run(run())


def test_mic_sends_live_pcm_and_silence_without_aec(live, monkeypatch):
    async def run():
        s = live.session
        s.session_initialized = True
        mic = live.module.LiveMicrophone(s)
        mic._uplink = Mock()
        monkeypatch.setattr(live.audio, 'echo_cancellation_active', lambda: False)
        live.audio.playback_done_event.clear()
        mic._process_microphone(np.ones(240, dtype=np.int16) * 2000)
        assert np.all(mic._uplink.submit.call_args.args[0] == 0)
        await mic._send_audio(np.zeros(240, dtype=np.int16))
        assert s.ws.sent[-1]['type'] == 'session.input_audio.append'
        assert len(base64.b64decode(s.ws.sent[-1]['audio'])) == 480
        live.audio.playback_done_event.set()
        mic._process_microphone(np.ones(240, dtype=np.int16) * 2000)
        assert np.all(mic._uplink.submit.call_args.args[0] == 2000)

    asyncio.run(run())


def backend(kind, **fields):
    return {
        'type': 'response.event',
        'delegation_id': 'd1',
        'event': {'type': kind, **fields},
    }


def test_tools_execute_once_without_blocking_audio_and_continue_after_all_results(live):
    async def run():
        engine, s = live.engine, live.session
        engine.tools = [{'name': 'get_mood'}]
        release = asyncio.Event()
        began = asyncio.Event()

        async def handle(name, arguments, call_id):
            began.set()
            await release.wait()
            await s._ws_send_json({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': '{"ok":true}',
                },
            })
            await s._ws_send_json({
                'type': 'response.create',
                'response': {'tools': []},
            })

        s.function_handler.handle = AsyncMock(side_effect=handle)
        await engine.handle(backend('response.created', response={'id': 'r1'}))
        item = {
            'type': 'function_call',
            'name': 'get_mood',
            'arguments': '{}',
            'call_id': 'c1',
        }
        event = backend(
            'response.output_item.done', output_index=0, sequence_number=8, item=item
        )
        await engine.handle(event)
        await engine.handle(event)
        await began.wait()
        await engine.handle({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(b'\x40\x00' * 240).decode(),
        })
        assert s.state.assistant_speaking
        await engine.handle(
            backend('response.completed', response={'id': 'r1', 'output': []})
        )
        assert not s.ws.sent
        assert s.state.assistant_speaking  # backend completion is not speech completion
        release.set()
        await asyncio.gather(*engine.tasks)
        assert [m['type'] for m in s.ws.sent] == [
            'response.item.create',
            'response.create',
        ]
        assert s.ws.sent[0]['item']['call_id'] == 'c1'
        assert s.function_handler.handle.await_count == 1
        assert not engine.responses
        assert not s.audio_handler.audio_buffer  # no session-long PCM accumulation

    asyncio.run(run())


@pytest.mark.parametrize(
    'name,args', [('not_enabled', '{}'), ('get_mood', '[]'), ('get_mood', '{bad')]
)
def test_invalid_tool_calls_return_errors_without_running_handler(live, name, args):
    async def run():
        e = live.engine
        e.tools = [{'name': 'get_mood'}]
        await e.handle(backend('response.created', response={'id': 'r1'}))
        await e.handle(
            backend(
                'response.output_item.done',
                output_index=0,
                item={
                    'type': 'function_call',
                    'name': name,
                    'arguments': args,
                    'call_id': 'c1',
                },
            )
        )
        await e.handle(backend('response.completed', response={'id': 'r1'}))
        await asyncio.gather(*e.tasks)
        live.session.function_handler.handle.assert_not_awaited()
        assert json.loads(live.session.ws.sent[0]['item']['output'])['ok'] is False
        assert live.session.ws.sent[-1] == {'type': 'response.create'}

    asyncio.run(run())


def test_close_waits_for_final_usage_and_no_late_tool_result(live):
    async def run():
        e, s = live.engine, live.session
        e.receiver = asyncio.create_task(e.receive())
        close = asyncio.create_task(e.close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert s.ws.sent[-1] == {'type': 'session.close'}
        ws = s.ws
        assert not ws.closed
        await ws.inbox.put({'type': 'session.closed', 'usage': {'duration': 12}})
        await close
        await e.receiver
        assert ws.closed and s.ws is None and e.closed.is_set()
        with pytest.raises(asyncio.CancelledError):
            await e.send({'type': 'response.create'})

    asyncio.run(run())


def test_output_and_validation_errors_fail_explicitly(live):
    async def run():
        with pytest.raises(RuntimeError, match='Backend model unavailable'):
            await live.engine.handle({
                'type': 'error',
                'error': {
                    'code': 'responses_validation_failed',
                    'message': 'Backend model unavailable',
                },
            })
        with pytest.raises(ValueError, match='Unsupported'):
            await live.engine.application_event({'type': 'input_audio_buffer.commit'})

    asyncio.run(run())


def test_shutdown_cancels_pending_tools(live):
    async def run():
        e, s = live.engine, live.session
        e.tools = [{'name': 'get_mood'}]
        started = asyncio.Event()

        async def pending(*args):
            started.set()
            await asyncio.Event().wait()

        s.function_handler.handle.side_effect = pending
        await e.handle(backend('response.created', response={'id': 'r1'}))
        await e.handle(
            backend(
                'response.output_item.done',
                output_index=0,
                item={
                    'type': 'function_call',
                    'name': 'get_mood',
                    'arguments': '{}',
                    'call_id': 'c1',
                },
            )
        )
        await started.wait()
        tasks = list(e.tasks)
        await e.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert tasks[0].cancelled()

    asyncio.run(run())


def test_silent_output_does_not_extend_inactivity_or_suppress_recovery(live):
    async def run():
        e, s = live.engine, live.session
        s.last_activity[0] = 123
        e.last_output = 0
        await e.handle({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(bytes(480)).decode(),
        })
        assert not s.state.assistant_speaking
        assert s.last_activity[0] == 123 and e.last_output == 0
        assert not e.audible_intervals
        e.drop_output = True
        await e.handle({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(bytes(480)).decode(),
        })
        assert e.last_output == 0

    asyncio.run(run())


def test_speaking_ends_when_audio_was_played_not_when_backend_completes(
    live, monkeypatch
):
    async def run():
        e, s = live.engine, live.session
        await e.handle({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(b'\x40\x00' * 240).decode(),
        })
        assert s.state.assistant_speaking
        monkeypatch.setattr(live.audio, 'aec_heard_audio_ms', lambda *_: 10)
        task = asyncio.create_task(e.watch_playback())
        await asyncio.sleep(0.15)
        assert s.state.assistant_speaking  # A short gap is still the same answer.
        assert (
            live.audio.playback_done_event.is_set()
        )  # Do not delay microphone capture.
        await asyncio.sleep(0.7)
        assert not s.state.assistant_speaking
        assert live.audio.playback_done_event.is_set()
        s._set_listening_state.assert_called_once()
        s.session_active.clear()
        await task

    asyncio.run(run())


@pytest.mark.parametrize('kind', ['prompt', 'literal'])
def test_session_lifecycle_and_mqtt_literal_is_not_a_backend_command(
    live, monkeypatch, kind
):
    s, e = live.session, live.engine
    fake_mic = Mock()
    monkeypatch.setattr(live.module, 'LiveMicrophone', Mock(return_value=fake_mic))
    monkeypatch.setattr(live.audio, 'recover_echo_cancellation', lambda: False)
    monkeypatch.setattr(live.audio, 'ensure_playback_worker_started', Mock())
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(get_current_persona_voice=lambda: 'marin')
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(
            get_instructions_with_user_context=lambda *_: 'Be Billy',
            get_tools_for_current_mode=lambda: [],
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.bluetooth',
        SimpleNamespace(mode=SimpleNamespace(active=lambda: False)),
    )
    s.user_handler = SimpleNamespace(auto_identify_default_user=AsyncMock())
    s.persona_handler = SimpleNamespace(reload_persona_from_profile=AsyncMock())
    s.state.reset_for_new_session = Mock()
    s.kickoff_text = 'Turn off all lights'
    s.kickoff_kind = kind
    s._set_idle_state = Mock()
    s.error_handler = SimpleNamespace(play_error_sound=AsyncMock())
    s.realtime_ai_provider.connect = AsyncMock(return_value=s.ws)
    s.realtime_ai_provider.generate_audio_clip = AsyncMock(return_value=b'\x01\x00')

    async def drain(**_):
        while not live.audio.playback_queue.empty():
            live.audio.playback_queue.get_nowait()
            live.audio.playback_queue.task_done()
        return True

    monkeypatch.setattr(live.audio.playback_queue, 'wait', drain)

    async def run():
        ws = s.ws
        original_send = ws.send

        async def send(raw):
            await original_send(raw)
            if json.loads(raw)['type'] == 'session.close':
                await ws.inbox.put({'type': 'session.closed', 'usage': {}})

        ws.send = send

        async def stop(**_):
            s.session_active.clear()
            fake_mic.stop()
            await e.close()

        s.stop_session = stop
        running = asyncio.create_task(e.start())
        async with asyncio.timeout(2):
            while e.receiver is None:
                await asyncio.sleep(0.01)
        assert s.session_initialized
        if kind == 'literal':
            s.realtime_ai_provider.generate_audio_clip.assert_awaited_once()
            assert (
                s.realtime_ai_provider.connect.call_args.kwargs['announcement']
                == s.kickoff_text
            )
            assert ws.sent == []  # no tool command, even for imperative text
        else:
            s.realtime_ai_provider.generate_audio_clip.assert_not_awaited()
            assert [m['type'] for m in ws.sent] == [
                'response.item.create',
                'response.create',
            ]
        await stop()
        await asyncio.wait_for(running, 2)
        assert ws.closed and s.ws is None
        fake_mic.start.assert_called_once()
        fake_mic.flush_prebuffer.assert_called_once()
        s.error_handler.play_error_sound.assert_not_awaited()
        s._set_idle_state.assert_called_once()

    asyncio.run(run())


def test_live_configuration_survives_validation_and_reload(tmp_path):
    from billy_runtime.configuration import patch_env, validate

    env = tmp_path / '.env'
    env.write_text('REALTIME_AI_PROVIDER=openai\nOPENAI_MODEL=existing-model\n')
    patch_env(
        env,
        {
            'REALTIME_AI_PROVIDER': 'gpt-live',
            'OPENAI_LIVE_MODEL': 'gpt-live-1',
            'OPENAI_LIVE_BACKEND_MODEL': 'gpt-6-luna',
        },
    )
    parsed = validate(env.read_text())
    assert parsed['REALTIME_AI_PROVIDER'] == 'gpt-live'
    assert parsed['OPENAI_MODEL'] == 'existing-model'


def test_live_search_respects_opt_in_and_api_key(live, monkeypatch):
    cfg = live.isolated.config
    cfg.CAMERA_HARDWARE = 'none'
    cfg.OPENAI_API_KEY = 'test'
    cfg.REALTIME_AI_PROVIDER = 'gpt-live'
    cfg.WEB_SEARCH_ENABLED = True
    cfg.is_conversation_state_enabled = lambda: False
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.base_tools',
        SimpleNamespace(
            get_base_tools=lambda: [{'type': 'function', 'name': 'web_search'}],
            get_user_tools=lambda: [],
        ),
    )
    module = live.isolated.load('session.tool_manager')
    manager = module.ToolManager()
    monkeypatch.setattr(manager, '_has_news_sources', lambda: False)
    monkeypatch.setenv('REALTIME_AI_PROVIDER', 'gpt-live')
    monkeypatch.setenv('WEB_SEARCH_ENABLED', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'test')
    assert manager.get_tools('guest')[0]['name'] == 'web_search'
    monkeypatch.setenv('WEB_SEARCH_ENABLED', 'false')
    assert manager.get_tools('guest') == []


def test_live_does_not_open_devices_while_bluetooth_owns_audio(live, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.bluetooth',
        SimpleNamespace(mode=SimpleNamespace(active=lambda: True)),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(persona_manager=Mock()),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(
            get_instructions_with_user_context=Mock(), get_tools_for_current_mode=Mock()
        ),
    )
    live.session.realtime_ai_provider.connect = AsyncMock()
    asyncio.run(live.engine.start())
    live.session.realtime_ai_provider.connect.assert_not_awaited()


def test_live_cleanup_does_not_stop_bluetooth_playback(live, monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.bluetooth',
        SimpleNamespace(mode=SimpleNamespace(active=lambda: True)),
    )
    asyncio.run(live.engine.close())
    live.audio.stop_playback.assert_not_called()


def test_bluetooth_handoff_close_respects_short_deadline(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        e.receiver = asyncio.create_task(e.receive())
        started = time.monotonic()
        await e.close(timeout=0.02)
        assert time.monotonic() - started < 0.5
        assert ws.closed and s.ws is None
        assert not e.closed.is_set()  # no false claim of final usage
        e.receiver.cancel()
        await asyncio.gather(e.receiver, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'transport_error'])
def test_live_transport_close_failure_does_not_escape_cleanup(live, failure):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        ws.transport = Mock()
        e.closed.set()  # Final usage already arrived; transport close can still fail.
        if failure == 'timeout':

            async def hang():
                await asyncio.Event().wait()

            ws.close = hang
        else:
            ws.close = AsyncMock(side_effect=OSError('transport gone'))
        await e.close(timeout=0.02)
        assert s.ws is None
        assert e.closed.is_set()
        ws.transport.abort.assert_called_once()
        live.audio.stop_playback.assert_called_once()

    asyncio.run(run())


def test_cancelled_close_keeps_cancellation_and_aborts_stuck_transport(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        ws.transport = Mock()
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        ws.close = hang
        e.closed.set()
        closing = asyncio.create_task(e.close())
        await started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert s.ws is None
        ws.transport.abort.assert_called_once()
        live.audio.stop_playback.assert_called_once()

    asyncio.run(run())


def test_concurrent_close_waits_for_existing_transport_cleanup(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        started, release = asyncio.Event(), asyncio.Event()

        async def close():
            started.set()
            await release.wait()
            ws.closed = True

        ws.close = close
        e.closed.set()
        first = asyncio.create_task(e.close())
        await started.wait()
        second = asyncio.create_task(e.close())
        await asyncio.sleep(0.01)
        try:
            assert not second.done()
        finally:
            release.set()
            await asyncio.gather(first, second)
        assert ws.closed and s.ws is None

    asyncio.run(run())


def test_external_stop_keeps_receiver_alive_until_cleanup_finishes(live, monkeypatch):
    import threading

    from test_session_cleanup import stop_method

    s, e = live.session, live.engine
    fake_mic = Mock()
    started, release = threading.Event(), threading.Event()

    def stop_mic():
        started.set()
        release.wait(2)

    fake_mic.stop.side_effect = stop_mic
    monkeypatch.setattr(live.module, 'LiveMicrophone', Mock(return_value=fake_mic))
    monkeypatch.setattr(live.audio, 'recover_echo_cancellation', lambda: False)
    monkeypatch.setattr(live.audio, 'ensure_playback_worker_started', Mock())
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(get_current_persona_voice=lambda: 'ballad')
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(
            get_instructions_with_user_context=lambda *_: 'Be Billy',
            get_tools_for_current_mode=lambda: [],
        ),
    )
    s.user_handler = SimpleNamespace(auto_identify_default_user=AsyncMock())
    s.persona_handler = SimpleNamespace(reload_persona_from_profile=AsyncMock())
    s.state.reset_for_new_session = Mock()
    s.kickoff_text = None
    s.kickoff_kind = None
    s._set_idle_state = Mock()
    s.error_handler = SimpleNamespace(play_error_sound=AsyncMock())
    s.realtime_ai_provider.connect = AsyncMock(return_value=s.ws)
    s.tool_calls = Mock()
    s._tool_args_buffer = {}
    s._interaction_count_recorded = False
    s._close_ws = lambda **kwargs: e.close(**kwargs)
    users = Mock()
    stop = stop_method(users)
    s.stop_session = lambda **kwargs: stop(s, **kwargs)

    async def run():
        ws = s.ws
        original_send = ws.send

        async def send(raw):
            await original_send(raw)
            if json.loads(raw)['type'] == 'session.close':
                await ws.inbox.put({'type': 'session.closed', 'usage': {'seconds': 5}})

        ws.send = send
        running = asyncio.create_task(e.start())
        stopping = None
        try:
            async with asyncio.timeout(2):
                while e.receiver is None:
                    await asyncio.sleep(0.001)
            stopping = asyncio.create_task(s.stop_session(reason='button'))
            while not started.is_set():
                await asyncio.sleep(0.001)
            # Capture shutdown can yield before Live.close marks the socket
            # closing. Late audio/tools must be ignored in this gap too.
            s.audio_handler._queued_output_bytes = 48 * 60_000
            await ws.inbox.put({
                'type': 'session.output_audio.delta',
                'delta': base64.b64encode(bytes(480)).decode(),
            })
            await ws.inbox.put({
                'type': 'response.event',
                'event': {'type': 'response.created', 'response': {'id': 'late'}},
            })
            await ws.inbox.put({
                'type': 'session.output_transcript.delta',
                'delta': 'Goodbye!',
            })
            # The playback monitor has exited, but microphone shutdown has not.
            await asyncio.sleep(0.15)
            assert live.audio.playback_queue.empty()
            assert not e.responses
            assert e.transcript.lines['billy']['text'] == 'Goodbye!'
            assert not running.done()
            assert not e.receiver.done()
            release.set()
            await asyncio.wait_for(asyncio.gather(running, stopping), 2)
            assert ws.closed and s.ws is None and e.closed.is_set()
            assert s._stop_complete.is_set()
            users.increment_current_user_interaction_count.assert_called_once()
            s.error_handler.play_error_sound.assert_not_awaited()
        finally:
            release.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await asyncio.gather(running, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize('closing', [True, False])
def test_transport_loss_is_only_quiet_during_requested_shutdown(live, closing):
    from websockets.exceptions import ConnectionClosedError

    class LostSocket(Socket):
        async def __anext__(self):
            raise ConnectionClosedError(None, None)

    live.session.ws = LostSocket()
    live.engine.closing = closing
    if closing:
        asyncio.run(live.engine.receive())
    else:
        with pytest.raises(ConnectionClosedError):
            asyncio.run(live.engine.receive())
    assert not live.engine.closed.is_set()


def test_short_concurrent_close_aborts_stalled_handshake_for_takeover(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        started, aborted = asyncio.Event(), asyncio.Event()
        ws.transport = SimpleNamespace(abort=aborted.set)

        async def close():
            started.set()
            await aborted.wait()
            ws.closed = True

        ws.close = close
        e.closed.set()
        first = asyncio.create_task(e.close())
        await started.wait()
        before = time.monotonic()
        await e.close(timeout=0.02)
        await first
        assert time.monotonic() - before < 0.5
        assert ws.closed and s.ws is None and e.close_complete.is_set()

    asyncio.run(run())


def test_cancellation_during_finalization_is_not_replaced_by_close_timeout(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        ws.transport = Mock()

        async def hang():
            await asyncio.Event().wait()

        ws.close = hang
        e.receiver = asyncio.create_task(e.receive())
        closing = asyncio.create_task(e.close(timeout=0.02))
        while not ws.sent:
            await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not e.closed.is_set()
        assert s.ws is None and e.close_complete.is_set()
        ws.transport.abort.assert_called_once()
        e.receiver.cancel()
        await asyncio.gather(e.receiver, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize('delegation', ['d1', None])
def test_live_tools_use_delegation_when_item_has_no_response_id(live, delegation):
    async def run():
        e, s = live.engine, live.session
        e.tools = [{'name': 'web_search'}]

        async def handler(name, args, call_id):
            assert name == 'web_search'
            assert json.loads(args) == {'query': 'Bitcoin price'}
            await s._ws_send_json({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': '{"result":"test quote"}',
                },
            })

        s.function_handler.handle.side_effect = handler

        def event(kind, **fields):
            return dict(backend(kind, **fields), delegation_id=delegation)

        for response_id, call_id in [('r1', 'c1'), ('r2', 'c2')]:
            await e.handle(
                event('response.created', response={'id': response_id, 'output': []})
            )
            await e.handle(
                event(
                    'response.function_call_arguments.done',
                    arguments='{}',
                    item_id='fc_' + call_id,
                )
            )
            assert s.function_handler.handle.await_count == (
                0 if response_id == 'r1' else 1
            )
            call = event(
                'response.output_item.done',
                sequence_number=9,
                output_index=0,
                item={
                    'type': 'function_call',
                    'id': 'fc_' + call_id,
                    'name': 'web_search',
                    'arguments': '{"query":"Bitcoin price"}',
                    'call_id': call_id,
                },
            )
            await e.handle(call)
            await e.handle(
                event('response.completed', response={'id': response_id, 'output': []})
            )
            await asyncio.gather(*e.tasks)
            await e.handle(call)  # replay after completion must not repeat an action
        assert s.function_handler.handle.await_count == 2
        assert [m['type'] for m in s.ws.sent] == [
            'response.item.create',
            'response.create',
        ] * 2
        assert [m['item']['call_id'] for m in s.ws.sent if 'item' in m] == ['c1', 'c2']
        assert not e.responses

    asyncio.run(run())


def test_live_tool_items_are_matched_to_their_own_delegation(live):
    async def run():
        e = live.engine
        e.tools = [{'name': 'get_mood'}]
        e.run_tool = AsyncMock()
        for response_id, delegation in [('r1', 'd1'), ('r2', 'd2')]:
            await e.handle(
                dict(
                    backend('response.created', response={'id': response_id}),
                    delegation_id=delegation,
                )
            )
        for call_id, delegation in [('c2', 'd2'), ('c1', 'd1')]:
            item = {
                'type': 'function_call',
                'name': 'get_mood',
                'arguments': '{}',
                'call_id': call_id,
            }
            await e.handle(
                dict(
                    backend('response.output_item.done', item=item),
                    delegation_id=delegation,
                )
            )
        await asyncio.gather(*e.tasks)
        assert [
            (call.args[0]['call_id'], call.args[1])
            for call in e.run_tool.call_args_list
        ] == [('c2', 'r2'), ('c1', 'r1')]

    asyncio.run(run())


@pytest.mark.parametrize(
    'case',
    ['unknown', 'missing', 'ambiguous', 'mismatch', 'explicit_unknown', 'completed'],
)
def test_live_does_not_guess_an_unmatched_tool_response(live, case):
    async def run():
        e = live.engine
        e.tools = [{'name': 'get_mood'}]
        await e.handle(backend('response.created', response={'id': 'r1'}))
        event = backend(
            'response.output_item.done',
            item={
                'type': 'function_call',
                'name': 'get_mood',
                'arguments': '{}',
                'call_id': 'c1',
            },
        )
        if case == 'unknown':
            event['delegation_id'] = 'unknown'
        elif case == 'missing':
            event.pop('delegation_id')
        elif case == 'ambiguous':
            await e.handle(backend('response.created', response={'id': 'r2'}))
        elif case == 'mismatch':
            event['event']['response_id'] = 'r1'
            event['delegation_id'] = 'd2'
        elif case == 'explicit_unknown':
            event['event']['response_id'] = 'unknown'
        elif case == 'completed':
            await e.handle(
                backend('response.completed', response={'id': 'r1', 'output': []})
            )
        with pytest.raises(RuntimeError, match='backend response'):
            await e.handle(event)
        live.session.function_handler.handle.assert_not_awaited()
        assert not e.tasks

    asyncio.run(run())


def test_live_multiple_functions_continue_only_after_all_results(live):
    async def run():
        e, s = live.engine, live.session
        e.tools = [{'name': 'get_mood'}]
        second = asyncio.Event()
        release = asyncio.Event()

        async def handler(name, args, call_id):
            if call_id == 'c2':
                second.set()
                await release.wait()
            await s._ws_send_json({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': '{}',
                },
            })

        s.function_handler.handle.side_effect = handler
        await e.handle(backend('response.created', response={'id': 'r1'}))
        for call_id in ('c1', 'c2'):
            await e.handle(
                backend(
                    'response.output_item.done',
                    item={
                        'type': 'function_call',
                        'name': 'get_mood',
                        'arguments': '{}',
                        'call_id': call_id,
                    },
                )
            )
        await e.handle(
            backend('response.completed', response={'id': 'r1', 'output': []})
        )
        await second.wait()
        assert [m['type'] for m in s.ws.sent] == ['response.item.create']
        release.set()
        await asyncio.gather(*e.tasks)
        assert [m['type'] for m in s.ws.sent] == [
            'response.item.create',
            'response.item.create',
            'response.create',
        ]

    asyncio.run(run())


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('mode', ['guest', 'user'])
@pytest.mark.parametrize('fallback', [True, False])
def test_mood_setting_changes_live_startup_instructions(
    live, monkeypatch, enabled, mode, fallback
):
    isolated = live.isolated
    isolated.config.INSTRUCTIONS = 'Fallback Billy persona'
    isolated.config.MOOD_INSTRUCTIONS_ENABLED = enabled
    isolated.config._filter_vision_instruction_line = lambda text: text
    isolated.config.get_tool_instructions = lambda: 'Use allowed tools.'
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.mood',
        SimpleNamespace(
            mood_manager=SimpleNamespace(
                get_prompt_section=lambda: '# Current Mood\nPlayful mood marker'
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(
                load_persona=lambda name: {} if fallback else {'meta': {'name': name}},
                get_persona_instructions=lambda name: 'Be Billy',
            )
        ),
    )
    builder = isolated.load('session.instruction_builder')
    profile = SimpleNamespace(
        data={'USER_INFO': {'preferred_persona': 'billy'}},
        get_context_string=lambda: 'Test user',
    )
    instructions = builder.InstructionBuilder().build(
        builder.InstructionContext(mode, 'billy', profile)
    )
    session = live.session.realtime_ai_provider.session_config(instructions, [])
    assert ('Playful mood marker' in session['instructions']) == enabled
    assert (
        'Playful mood marker' in session['delegation']['responses']['instructions']
    ) == enabled


def test_live_transcripts_go_to_captions_without_journal_fragments(live):
    async def run():
        live.module.logger.info.reset_mock()
        for kind, text in [('input', 'Hello'), ('output', 'Hi'), ('input', ' Billy')]:
            await live.engine.handle({
                'type': f'session.{kind}_transcript.delta',
                'delta': text,
            })
        assert live.engine.transcript.lines['user']['text'] == 'Hello Billy'
        assert live.engine.transcript.lines['billy']['text'] == 'Hi'
        live.module.logger.info.assert_not_called()
        await live.engine.close(timeout=0.01)
        assert live.engine.transcript.closed and not live.engine.transcript.lines

    asyncio.run(run())


def test_live_small_audio_gaps_do_not_flicker_mqtt_state(live, monkeypatch):
    async def run():
        e, s = live.engine, live.session
        monkeypatch.setattr(
            live.audio,
            'aec_heard_audio_ms',
            lambda *_: s.audio_handler._queued_output_bytes / 48,
        )
        task = asyncio.create_task(e.watch_playback())
        for _ in range(12):
            await e.handle({
                'type': 'session.output_audio.delta',
                'delta': base64.b64encode(b'\x40\x00' * 2400).decode(),
            })
            await asyncio.sleep(0.11)
            assert s.state.assistant_speaking
        s._set_speaking_state.assert_called_once()
        s._set_listening_state.assert_not_called()
        # Continued silence must not keep the state speaking indefinitely.
        for _ in range(9):
            await e.handle({
                'type': 'session.output_audio.delta',
                'delta': base64.b64encode(bytes(4800)).decode(),
            })
            await asyncio.sleep(0.1)
        s._set_listening_state.assert_called_once()
        s.session_active.clear()
        await task

    asyncio.run(run())


@pytest.mark.parametrize(
    'level,visible',
    [('ERROR', False), ('WARNING', False), ('INFO', True), ('VERBOSE', True)],
)
def test_live_final_transcript_uses_info_level_even_with_captions_disabled(
    live, monkeypatch, capsys, level, visible
):
    real_logger = live.isolated.load('logger').logger
    monkeypatch.setattr(live.module, 'logger', real_logger)
    monkeypatch.setenv('LOG_LEVEL', level)
    live.engine.transcript.enabled = False  # No web UI/caption subscriber needed.

    async def run():
        e = live.engine
        for kind, text in [
            ('input', 'Hello'),
            ('output', 'Hi'),
            ('input', ' Billy'),
            ('output', '!'),
        ]:
            await e.handle({'type': f'session.{kind}_transcript.delta', 'delta': text})
        assert capsys.readouterr().out == ''
        e.closed.set()
        await e.close()
        await e.close()
        e._log_transcript()  # start() finally must not log a second copy.

    asyncio.run(run())
    output = capsys.readouterr().out
    if visible:
        assert output.count('GPT-Live session transcript:') == 1
        assert 'User: Hello Billy\nBilly: Hi!' in output
    else:
        assert 'session transcript' not in output and 'Hello Billy' not in output


def test_live_close_drains_final_transcript_without_playing_late_audio_or_tools(live):
    async def run():
        e, s = live.engine, live.session
        ws = s.ws
        live.module.logger.info.reset_mock()
        await e.handle({'type': 'session.output_transcript.delta', 'delta': 'Good'})
        e.receiver = asyncio.create_task(e.receive())
        closing = asyncio.create_task(e.close(timeout=1))
        await asyncio.sleep(0)
        assert e.closing and e.transcript.closed
        await ws.inbox.put({'type': 'session.output_transcript.delta', 'delta': 'bye!'})
        await ws.inbox.put({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(bytes(480)).decode(),
        })
        await ws.inbox.put({
            'type': 'response.event',
            'event': {'type': 'response.created', 'response': {'id': 'late'}},
        })
        await ws.inbox.put({'type': 'session.closed', 'usage': {}})
        await closing
        await e.receiver
        assert live.audio.playback_queue.empty()
        assert not e.responses and not e.transcript.lines
        assert [
            c.args[0]
            for c in live.module.logger.info.call_args_list
            if c.args[0].startswith('GPT-Live session transcript:')
        ] == ['GPT-Live session transcript:\nBilly: Goodbye!']

    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'cancelled'])
def test_live_transcript_logged_once_even_when_transport_shutdown_fails(live, failure):
    async def run():
        e, s = live.engine, live.session
        await e.handle({
            'type': 'session.input_transcript.delta',
            'delta': 'A last question',
        })
        e.closed.set()
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        s.ws.close = hang
        s.ws.transport = Mock()
        task = asyncio.create_task(e.close(timeout=0.02))
        await started.wait()
        if failure == 'cancelled':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        e._log_transcript()
        assert [
            c.args[0]
            for c in live.module.logger.info.call_args_list
            if c.args[0].startswith('GPT-Live session transcript:')
        ] == ['GPT-Live session transcript:\nUser: A last question']
        assert e.close_complete.is_set()

    asyncio.run(run())


def test_final_transcript_uses_log_level_at_session_end(live, monkeypatch, capsys):
    monkeypatch.setattr(live.module, 'logger', live.isolated.load('logger').logger)

    async def run():
        monkeypatch.setenv('LOG_LEVEL', 'WARNING')
        await live.engine.handle({
            'type': 'session.input_transcript.delta',
            'delta': 'Keep the beginning too.',
        })
        monkeypatch.setenv('LOG_LEVEL', 'INFO')
        await live.engine.handle({
            'type': 'session.output_transcript.delta',
            'delta': 'Certainly.',
        })
        live.engine.closed.set()
        await live.engine.close()

    asyncio.run(run())
    output = capsys.readouterr().out
    assert output.count('GPT-Live session transcript:') == 1
    assert 'User: Keep the beginning too.\nBilly: Certainly.' in output


@pytest.mark.parametrize(
    'shutdown', ['inactive', 'closing', 'invalid_generation', 'invalidated_during_read']
)
@pytest.mark.parametrize(
    'pcm', [bytes(480), b'\x40\x00' * 240], ids=['silence', 'speech']
)
def test_live_late_audio_cannot_restart_playback_or_report_false_backlog(
    live, monkeypatch, shutdown, pcm
):
    e, s = live.engine, live.session
    handler = s.audio_handler
    handler._playback_generation = 1
    handler._queued_output_bytes = 48 * 60_000
    if shutdown == 'inactive':
        s.session_active.clear()
    elif shutdown == 'closing':
        e.closing = True
    elif shutdown == 'invalid_generation':
        live.audio.echo_canceller.is_current_render_generation.return_value = False
    else:

        def heard(_generation):
            # Physical button/takeover resets playback from another thread.
            live.audio.echo_canceller.is_current_render_generation.return_value = False
            return 0

        monkeypatch.setattr(live.audio, 'aec_heard_audio_ms', heard)
    asyncio.run(
        e.handle({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(pcm).decode(),
        })
    )
    assert live.audio.playback_queue.empty()
    assert handler._queued_output_bytes == 48 * 60_000
    assert not e.audible_intervals
    assert not s.state.assistant_speaking
    s._set_speaking_state.assert_not_called()
    assert live.audio.playback_done_event.is_set()


def test_live_real_playback_backlog_still_fails(live):
    handler = live.session.audio_handler
    handler._playback_generation = 1
    handler._queued_output_bytes = 48 * 30_000
    with pytest.raises(RuntimeError, match='more than 30 seconds behind'):
        asyncio.run(
            live.engine.handle({
                'type': 'session.output_audio.delta',
                'delta': base64.b64encode(bytes(480)).decode(),
            })
        )
    assert live.audio.playback_queue.empty()


@pytest.fixture
def live_clock(live, monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        live.module,
        'time',
        SimpleNamespace(
            time=lambda: clock.now,
            monotonic=lambda: clock.now,
        ),
    )
    live.session.last_activity[0] = clock.now
    live.engine.started_at = clock.now
    return clock


@pytest.mark.parametrize('kind', ['input', 'output'])
@pytest.mark.parametrize('delta', ['', ' ', '\n', None])
def test_live_empty_transcript_events_do_not_restart_idle_timer(
    live, live_clock, kind, delta
):
    live_clock.now += 8
    asyncio.run(
        live.engine.handle({
            'type': f'session.{kind}_transcript.delta',
            'delta': delta,
        })
    )
    assert live.session.last_activity[0] == 1000


def test_live_delayed_output_transcript_does_not_restart_idle_timer(live, live_clock):
    live_clock.now += 8
    asyncio.run(
        live.engine.handle({
            'type': 'session.output_transcript.delta',
            'delta': 'Goodbye.',
        })
    )
    assert live.session.last_activity[0] == 1000
    assert live.engine.transcript.finish() == 'Billy: Goodbye.'


def test_live_recognized_user_words_restart_idle_timer(live, live_clock):
    live_clock.now += 8
    asyncio.run(
        live.engine.handle({
            'type': 'session.input_transcript.delta',
            'delta': 'One more thing.',
        })
    )
    assert live.session.last_activity[0] == 1008
    assert live.engine.transcript.finish() == 'User: One more thing.'


@pytest.mark.parametrize('threshold', [0, 500])
def test_live_silent_stream_stops_at_configured_timeout(
    live, live_clock, monkeypatch, threshold
):
    monkeypatch.setattr(live.module, 'SILENCE_THRESHOLD', threshold)

    async def run():
        e, s = live.engine, live.session
        s.session_initialized = True
        mic = live.module.LiveMicrophone(s)
        mic._uplink = Mock()
        monkeypatch.setattr(live.audio, 'echo_cancellation_active', lambda: False)

        async def tick(_seconds):
            live_clock.now += 1
            assert live_clock.now < 1015, 'Silence must not keep the session alive'
            mic._process_microphone(np.zeros(240, dtype=np.int16))
            await e.handle({
                'type': 'session.output_audio.delta',
                'delta': base64.b64encode(bytes(480)).decode(),
            })
            for kind in ['input', 'output']:
                await e.handle({
                    'type': f'session.{kind}_transcript.delta',
                    'delta': ' ',
                })
            if live_clock.now <= 1010:
                s.stop_session.assert_not_called()

        monkeypatch.setattr(live.module, 'asyncio', SimpleNamespace(sleep=tick))
        await e.watch_playback()
        assert live_clock.now == 1011
        s.stop_session.assert_awaited_once_with(reason='live_inactivity')
        assert mic._uplink.submit.call_count == 11

    asyncio.run(run())


@pytest.mark.parametrize('busy', ['playback', 'backend', 'tool'])
def test_live_idle_timeout_waits_for_playback_and_pending_work(
    live, live_clock, monkeypatch, busy
):
    async def run():
        e, s = live.engine, live.session
        if busy == 'playback':
            s.state.assistant_speaking = True
            e.audible_intervals.append((0, 1000))
        elif busy == 'backend':
            e.responses['r1'] = {'started': live_clock.now}
        else:
            e.tasks.add(object())

        async def tick(_seconds):
            live_clock.now += 1
            assert live_clock.now < 1015
            if live_clock.now == 1012:
                s.stop_session.assert_not_called()
                s.session_active.clear()

        monkeypatch.setattr(live.module, 'asyncio', SimpleNamespace(sleep=tick))
        await e.watch_playback()
        s.stop_session.assert_not_called()

    asyncio.run(run())


def test_live_microphone_activity_keeps_listening_then_silence_expires(
    live, live_clock, monkeypatch
):
    async def run():
        e, s = live.engine, live.session
        s.session_initialized = True
        mic = live.module.LiveMicrophone(s)
        mic._uplink = Mock()
        monkeypatch.setattr(live.audio, 'echo_cancellation_active', lambda: False)

        async def tick(_seconds):
            live_clock.now += 1
            assert live_clock.now < 1021
            mic._process_microphone(
                np.full(240, 2000 if live_clock.now <= 1008 else 100, dtype=np.int16)
            )
            if live_clock.now <= 1018:
                s.stop_session.assert_not_called()

        monkeypatch.setattr(live.module, 'asyncio', SimpleNamespace(sleep=tick))
        await e.watch_playback()
        assert live_clock.now == 1019
        s.stop_session.assert_awaited_once_with(reason='live_inactivity')

    asyncio.run(run())


def test_live_verbose_mic_report_explains_idle_delays(live, live_clock, monkeypatch):
    s, e = live.session, live.engine
    s._live = e
    s.session_initialized = True
    mic = live.module.LiveMicrophone(s)
    mic._uplink = Mock()
    monkeypatch.setattr(live.audio, 'echo_cancellation_active', lambda: False)
    live_clock.now += 3
    mic._process_microphone(np.full(240, 700, dtype=np.int16))
    live_clock.now += 2
    mic._process_microphone(np.zeros(240, dtype=np.int16))
    e.responses['r1'] = {'started': 1000}
    e.tasks.add(object())
    mic._report_uplink({'overflow': 0})
    message = live.module.logger.verbose.call_args.args[0]
    assert "'mic_rms': 0.0" in message
    assert "'speech_threshold': 500" in message
    assert "'mic_activity_age_seconds': 2.0" in message
    assert "'idle_seconds': 2.0" in message
    assert "'idle_timeout_seconds': 10" in message
    assert "'pending_responses': 1" in message
    assert "'pending_tools': 1" in message


@pytest.mark.parametrize('busy', ['playback', 'backend'])
def test_live_gets_full_listening_window_after_long_output_or_tool_work(
    live, live_clock, monkeypatch, busy
):
    async def run():
        e, s = live.engine, live.session
        if busy == 'playback':
            s.state.assistant_speaking = True
            e.audible_intervals.append((0, 1000))
            monkeypatch.setattr(
                live.audio,
                'aec_heard_audio_ms',
                lambda *_: 1000 if live_clock.now >= 1012 else 0,
            )
        else:
            await e.handle(backend('response.created', response={'id': 'r1'}))

        async def tick(_seconds):
            live_clock.now += 1
            assert live_clock.now < 1026
            if busy == 'backend' and live_clock.now == 1012:
                await e.handle(backend('response.completed', response={'id': 'r1'}))
            if live_clock.now <= 1022:
                s.stop_session.assert_not_called()

        monkeypatch.setattr(live.module, 'asyncio', SimpleNamespace(sleep=tick))
        await e.watch_playback()
        assert live_clock.now == (1024 if busy == 'playback' else 1023)
        s.stop_session.assert_awaited_once_with(reason='live_inactivity')

    asyncio.run(run())


def test_identifying_a_user_with_another_voice_continues_on_a_new_connection(
    live, monkeypatch
):
    """Live fixes the voice at session.start: switching to a user's persona voice
    hands the conversation to a new connection instead of keeping the old voice."""
    s, e = live.session, live.engine
    fake_mic = Mock()
    monkeypatch.setattr(live.module, 'LiveMicrophone', Mock(return_value=fake_mic))
    monkeypatch.setattr(live.audio, 'recover_echo_cancellation', lambda: False)
    voices = iter(['ballad', 'sillage'])
    current_voice = {'value': next(voices)}
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_current_persona_voice=lambda: current_voice['value']
            )
        ),
    )
    instructions = {'value': 'Be Billy as guest'}
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(
            get_instructions_with_user_context=lambda *_: instructions['value'],
            get_tools_for_current_mode=lambda: [
                {'type': 'function', 'name': 'identify_user'}
            ],
        ),
    )
    s.user_handler = SimpleNamespace(auto_identify_default_user=AsyncMock())
    s.persona_handler = SimpleNamespace(reload_persona_from_profile=AsyncMock())
    s.state.reset_for_new_session = Mock()
    s.kickoff_text = None
    s.kickoff_kind = None
    s._set_idle_state = Mock()
    s.error_handler = SimpleNamespace(play_error_sound=AsyncMock())
    old, new = s.ws, Socket()
    s.realtime_ai_provider.connect = AsyncMock(side_effect=[old, new])

    def closes_with_usage(ws, seconds):
        original = ws.send

        async def send(raw):
            await original(raw)
            if json.loads(raw)['type'] == 'session.close':
                await ws.inbox.put({
                    'type': 'session.closed',
                    'usage': {'seconds': seconds},
                })

        ws.send = send

    closes_with_usage(old, 6)
    closes_with_usage(new, 9)

    async def identify(name, arguments, call_id):
        # What user_handler does for a returning user with their own persona.
        instructions['value'] = 'Be Billy Bourguignon for Thom'
        current_voice['value'] = next(voices)
        await s._ws_send_json({
            'type': 'session.update',
            'session': {
                'type': 'realtime',
                'instructions': instructions['value'],
                'audio': {'output': {'voice': 'sillage'}},
            },
        })
        await s._ws_send_json({
            'type': 'conversation.item.create',
            'item': {
                'type': 'function_call_output',
                'call_id': call_id,
                'output': '{"status":"success","user":"Thom"}',
            },
        })
        await s._ws_send_json({
            'type': 'conversation.item.create',
            'item': {
                'type': 'message',
                'role': 'user',
                'content': [
                    {
                        'type': 'input_text',
                        'text': '[SYSTEM: Speak your greeting now to acknowledge the returning user]',
                    }
                ],
            },
        })
        await s._ws_send_json({'type': 'response.create'})

    s.function_handler.handle = AsyncMock(side_effect=identify)

    async def run():
        async def stop(**_):
            s.session_active.clear()
            await e.close()

        s.stop_session = AsyncMock(side_effect=stop)
        running = asyncio.create_task(e.start())
        async with asyncio.timeout(2):
            while e.receiver is None:
                await asyncio.sleep(0.01)
        assert s.realtime_ai_provider.connect.call_args.kwargs['voice'] == 'ballad'
        await old.inbox.put({
            'type': 'session.input_transcript.delta',
            'delta': "It's me, Thom",
        })
        await old.inbox.put({
            'type': 'session.output_transcript.delta',
            'delta': 'One sec.',
        })
        await old.inbox.put(backend('response.created', response={'id': 'r1'}))
        await old.inbox.put(
            backend(
                'response.output_item.done',
                item={
                    'type': 'function_call',
                    'name': 'identify_user',
                    'arguments': '{"name": "Thom", "confidence": "high"}',
                    'call_id': 'c1',
                },
            )
        )
        async with asyncio.timeout(2):
            while s.ws is not new:
                await asyncio.sleep(0.01)
            while not old.closed:
                await asyncio.sleep(0.01)
            while [m['type'] for m in new.sent][-1:] != ['response.create']:
                await asyncio.sleep(0.01)

        second = s.realtime_ai_provider.connect.call_args
        assert second.kwargs['voice'] == 'sillage'
        assert second.args[0].startswith('Be Billy Bourguignon for Thom')
        assert "User: It's me, Thom" in second.args[0]
        assert 'Billy: One sec.' in second.args[0]
        # The old connection is closed, never answered: its delegation is gone.
        assert [m['type'] for m in old.sent] == ['session.close']
        assert [m['type'] for m in new.sent] == [
            'response.item.create',
            'response.create',
        ]
        assert 'greeting' in new.sent[0]['item']['content'][0]['text']
        assert e.voice == 'sillage' and not e.responses
        assert not running.done(), 'the conversation continues on the new connection'

        # Late audio from a replaced connection is never played, and its end
        # does not end the session.
        queued = live.audio.playback_queue.qsize()
        late = Socket()
        await late.inbox.put({
            'type': 'session.output_audio.delta',
            'delta': base64.b64encode(b'\x40\x00' * 240).decode(),
        })
        await late.inbox.put({'type': 'session.closed', 'usage': {}})
        await e.receive(late)
        assert live.audio.playback_queue.qsize() == queued
        assert not e.closed.is_set()
        await stop()
        await asyncio.wait_for(running, 2)
        assert live.audio.playback_queue.qsize() == queued
        assert new.closed and s.ws is None
        s.error_handler.play_error_sound.assert_not_awaited()
        assert s.function_handler.handle.await_count == 1

    asyncio.run(run())


def test_a_tool_keeping_the_voice_answers_on_the_same_connection(live):
    async def run():
        e, s = live.engine, live.session
        e.voice = 'sillage'
        e.tools = [{'name': 'identify_user'}]
        s.realtime_ai_provider.connect = AsyncMock()

        async def identify(name, arguments, call_id):
            await s._ws_send_json({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'function_call_output',
                    'call_id': call_id,
                    'output': '{}',
                },
            })
            await s._ws_send_json({
                'type': 'session.update',
                'session': {
                    'instructions': 'x',
                    'audio': {'output': {'voice': 'sillage'}},
                },
            })

        s.function_handler.handle = AsyncMock(side_effect=identify)
        await e.handle(backend('response.created', response={'id': 'r1'}))
        await e.handle(
            backend(
                'response.output_item.done',
                item={
                    'type': 'function_call',
                    'name': 'identify_user',
                    'arguments': '{}',
                    'call_id': 'c1',
                },
            )
        )
        await e.handle(backend('response.completed', response={'id': 'r1'}))
        await asyncio.gather(*e.tasks)
        s.realtime_ai_provider.connect.assert_not_awaited()
        assert s.ws.sent[0] == {
            'type': 'response.item.create',
            'item': {'type': 'function_call_output', 'call_id': 'c1', 'output': '{}'},
        }

    asyncio.run(run())


def test_voice_prompt_asks_for_one_greeting_after_identification(live):
    voice, _ = live.provider.split_instructions(
        'Be Billy', [{'name': 'identify_user', 'description': 'Identify the user'}]
    )
    assert 'One greeting, not two.' in voice
    voice, _ = live.provider.split_instructions('Be Billy', [{'name': 'get_mood'}])
    assert 'One greeting' not in voice
