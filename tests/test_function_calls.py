"""Real shared tool handlers through both Live and Realtime event contracts.

Only external services, hardware and configuration persistence are replaced.
Profile storage uses the real INI/transaction implementation in a temporary root.
"""

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_live import backend  # Share the offline Live/socket fixture.
from test_live import live as live


@pytest.fixture(params=['gpt-live', 'openai'])
def calls(request, live, monkeypatch, tmp_path):
    import billy_runtime.configuration as configuration
    import billy_runtime.profiles as locks

    isolated, s, engine = live.isolated, live.session, live.engine
    monkeypatch.setattr(configuration, 'ROOT', tmp_path)
    monkeypatch.setattr(locks.profile_lock, '__defaults__', (tmp_path,))
    persona = SimpleNamespace(
        current_persona='default',
        get_available_personas=lambda: [{'name': 'default'}, {'name': 'pirate'}],
        get_current_persona_voice=lambda: 'marin',
        get_current_persona_data=lambda: {'meta': {'description': 'Test persona'}},
    )

    def switch(name):
        persona.current_persona = name
        return True

    persona.switch_persona = Mock(side_effect=switch)
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(persona_manager=persona),
    )
    profiles = isolated.load('profile_manager')
    personality = SimpleNamespace(humor=50, _bucket=lambda value: 'med')
    isolated.config.PERSONALITY = personality
    services = {
        'ha': SimpleNamespace(
            send_conversation_prompt=AsyncMock(
                return_value={
                    'response_type': 'action_done',
                    'speech': {'plain': {'speech': 'Lights on'}},
                }
            )
        ),
        'knowledge_manager': SimpleNamespace(
            knowledge_manager=SimpleNamespace(
                search=Mock(
                    return_value={'ok': True, 'matches': [{'text': 'Test fact'}]}
                ),
                list_folders=lambda: [],
            )
        ),
        'mood': SimpleNamespace(
            MODEL_REPORTED_MOOD_EVENTS=('neutral',),
            mood_manager=SimpleNamespace(
                snapshot=lambda: {'label': 'neutral'},
                set_mood=Mock(return_value={'ok': True, 'mood': {'label': 'happy'}}),
                apply_event=Mock(),
            ),
        ),
        'news_digest': SimpleNamespace(
            get_news_digest=Mock(return_value={'ok': True, 'summary': 'Test news'})
        ),
        'web_search': SimpleNamespace(
            web_search=Mock(return_value={'ok': True, 'summary': 'Test search'})
        ),
        'vision': SimpleNamespace(
            describe_scene=Mock(
                return_value={
                    'ok': True,
                    'summary': 'Image captured',
                    'image_url': 'data:image/jpeg;base64,AAAA',
                }
            )
        ),
        'persona': SimpleNamespace(update_persona_ini=Mock()),
        'song_manager': SimpleNamespace(
            song_manager=SimpleNamespace(
                get_dynamic_tool_description=lambda: 'Play a song'
            )
        ),
        'session.tool_manager': SimpleNamespace(
            tool_manager=SimpleNamespace(is_available=lambda _: True)
        ),
    }
    for name, module in services.items():
        monkeypatch.setitem(sys.modules, f'isolated_core.{name}', module)
    definitions = isolated.load('base_tools')
    tools = definitions.get_base_tools() + definitions.get_user_tools()

    def instructions():
        user = profiles.user_manager.get_current_user()
        return (
            f'# Current User Context\n{user.name if user else "Guest"}\nPersona: {persona.current_persona}\n'
            + (user.get_context_string() if user else '')
        )

    monkeypatch.setitem(
        sys.modules,
        'isolated_core.session_manager',
        SimpleNamespace(
            get_instructions_with_user_context=instructions,
            get_tools_for_current_mode=lambda: tools,
        ),
    )
    s.mark_mood_instructions_current = Mock()
    s.tool_calls = SimpleNamespace(in_call=lambda: True)
    user_module = isolated.load('session.user_handler')
    s.user_handler = user_module.UserHandler(s)
    s.user_handler.save_current_user_to_env = AsyncMock()
    persona_module = isolated.load('session.persona_handler')
    s.persona_handler = persona_module.PersonaHandler(s)
    functions = isolated.load('session.function_handler')
    s.function_handler = functions.FunctionHandler(s)
    # Skip pacing delays without replacing the event loop's own sleep.
    fast_asyncio = SimpleNamespace(sleep=AsyncMock(), to_thread=asyncio.to_thread)
    monkeypatch.setattr(functions, 'asyncio', fast_asyncio)
    monkeypatch.setattr(user_module, 'asyncio', fast_asyncio)
    engine.tools = live.provider.live_tools(tools)
    engine.voice = 'marin'
    provider = request.param
    if provider == 'openai':
        s.realtime_ai_provider = SimpleNamespace(get_provider_name=lambda: 'openai')

        async def send(event):
            await s.ws.send(json.dumps(event))

        s._ws_send_json = send

    counter = 0

    async def invoke(name, args):
        nonlocal counter
        counter += 1
        call_id = f'call-{counter}'
        start = len(s.ws.sent)
        if provider == 'gpt-live':
            await engine.handle(
                backend('response.created', response={'id': f'r{counter}'})
            )
            await engine.handle(
                backend(
                    'response.output_item.done',
                    item={
                        'type': 'function_call',
                        'name': name,
                        'arguments': json.dumps(args),
                        'call_id': call_id,
                    },
                )
            )
            await engine.handle(
                backend('response.completed', response={'id': f'r{counter}'})
            )
            await asyncio.gather(*engine.tasks)
        else:
            await s.function_handler.handle(name, json.dumps(args), call_id)
        messages = s.ws.sent[start:]
        outputs = [
            m
            for m in messages
            if m.get('item', {}).get('type') == 'function_call_output'
        ]
        assert len(outputs) == 1, messages
        assert outputs[0]['item']['call_id'] == call_id
        assert outputs[0]['type'] == (
            'response.item.create'
            if provider == 'gpt-live'
            else 'conversation.item.create'
        )
        assert len([m for m in messages if m['type'] == 'response.create']) == 1, (
            messages
        )
        if provider == 'gpt-live':
            assert not engine.responses
            assert all(
                m['type'] not in {'conversation.item.create', 'persona_changed'}
                for m in messages
            )
            assert messages[-1] == {'type': 'response.create'}
        else:
            assert all(
                m['type']
                in {'conversation.item.create', 'session.update', 'response.create'}
                for m in messages
            )
        return json.loads(outputs[0]['item']['output']), messages

    return SimpleNamespace(
        invoke=invoke,
        profiles=profiles,
        users=profiles.user_manager,
        session=s,
        persona=persona,
        functions=functions,
        services=services,
        tools=tools,
        provider=provider,
        live=live,
    )


