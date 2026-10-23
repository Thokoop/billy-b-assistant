"""Import and run the real session receiver with hardware/network boundaries faked."""

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock


def test_receiver_processes_speech_while_real_tool_runner_waits(
    audio_module, isolated, monkeypatch
):
    cfg = isolated.config
    for name, value in dict(
        AEC_BARGE_IN_SNR_DB=9,
        FOLLOW_UP_RETRY_LIMIT=1,
        MIC_TIMEOUT_SECONDS=10,
        MOOD_INSTRUCTIONS_ENABLED=False,
        MOOD_INSTRUCTIONS_MIN_INTERVAL_SECONDS=1,
        REALTIME_AI_PROVIDER='openai',
        RUN_MODE='normal',
        SERVER_VAD_PARAMS={'high': {}},
        SILENCE_THRESHOLD=500,
        TURN_EAGERNESS='high',
        is_conversation_state_enabled=lambda: True,
        HEAD_RETRACT_DELAY_SECONDS=1,
    ).items():
        setattr(cfg, name, value)
    for name in [
        'mood',
        'persona_manager',
        'profile_manager',
        'status_led',
        'mqtt',
        'providers.realtime',
    ]:
        monkeypatch.setitem(sys.modules, f'isolated_core.{name}', Mock())
    websockets = ModuleType('websockets')
    exceptions = ModuleType('websockets.exceptions')

    class Closed(Exception):
        pass

    exceptions.ConnectionClosed = Closed
    websockets.exceptions = exceptions
    monkeypatch.setitem(sys.modules, 'websockets', websockets)
    monkeypatch.setitem(sys.modules, 'websockets.exceptions', exceptions)
    package = ModuleType('isolated_core.session')
    package.__path__ = [str(Path(__file__).parents[1] / 'core/session')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    package.AudioHandler = isolated.load('session.audio_handler').AudioHandler
    package.SessionState = isolated.load('session.state_machine').SessionState
    for name in [
        'ErrorHandler',
        'FunctionHandler',
        'MicManagerWrapper',
        'PersonaHandler',
        'UserHandler',
    ]:
        setattr(package, name, Mock())
    manager = isolated.load('session_manager')
    audio_module.playback_done_event.set()

    async def run():
        provider = Mock()
        provider.get_provider_name.return_value = 'openai'
        provider.send_message = AsyncMock()
        session = manager.BillySession(conversation_provider=provider)
        session.loop = asyncio.get_running_loop()
        session.session_active.set()
        session.mic_manager.start = Mock()
        inbox = asyncio.Queue()

        async def receive():
            event = await inbox.get()
            if event is None:
                raise Closed()
            return json.dumps(event)

        async def close():
            await inbox.put(None)

        session.ws = SimpleNamespace(recv=receive, close=close)
        started = asyncio.Event()

        async def external(*args):
            started.set()
            await asyncio.Event().wait()

        session.function_handler.handle = external
        received_speech = asyncio.Event()
        session._on_input_speech_started = Mock(
            side_effect=lambda data: received_speech.set() or False
        )
        receiver = asyncio.create_task(session.run_stream())
        await inbox.put(dict(type='response.created', response=dict(id='r1')))
        await inbox.put(
            dict(
                type='response.function_call_arguments.done',
                response_id='r1',
                name='web_search',
                call_id='c1',
                arguments='{"query":"test"}',
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        await inbox.put(
            dict(type='response.done', response=dict(id='r1', status='completed'))
        )
        await inbox.put(dict(type='input_audio_buffer.speech_started', item_id='user1'))
        await asyncio.wait_for(received_speech.wait(), 1)
        await asyncio.sleep(0.35)  # real playback drain's acoustic settling delay
        assert session.session_active.is_set()
        assert session.state.allow_mic_input
        assert not session.state.assistant_speaking
        assert session.tool_calls.pending('r1')
        await session.request_stop()
        await asyncio.wait_for(receiver, 1)
        await asyncio.sleep(0)
        assert not session.tool_calls._tasks
        assert not any(
            call.args[1].get('type') == 'response.create'
            for call in provider.send_message.call_args_list
        )
        session.mic_manager.stop.assert_called_once()

    asyncio.run(run())
