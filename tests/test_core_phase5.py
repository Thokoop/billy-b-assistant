"""Errors, deadlines, tool identity and live receive-loop regression tests."""

import asyncio
import base64
import contextlib
import json
import time
from collections import deque
from functools import wraps
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import definitions


def collector(timeout=0.05, limit=1024):
    return definitions(
        'core/realtime_ai_provider.py',
        ['_collect_audio_response'],
        dict(
            asyncio=asyncio,
            json=json,
            base64=base64,
            CLIP_TIMEOUT_SECONDS=timeout,
            MAX_CLIP_BYTES=limit,
        ),
        cls='RealtimeAIProvider',
    )._collect_audio_response


async def messages(events, *, hang=False):
    for event in events:
        yield json.dumps(event)
    if hang:
        await asyncio.Event().wait()


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'incomplete'])
def test_unsuccessful_clip_never_returns_partial_audio(status):
    events = [
        dict(type='response.output_audio.delta', delta='AQABAA=='),
        dict(type='response.done', response=dict(status=status)),
    ]

    async def run():
        with pytest.raises(RuntimeError):
            await collector()(None, messages(events))

    asyncio.run(run())


@pytest.mark.parametrize(
    'events',
    [
        [],
        [dict(type='error', error=dict(message='bad session'))],
        [dict(type='response.done', response=dict(status='completed'))],
    ],
)
def test_clip_eof_empty_and_api_errors_are_explicit(events):
    async def run():
        with pytest.raises(RuntimeError):
            await collector()(None, messages(events))

    asyncio.run(run())


def test_clip_timeout_and_cancellation():
    async def run():
        with pytest.raises(TimeoutError):
            await collector()(None, messages([], hang=True))
        task = asyncio.create_task(collector()(None, messages([], hang=True)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())


@pytest.mark.parametrize(
    'event_type',
    ['response.output_audio', 'response.output_audio.delta', 'response.audio.delta'],
)
def test_completed_clip_keeps_exact_pcm(event_type):
    pcm = b'\x01\x00\xfe\xff' * 10

    async def run():
        events = [
            dict(type=event_type, delta=base64.b64encode(pcm).decode()),
            dict(type='response.done', response={'status': 'completed'}),
        ]
        assert await collector()(None, messages(events)) == pcm
        with pytest.raises(RuntimeError, match='size limit'):
            await collector(limit=1)(None, messages(events))

    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'api'])
def test_provider_closes_clip_socket_on_every_exit(failure):
    ns = dict(asyncio=asyncio, wraps=wraps, CLIP_TIMEOUT_SECONDS=0.03)
    decorator = definitions(
        'core/realtime_ai_provider.py', ['bounded_audio_generation'], ns
    ).bounded_audio_generation
    generate = definitions(
        'core/providers/openai_provider.py',
        ['generate_audio_clip'],
        dict(json=json, bounded_audio_generation=decorator),
        cls='OpenAIProvider',
    ).generate_audio_clip

    class Socket:
        send = AsyncMock()
        closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            self.closed = True

    async def run():
        ws = Socket()

        async def collect(_):
            if failure == 'api':
                raise RuntimeError('API error')
            await asyncio.Event().wait()

        p = SimpleNamespace(
            default_voice='ballad',
            _connect_websocket=AsyncMock(return_value=ws),
            _collect_audio_response=collect,
        )
        task = asyncio.create_task(generate(p, 'hello'))
        if failure == 'cancel':
            await asyncio.sleep(0)
            task.cancel()
        error = {
            'cancel': asyncio.CancelledError,
            'timeout': TimeoutError,
            'api': RuntimeError,
        }[failure]
        with pytest.raises(error):
            await task
        assert ws.closed

    asyncio.run(run())