def profile_bytes(calls):
    return {p.name: p.read_bytes() for p in calls.users.profiles_dir.glob('*.ini')}


@pytest.mark.parametrize(
    'existing,spoken',
    [
        ('Henk', 'Hank'),
        ('Thom', 'Tom'),
        ('Tom', 'Thom'),
        ('Sarah', 'Sara'),
        ('Sophie', 'Sofie'),
        ('Steven', 'Stephen'),
        ('José', 'Jose'),
        ('Anne-Marie', 'Annemarie'),
    ],
)
def test_similar_names_require_clarification_without_side_effects(
    calls, existing, spoken
):
    async def run():
        original = calls.users.identify_user(existing)
        before = profile_bytes(calls)
        result, messages = await calls.invoke(
            'identify_user', {'name': spoken, 'confidence': 'high'}
        )
        assert result['status'] == 'clarification_required'
        assert result['candidates'][0]['name'] == existing
        assert calls.users.get_current_user() is original
        assert profile_bytes(calls) == before
        calls.session.user_handler.save_current_user_to_env.assert_not_awaited()
        calls.persona.switch_persona.assert_not_called()
        assert not any(m['type'] == 'session.update' for m in messages)

    asyncio.run(run())


@pytest.mark.parametrize('selection', ['Henk', 'Hank'])
def test_clarification_can_select_existing_or_confirm_distinct_new_user(
    calls, selection
):
    async def run():
        calls.users.identify_user('Henk')
        await calls.invoke('identify_user', {'name': 'Hank', 'confidence': 'high'})
        result, messages = await calls.invoke(
            'identify_user',
            {
                'name': selection,
                'confidence': 'high',
                'new_user_confirmed': selection == 'Hank',
                'context': 'not Henk, a different Hank'
                if selection == 'Hank'
                else 'Yes, Henk',
            },
        )
        assert result['status'] == 'success'
        assert calls.users.get_current_user().name == selection
        assert (calls.users.profiles_dir / 'hank.ini').exists() == (selection == 'Hank')
        calls.session.user_handler.save_current_user_to_env.assert_awaited_once_with(
            selection
        )
        assert_session_update(calls, messages, selection)

    asyncio.run(run())


