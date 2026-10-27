"""Keep persona selection, web options and Live speech on the same voice catalog."""

import asyncio
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import definitions
from flask import Blueprint, Flask, jsonify, render_template


# OpenAI Live API reference, checked 2026-10-24. Keep this independent of the
# implementation so accidentally dropping legacy voices breaks the regression.
EXPECTED_LIVE = {
    'alloy',
    'ash',
    'ballad',
    'beacon',
    'bossa',
    'brise',
    'cedar',
    'cinder',
    'coral',
    'delta',
    'echo',
    'flitz',
    'gleam',
    'harema',
    'juni',
    'marin',
    'meridian',
    'nira',
    'noeul',
    'nuri',
    'quartz',
    'ripple',
    'sage',
    'shida',
    'shimmer',
    'sillage',
    'stone',
    'tempo',
    'verse',
    'vesper',
    'willow',
}
EXPECTED_REALTIME = {
    'alloy',
    'ash',
    'ballad',
    'coral',
    'echo',
    'sage',
    'shimmer',
    'verse',
    'marin',
    'cedar',
}


@pytest.fixture
def providers(isolated, monkeypatch):
    modules = {}
    for name in ('live', 'openai', 'xai'):
        module = importlib.import_module(f'isolated_core.providers.{name}_provider')
        monkeypatch.setitem(sys.modules, f'core.providers.{name}_provider', module)
        modules[name] = module
    return SimpleNamespace(
        live=modules['live'].LiveProvider('test'),
        realtime=modules['openai'].OpenAIProvider('test'),
        xai=modules['xai'].XAIProvider('test'),
        live_module=modules['live'],
    )


def test_complete_catalogs_and_defaults(providers):
    live = providers.live.get_supported_voices()
    assert set(live) == EXPECTED_LIVE
    assert len(live) == len(set(live))
    assert live[0] == providers.live.default_voice == 'marin'
    assert set(providers.realtime.get_supported_voices()) == EXPECTED_REALTIME
    assert providers.realtime.default_voice == 'ballad'


@pytest.mark.parametrize('voice', sorted(EXPECTED_LIVE))
def test_persona_voice_reaches_live_unchanged(providers, voice):
    logger = Mock()
    functions = definitions(
        'application/core/persona_manager.py',
        ['get_persona_voice'],
        {
            'voice_provider_registry': SimpleNamespace(
                get_provider=lambda: providers.live
            ),
            'logger': logger,
        },
        cls='PersonaManager',
    )
    persona = SimpleNamespace(
        load_persona=lambda _: {'meta': {'voice': ' ' + voice.upper() + ' '}}
    )
    selected = functions.get_persona_voice(persona, 'billy-berserk')
    assert selected == voice
    logger.warning.assert_not_called()
    session = providers.live.session_config('Speak like Billy.', [], voice=selected)
    assert session['audio']['output']['voice'] == voice


def test_invalid_or_unset_live_voice_still_uses_marin(providers):
    for voice in (None, '', 'not-a-voice'):
        session = providers.live.session_config('Billy', [], voice=voice)
        assert session['audio']['output']['voice'] == 'marin'


@pytest.mark.parametrize('voice', sorted(EXPECTED_LIVE))
def test_announcements_preserve_every_live_voice(providers, monkeypatch, voice):
    clips = importlib.import_module('isolated_core.providers.live_clips')
    recorder = AsyncMock(return_value=b'pcm')
    monkeypatch.setattr(clips, 'record_audio_clip', recorder)
    result = asyncio.run(providers.live.generate_audio_clip('Hello', voice=voice))
    assert result == b'pcm'
    recorder.assert_awaited_once_with(
        providers.live, 'Hello', voice=voice, instructions=None
    )


@pytest.mark.parametrize('registered', [True, False])
@pytest.mark.parametrize('provider', ['gpt-live', 'openai', 'xai'])
def test_initial_page_and_config_agree_even_before_provider_restart(
    providers, monkeypatch, provider, registered
):
    selected = {
        'gpt-live': providers.live,
        'openai': providers.realtime,
        'xai': providers.xai,
    }[provider]
    registry = Mock()
    registry.get_provider.side_effect = lambda name: (
        selected if name == provider else providers.realtime
    )
    if not registered:
        registry.get_provider.side_effect = ValueError('Not registered yet')
    config = SimpleNamespace(REALTIME_AI_PROVIDER=provider, DEFAULT_USER='guest')
    monkeypatch.setenv('CURRENT_USER', 'guest')
    monkeypatch.setitem(sys.modules, 'core.config', config)
    monkeypatch.setitem(
        sys.modules,
        'core.profile_manager',
        SimpleNamespace(
            user_manager=SimpleNamespace(list_all_users=lambda: []), UserProfile=Mock()
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        'core.persona_manager',
        SimpleNamespace(
            persona_manager=SimpleNamespace(
                get_available_personas=lambda: [], current_persona='default'
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules, 'webconfig.app.core_imports', SimpleNamespace(core_config=config)
    )
    namespace = {
        '__package__': 'webconfig.app.routes',
        'voice_provider_registry': registry,
        'core_config': config,
        'CONFIG_KEYS': ['REALTIME_AI_PROVIDER'],
        'WIFI_COUNTRIES': [],
        '_is_wifi_onboarding_active': lambda: False,
        '_reload_core_config_if_env_changed': lambda: None,
        'logger': Mock(),
        'jsonify': jsonify,
        'os': os,
        'bp': Blueprint('voices', __name__),
    }
    functions = definitions(
        'application/webconfig/app/routes/system.py',
        ['_voice_options', '_config_value_for_ui', '_page_context', 'get_config'],
        namespace,
    )
    app = Flask(
        __name__,
        template_folder=str(
            Path(__file__).parents[1] / 'application/webconfig/templates'
        ),
    )
    with app.test_request_context('/config'):
        initial = functions._page_context('personas')['config']
        fetched = functions.get_config().get_json()
        html = render_template('components/persona-form.html', config=initial)
    expected = selected.get_supported_voices()
    assert initial['VOICE_OPTIONS'] == fetched['VOICE_OPTIONS'] == expected
    for voice in expected:
        assert f'<option value="{voice}">' in html
    assert 'GPT-Live clip generation is experimental.' not in html
    if provider == 'gpt-live':
        assert set(expected) == EXPECTED_LIVE
    else:
        assert 'vesper' not in expected
    registry.get_provider.assert_called_with(provider)