def tool_session(module):
    active = asyncio.Event()
    active.set()
    s = SimpleNamespace(
        session_active=active,
        function_handler=Mock(),
        _report_tool_failure=AsyncMock(),
        _tool_args_buffer={},
        _runaway_tool_args=False,
        _current_response_id='r1',
        _interrupted_response_ids=deque(maxlen=64),
        _response_done_event=asyncio.Event(),
        ws_lock=asyncio.Lock(),
        ws=object(),
        realtime_ai_provider=Mock(),
        refresh_mood_instructions=AsyncMock(),
    )
    s.realtime_ai_provider.send_message = AsyncMock()
    s.loop = asyncio.get_running_loop()
    s._on_tool_calls_finished = Mock()
    s.audio_handler = SimpleNamespace(
        wait_for_playback_complete=AsyncMock(return_value=True),
        signal_playback_done=Mock(),
    )
    s.tool_calls = module.ToolCalls(s, timeout=1.0)
    ns = dict(
        asyncio=asyncio,
        json=json,
        contextlib=contextlib,
        time=time,
        logger=Mock(),
        MAX_TOOL_ARGUMENT_CHARS=4000,
    )
    for name in [
        '_on_tool_args_done',
        '_on_tool_args_delta',
        '_ws_send_json',
        '_cancel_runaway_response',
    ]:
        method = getattr(
            definitions('core/session_manager.py', [name], ns, cls='BillySession'), name
        )
        setattr(s, name, MethodType(method, s))
    return s