def test_confirmation_flag_cannot_bypass_initial_question_or_apply_to_another_name(
    calls,
):
    async def run():
        calls.users.identify_user('Henk')
        calls.users.identify_user('Thom')
        before = profile_bytes(calls)
        for name, flag in [
            ('Hank', True),
            ('Hank', False),
            ('Hank', 'true'),
            ('Tom', True),
        ]:
            result, _ = await calls.invoke(
                'identify_user',
                {'name': name, 'confidence': 'high', 'new_user_confirmed': flag},
            )
            assert result['status'] == 'clarification_required'
            assert profile_bytes(calls) == before

    asyncio.run(run())


@pytest.mark.parametrize('name', ['thom', 'Tommy'])
def test_exact_name_or_display_name_still_selects_existing_profile(calls, name):
    async def run():
        profile = calls.users.identify_user('Thom')
        profile.set_display_name('Tommy')
        before = profile_bytes(calls)
        result, messages = await calls.invoke(
            'identify_user', {'name': name, 'confidence': 'high'}
        )
        assert result['status'] == 'success' and result['user'] == 'Thom'
        assert profile_bytes(calls) == before
        assert_session_update(calls, messages, 'Thom')

    asyncio.run(run())


def test_display_name_similarity_does_not_load_profile(calls):
    profile = calls.users.identify_user('Person42')
    profile.set_display_name('Thom')
    calls.users.clear_current_user()
    before = profile_bytes(calls)
    assert calls.users.find_similar_users('Tom') == [
        {'name': 'Person42', 'display_name': 'Thom'}
    ]
    assert calls.users.get_current_user() is None
    assert profile_bytes(calls) == before


@pytest.mark.parametrize(
    'confidence,status', [('high', 'success'), ('low', 'clarification_required')]
)
def test_unrelated_new_name_and_low_confidence(calls, confidence, status):
    async def run():
        calls.users.identify_user('Thom')
        result, _ = await calls.invoke(
            'identify_user', {'name': 'Zoe', 'confidence': confidence}
        )
        assert result['status'] == status
        assert (calls.users.profiles_dir / 'zoe.ini').exists() == (confidence == 'high')

    asyncio.run(run())


def assert_session_update(calls, messages, expected):
    updates = [m for m in messages if m['type'] == 'session.update']
    assert len(updates) == 1
    config = updates[0]['session']
    if calls.provider == 'gpt-live':
        assert set(config) == {'delegation'}
        assert config['delegation']['type'] == 'responses'
        assert expected in config['delegation']['responses']['instructions']
        assert 'identify_user' in {
            t['name'] for t in config['delegation']['responses']['tools']
        }
        appends = [m for m in messages if m['type'] == 'session.instructions.append']
        assert expected in ''.join(m['content'] for m in appends)
        assert all(m['delegation_id'] is None for m in appends)
    else:
        assert config['type'] == 'realtime'
        assert expected in config['instructions']
        assert config['audio']['output']['voice'] == 'marin'


def test_memory_persistence_updates_context_on_both_providers(calls):
    async def run():
        profile = calls.users.identify_user('Thom')
        result, messages = await calls.invoke(
            'store_memory', {'memory': 'Likes bicycles'}
        )
        assert result['status'] == 'success'
        assert 'Likes bicycles' in json.dumps(
            calls.profiles.UserProfile(profile.name).get_memories()
        )
        assert_session_update(calls, messages, 'Likes bicycles')

    asyncio.run(run())


@pytest.mark.parametrize(
    'tool,args,persist',
    [
        ('switch_persona', {'persona': 'pirate'}, False),
        (
            'manage_profile',
            {'action': 'switch_persona', 'preferred_persona': 'pirate'},
            True,
        ),
    ],
)
def test_persona_calls_report_success_and_preserve_realtime_updates(
    calls, tool, args, persist
):
    async def run():
        profile = calls.users.identify_user('Thom')
        result, messages = await calls.invoke(tool, args)
        assert result['ok'] is True and calls.persona.current_persona == 'pirate'
        saved = calls.profiles.UserProfile(profile.name).data['USER_INFO'][
            'preferred_persona'
        ]
        assert saved == ('pirate' if persist else 'default')
        assert_session_update(calls, messages, 'pirate')

    asyncio.run(run())


