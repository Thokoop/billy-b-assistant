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


# OpenAI Live API reference, checked 2026-11-12. Keep this independent of the
# implementation so accidentally dropping legacy voices breaks the regression.
EXPECTED_LIVE = {
    'alloy',
    'ash',
    'ballad',
    'beacon',
    'bossa',
    'brise',
    'calandria',
    'cedar',
    'cinder',
    'compas',
    'coral',
    'delta',
    'destello',
    'echo',
    'flitz',
    'gleam',
    'halo',
    'harema',
    'hornero',
    'juni',
    'luciernaga',
    'marin',
    'meridian',
    'nira',
    'noeul',
    'nuri',
    'pearl',
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


def _voice_for(provider, meta):
    logger = Mock()
    functions = definitions(
        'application/core/persona_manager.py',
        ['get_persona_voice'],
        {
            'voice_provider_registry': SimpleNamespace(get_provider=lambda: provider),
            'logger': logger,
        },
        cls='PersonaManager',
    )
    persona = SimpleNamespace(load_persona=lambda _: {'meta': meta})
    return functions.get_persona_voice(persona, 'demo'), logger


def test_live_voice_falls_back_to_the_personas_realtime_voice(providers):
    # A GPT-Live voice on Realtime: the persona's own fallback voice.
    meta = {'voice': 'nira', 'voice_fallback': 'Coral '}
    assert _voice_for(providers.realtime, meta)[0] == 'coral'
    # On GPT-Live the Live voice itself.
    assert _voice_for(providers.live, meta)[0] == 'nira'
    # No (usable) fallback: the provider default, with a warning.
    for fallback in ('', 'nira', 'not-a-voice'):
        voice, logger = _voice_for(
            providers.realtime, {'voice': 'nira', 'voice_fallback': fallback}
        )
        assert voice == providers.realtime.default_voice
        logger.warning.assert_called_once()
    # xAI knows neither: its default.
    assert _voice_for(providers.xai, meta)[0] == providers.xai.default_voice


def test_custom_voice_id_keeps_its_case():
    provider = SimpleNamespace(
        default_voice='marin', get_supported_voices=lambda: ['marin', 'voice_Ab12']
    )
    assert _voice_for(provider, {'voice': 'voice_Ab12'})[0] == 'voice_Ab12'
    assert _voice_for(provider, {'voice': 'Marin'})[0] == 'marin'


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
    config = SimpleNamespace(
        REALTIME_AI_PROVIDER=provider,
        DEFAULT_USER='guest',
        OPENAI_LIVE_CUSTOM_VOICES='Ada|voice_ada1|Irish English',
    )
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
        [
            '_voice_options',
            '_custom_voices',
            '_voice_labels',
            '_config_value_for_ui',
            '_page_context',
            'get_config',
        ],
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
    expected = list(selected.get_supported_voices())
    if provider == 'gpt-live' and 'voice_ada1' not in expected:
        expected.append('voice_ada1')  # the custom voice, as saved now
    assert initial['VOICE_OPTIONS'] == fetched['VOICE_OPTIONS'] == expected
    for voice in expected:
        assert f'<option value="{voice}">' in html
    assert 'GPT-Live clip generation is experimental.' not in html
    # Custom GPT-Live voices: in the persona voice list with their name.
    assert (
        initial['VOICE_LABELS']
        == fetched['VOICE_LABELS']
        == {'voice_ada1': 'Ada (custom)'}
    )
    assert ('voice_ada1' in initial['VOICE_OPTIONS']) == (provider == 'gpt-live')
    assert ('voice_ada1' in fetched['VOICE_OPTIONS']) == (provider == 'gpt-live')
    # The fallback voice offers the Realtime voices whatever the provider.
    assert initial['VOICE_FALLBACK_OPTIONS'] == fetched['VOICE_FALLBACK_OPTIONS']
    assert set(initial['VOICE_FALLBACK_OPTIONS']) == EXPECTED_REALTIME
    assert 'id="VOICE_FALLBACK"' in html and 'id="voice-fallback-row" hidden' in html
    if provider == 'gpt-live':
        assert set(expected) == EXPECTED_LIVE | {'voice_ada1'}
    else:
        assert 'vesper' not in expected
    registry.get_provider.assert_called_with(provider)


def test_saved_persona_keeps_its_fallback_voice(tmp_path, monkeypatch):
    import configparser

    monkeypatch.chdir(tmp_path)
    (tmp_path / 'personas').mkdir()
    monkeypatch.setitem(
        sys.modules,
        'core.persona_manager',
        SimpleNamespace(persona_manager=SimpleNamespace(clear_persona_cache=Mock())),
    )
    functions = definitions(
        'application/webconfig/app/routes/persona.py',
        ['save_persona', '_normalize_wakeup_moods'],
        {
            'bp': Blueprint('persona_save', __name__),
            'configparser': configparser,
            'jsonify': jsonify,
            'logger': Mock(),
            'PERSONA_PATH': tmp_path / 'persona.ini',
            'request': SimpleNamespace(
                json={
                    'persona_name': 'demo',
                    'META': {
                        'name': 'Demo',
                        'voice': 'nira',
                        'voice_fallback': 'coral',
                    },
                }
            ),
        },
    )
    app = Flask(__name__)
    with app.app_context():
        functions.save_persona()
    saved = configparser.ConfigParser()
    saved.read(tmp_path / 'personas' / 'demo' / 'persona.ini')
    assert saved['META']['voice'] == 'nira'
    assert saved['META']['voice_fallback'] == 'coral'


def test_home_assistant_provider_keeps_realtime_voices_for_personas(
    providers, monkeypatch
):
    # Home Assistant picks its own voice, so personas keep editing the voice
    # they use again once an AI provider is selected; no warning is logged.
    registry = Mock()
    registry.get_provider.side_effect = ValueError('not registered')
    logger = Mock()
    functions = definitions(
        'application/webconfig/app/routes/system.py',
        ['_voice_options', '_custom_voices'],
        {
            '__package__': 'webconfig.app.routes',
            'voice_provider_registry': registry,
            'core_config': SimpleNamespace(OPENAI_LIVE_CUSTOM_VOICES=''),
            'logger': logger,
            'os': os,
        },
    )
    assert set(functions._voice_options('home-assistant')) == EXPECTED_REALTIME
    logger.warning.assert_not_called()