async def settle(runner):
    tasks = [task for _, task in runner._tasks.values()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


def test_slow_tool_does_not_block_receiver_and_stop_cancels_it(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        started = asyncio.Event()

        async def slow(*args):
            started.set()
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        await asyncio.wait_for(
            s._on_tool_args_done(
                dict(name='web_search', arguments='{}', call_id='c1', response_id='r1')
            ),
            0.01,
        )
        await started.wait()
        # Real dispatch remains free to process speech events during execution.
        s._on_input_speech_started = Mock(return_value=False)
        handle = definitions(
            'core/session_manager.py',
            ['handle_message'],
            dict(asyncio=asyncio),
            cls='BillySession',
        ).handle_message
        await asyncio.wait_for(
            handle(s, {'type': 'input_audio_buffer.speech_started'}), 0.01
        )
        s._on_input_speech_started.assert_called_once()
        s.session_active.clear()
        s.tool_calls.cancel()
        await settle(s.tool_calls)
        s._report_tool_failure.assert_not_awaited()

    asyncio.run(run())


def test_tool_deadline_reports_original_call_id(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)

        async def slow(*args):
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        s.tool_calls.timeout = 0.01
        s.tool_calls.start('web_search', '{}', 'call-17', 'r1')
        await settle(s.tool_calls)
        args = s._report_tool_failure.call_args.args
        assert args[:2] == ('call-17', 'web_search') and isinstance(
            args[2], TimeoutError
        )

    asyncio.run(run())


def test_stale_tool_cannot_send_even_if_handler_suppresses_cancellation(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        started = asyncio.Event()

        async def resistant(*args):
            started.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
            await s._ws_send_json({'type': 'conversation.item.create'})

        s.function_handler.handle = resistant
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        await started.wait()
        s.tool_calls.cancel()
        await settle(s.tool_calls)
        s.realtime_ai_provider.send_message.assert_not_awaited()

    asyncio.run(run())


def test_guard_rechecks_after_waiting_for_send_lock(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        entered = asyncio.Event()
        await s.ws_lock.acquire()

        async def handler(*args):
            entered.set()
            await s._ws_send_json({'type': 'conversation.item.create'})

        s.function_handler.handle = handler
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        await entered.wait()
        s.session_active.clear()
        s.ws_lock.release()
        await settle(s.tool_calls)
        s.realtime_ai_provider.send_message.assert_not_awaited()

    asyncio.run(run())


def test_tool_continuation_waits_for_response_done(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        entered = asyncio.Event()

        async def handler(*args):
            entered.set()
            await s._ws_send_json({'type': 'response.create'})

        s.function_handler.handle = handler
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        await entered.wait()
        await asyncio.sleep(0)
        s.realtime_ai_provider.send_message.assert_not_awaited()
        s._response_done_event.set()
        await settle(s.tool_calls)
        s.realtime_ai_provider.send_message.assert_awaited_once()

    asyncio.run(run())


def test_two_same_named_calls_keep_separate_arguments_and_deduplicate(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        s.function_handler.handle = AsyncMock()
        for item, fragment in [('item1', '{"q":1}'), ('item2', '{"q":2}')]:
            s._on_tool_args_delta(dict(item_id=item, arguments=fragment))
        for n in [1, 2, 1]:
            await s._on_tool_args_done(
                dict(
                    item_id=f'item{n}',
                    name='web_search',
                    call_id=f'c{n}',
                    response_id='r1',
                )
            )
        await settle(s.tool_calls)
        assert [c.args for c in s.function_handler.handle.call_args_list] == [
            ('web_search', '{"q":1}', 'c1'),
            ('web_search', '{"q":2}', 'c2'),
        ]
        assert not s._tool_args_buffer

    asyncio.run(run())


def test_pending_tool_does_not_end_session_on_response_done(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        s.state = SimpleNamespace(response_active=True, _skip_post_response_once=False)
        s._server_barge_in_in_progress = False
        s._response_spoke_an_answer = Mock(return_value=False)
        s._response_finish_task = None

        async def slow(*a):
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        done = definitions(
            'core/session_manager.py',
            ['_on_response_done'],
            dict(logger=Mock()),
            cls='BillySession',
        )._on_response_done
        await done(s, dict(response=dict(id='r1', status='completed')))
        assert s._response_done_event.is_set() and s._response_finish_task is None
        s.tool_calls.cancel()
        await settle(s.tool_calls)

    asyncio.run(run())


def test_pending_tool_reopens_input_after_leadin_without_finishing_session(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        s.state = SimpleNamespace(assistant_speaking=True, allow_mic_input=False)
        playback = asyncio.Event()

        async def drain():
            await playback.wait()
            return True

        s.audio_handler.wait_for_playback_complete = drain

        async def slow(*a):
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        s.tool_calls.release_input_after_playback('r1')
        await asyncio.sleep(0)
        assert not s.state.allow_mic_input
        playback.set()
        await s.tool_calls._input_wait
        assert s.state.allow_mic_input and not s.state.assistant_speaking
        s.audio_handler.signal_playback_done.assert_called_once()
        assert s.tool_calls.pending('r1')
        s.tool_calls.cancel()
        await settle(s.tool_calls)

    asyncio.run(run())


def test_old_leadin_wait_never_changes_new_response_state(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        s.state = SimpleNamespace(assistant_speaking=True, allow_mic_input=False)
        playback = asyncio.Event()

        async def drain():
            await playback.wait()
            return True

        s.audio_handler.wait_for_playback_complete = drain

        async def slow(*a):
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        s.tool_calls.release_input_after_playback('r1')
        s._current_response_id = 'new-response'
        playback.set()
        await s.tool_calls._input_wait
        assert s.state.assistant_speaking and not s.state.allow_mic_input
        s.tool_calls.cancel()
        await settle(s.tool_calls)

    asyncio.run(run())


def test_tool_without_continuation_finishes_its_original_response():
    async def run():
        s = SimpleNamespace(
            session_active=asyncio.Event(),
            _current_response_id='r1',
            state=SimpleNamespace(response_active=False, _triggered_new_response=False),
            _finish_response_after_playback=AsyncMock(),
        )
        s.session_active.set()
        finished = definitions(
            'core/session_manager.py',
            ['_on_tool_calls_finished'],
            dict(asyncio=asyncio),
            cls='BillySession',
        )._on_tool_calls_finished
        finished(s, 'r1')
        await s._response_finish_task
        s._finish_response_after_playback.assert_awaited_once_with('r1')

    asyncio.run(run())


def test_unvalidated_audio_commit_does_not_cancel_pending_tool(isolated):
    module = isolated.load('tool_calls')

    async def run():
        s = tool_session(module)
        s.state = Mock()
        s._tool_answer_awaiting_speech = True
        s.TRANSCRIPT_DONE_TYPES = set()
        s.AUDIO_OUT_TYPES = set()

        async def slow(*a):
            await asyncio.Event().wait()

        s.function_handler.handle = slow
        s.tool_calls.start('search', '{}', 'c1', 'r1')
        handle = definitions(
            'core/session_manager.py', ['handle_message'], dict(), cls='BillySession'
        ).handle_message
        await handle(s, {'type': 'input_audio_buffer.committed'})
        assert s.tool_calls.pending('r1') and s._tool_answer_awaiting_speech
        s.tool_calls.cancel()
        await settle(s.tool_calls)

    asyncio.run(run())