@pytest.mark.parametrize(
    'tool,args',
    [
        ('switch_persona', {'persona': 'missing'}),
        ('switch_persona', {}),
        (
            'manage_profile',
            {'action': 'switch_persona', 'preferred_persona': 'default'},
        ),
        ('manage_profile', {'action': 'unsupported'}),
    ],
)
def test_persona_failures_do_not_infer_success_from_current_persona(calls, tool, args):
    async def run():
        result, messages = await calls.invoke(tool, args)
        assert result['ok'] is False
        assert not any(m['type'] == 'session.update' for m in messages)

    asyncio.run(run())


def test_failed_preference_save_reports_failure_even_if_persona_already_matches(
    calls, monkeypatch
):
    async def run():
        profile = calls.users.identify_user('Thom')
        before = profile_bytes(calls)
        monkeypatch.setattr(
            profile, 'set_preferred_persona', Mock(side_effect=OSError('disk full'))
        )
        result, _ = await calls.invoke(
            'manage_profile',
            {'action': 'switch_persona', 'preferred_persona': 'default'},
        )
        assert result['ok'] is False
        assert profile_bytes(calls) == before
        calls.persona.switch_persona.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    'tool,args',
    [
        ('get_mood', {}),
        ('set_mood', {'mood': 'happy'}),
        ('web_search', {'query': 'test'}),
        ('get_news_digest', {'category': 'news'}),
        ('search_local_knowledge', {'query': 'test'}),
        ('describe_scene', {}),
        ('smart_home_command', {'prompt': 'turn on the test lamp'}),
        ('update_personality', {'humor': 'high'}),
    ],
)
def test_existing_tools_complete_once_with_matching_call_id(calls, tool, args):
    async def run():
        result, messages = await calls.invoke(tool, args)
        assert result.get('ok') is True or result.get('status') == 'success'
        if tool == 'describe_scene':
            images = [
                m
                for m in messages
                if any(
                    c['type'] == 'input_image'
                    for c in m.get('item', {}).get('content', [])
                )
            ]
            assert len(images) == 1
            assert images[0]['type'] == (
                'response.item.create'
                if calls.provider == 'gpt-live'
                else 'conversation.item.create'
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    'failure',
    ['ha_error', 'ha_unavailable', 'no_image', 'camera_error', 'search_error'],
)
def test_external_failures_are_not_reported_as_success(calls, failure):
    async def run():
        tool, args = 'describe_scene', {}
        if failure.startswith('ha_'):
            tool, args = 'smart_home_command', {'prompt': 'test'}
            calls.functions.send_conversation_prompt.return_value = (
                {'response_type': 'error'} if failure == 'ha_error' else None
            )
        elif failure == 'no_image':
            calls.functions.describe_scene.return_value = {
                'ok': True,
                'summary': 'Captured',
            }
        elif failure == 'camera_error':
            calls.functions.describe_scene.side_effect = OSError('camera unavailable')
        else:
            tool, args = 'web_search', {'query': 'test'}
            calls.functions.web_search.return_value = {
                'ok': False,
                'error': 'unavailable',
            }
        result, _ = await calls.invoke(tool, args)
        assert result.get('ok') is False or result.get('status') == 'error'

    asyncio.run(run())


def test_identity_tool_schema_requires_explicit_later_confirmation(calls):
    tool = next(t for t in calls.tools if t['name'] == 'identify_user')
    confirmation = tool['parameters']['properties']['new_user_confirmed']
    assert confirmation['type'] == 'boolean'
    assert 'later' in confirmation['description']
    assert 'new_user_confirmed' not in tool['parameters']['required']


def test_denial_completes_tool_call_without_selecting_profile(calls):
    async def run():
        original = calls.users.identify_user('Thom')
        before = profile_bytes(calls)
        result, _ = await calls.invoke(
            'identify_user',
            {
                'name': 'Thom',
                'confidence': 'high',
                'context': 'I am not Thom',
            },
        )
        assert result['status'] == 'clarification_required'
        assert calls.users.get_current_user() is original
        assert profile_bytes(calls) == before

    asyncio.run(run())


def test_play_song_still_ends_voice_session_before_audio_playback(calls, monkeypatch):
    async def run():
        order = []
        s, engine = calls.session, calls.live.engine

        async def stop():
            order.append('stop')
            s.session_active.clear()

        async def play(name, **kwargs):
            assert not s.session_active.is_set()
            order.append(name)

        s.stop_session = stop
        monkeypatch.setattr(calls.live.audio, 'play_song', play)
        if calls.provider == 'gpt-live':
            await engine.handle(backend('response.created', response={'id': 'song'}))
            await engine.handle(
                backend(
                    'response.output_item.done',
                    item={
                        'type': 'function_call',
                        'name': 'play_song',
                        'arguments': '{"song":"test-song"}',
                        'call_id': 'song-call',
                    },
                )
            )
            await engine.handle(backend('response.completed', response={'id': 'song'}))
            await asyncio.gather(*engine.tasks)
        else:
            await s.function_handler.handle(
                'play_song', '{"song":"test-song"}', 'song-call'
            )
        assert order == ['stop', 'test-song']
        assert not s.ws.sent  # No attempt to continue a session after stopping it.

    asyncio.run(run())


@pytest.mark.parametrize(
    'names', [('Henk', 'Hank'), ('Thom', 'Tom'), ('Sarah', 'Sara')]
)
def test_exact_transcription_also_asks_when_similar_profiles_already_exist(
    calls, names
):
    async def run():
        for name in names:
            calls.users.identify_user(name)
        calls.users.clear_current_user()
        before = profile_bytes(calls)
        result, messages = await calls.invoke(
            'identify_user',
            {
                'name': names[1],
                'confidence': 'high',
                'context': f"It's me {names[1]}",
            },
        )
        assert result['status'] == 'clarification_required'
        assert {candidate['name'] for candidate in result['candidates']} == set(names)
        assert calls.users.get_current_user() is None
        assert profile_bytes(calls) == before
        assert not any(m['type'] == 'session.update' for m in messages)
        calls.session.user_handler.save_current_user_to_env.assert_not_awaited()
        calls.persona.switch_persona.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize('chosen', ['Henk', 'Hank'])
def test_existing_ambiguous_profile_can_be_selected_after_explicit_confirmation(
    calls, chosen
):
    async def run():
        calls.users.identify_user('Henk')
        calls.users.identify_user('Hank')
        calls.users.clear_current_user()
        before = profile_bytes(calls)
        result, _ = await calls.invoke(
            'identify_user', {'name': 'Hank', 'confidence': 'high'}
        )
        assert result['status'] == 'clarification_required'
        result, messages = await calls.invoke(
            'identify_user',
            {
                'name': chosen,
                'confidence': 'high',
                'existing_user_confirmed': True,
            },
        )
        assert result['status'] == 'success' and result['user'] == chosen
        assert calls.users.get_current_user().name == chosen
        assert profile_bytes(calls) == before
        calls.session.user_handler.save_current_user_to_env.assert_awaited_once_with(
            chosen
        )
        assert_session_update(calls, messages, chosen)

    asyncio.run(run())


def test_existing_name_confirmation_requires_pending_choice_and_boolean_flag(calls):
    async def run():
        for name in ('Henk', 'Hank', 'Thom', 'Tom'):
            calls.users.identify_user(name)
        calls.users.clear_current_user()
        before = profile_bytes(calls)
        for name, flag in [
            ('Hank', True),
            ('Hank', False),
            ('Hank', 'true'),
            ('Tom', True),
        ]:
            result, _ = await calls.invoke(
                'identify_user',
                {
                    'name': name,
                    'confidence': 'high',
                    'existing_user_confirmed': flag,
                },
            )
            assert result['status'] == 'clarification_required'
            assert calls.users.get_current_user() is None
            assert profile_bytes(calls) == before

    asyncio.run(run())


def test_similar_display_names_also_require_an_unambiguous_profile_choice(calls):
    async def run():
        calls.users.identify_user('Person42').set_display_name('Henk')
        calls.users.identify_user('Person84').set_display_name('Hank')
        calls.users.clear_current_user()
        result, _ = await calls.invoke(
            'identify_user', {'name': 'Hank', 'confidence': 'high'}
        )
        assert result['status'] == 'clarification_required'
        assert {c['name'] for c in result['candidates']} == {'Person42', 'Person84'}
        assert calls.users.get_current_user() is None
        result, _ = await calls.invoke(
            'identify_user',
            {
                'name': 'Person42',
                'confidence': 'high',
                'existing_user_confirmed': True,
            },
        )
        assert result['user'] == 'Person42'

    asyncio.run(run())
