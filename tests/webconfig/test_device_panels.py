"""Settings an extension device adds to the Device page (app/device_panels.py)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions
from flask import Blueprint, Flask, abort, jsonify, redirect, render_template


ROOT = Path(__file__).parents[2]


def load_device_panels(monkeypatch, panel):
    """app/device_panels.py with core and the system routes replaced."""
    package = ModuleType('panels_app')
    package.__path__ = [str(ROOT / 'application/webconfig/app')]
    system = SimpleNamespace(DEVICE_CONFIG_KEYS=set())
    routes = ModuleType('panels_app.routes')
    routes.system = system
    monkeypatch.setitem(sys.modules, 'panels_app', package)
    monkeypatch.setitem(sys.modules, 'panels_app.routes', routes)
    monkeypatch.setitem(sys.modules, 'panels_app.routes.system', system)
    monkeypatch.setitem(
        sys.modules, 'core.config', SimpleNamespace(BILLY_DEVICE='x:Device')
    )
    monkeypatch.setitem(
        sys.modules, 'core.devices', SimpleNamespace(webconfig_panel=lambda name: panel)
    )
    monkeypatch.setitem(sys.modules, 'core.logger', SimpleNamespace(logger=Mock()))
    spec = importlib.util.spec_from_file_location(
        'panels_app.device_panels', ROOT / 'application/webconfig/app/device_panels.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, system


@pytest.fixture
def app_with_panel(monkeypatch, tmp_path):
    (tmp_path / 'demo_panel.html').write_text(
        '<input name="DEMO_DELAY" value="{{ panel_context.delay }}">'
        '<script data-device-panel>window.demo = 1;</script>'
    )
    extra = Blueprint('demo_extra', __name__)
    extra.add_url_rule('/device/demo/ping', 'ping', lambda: jsonify(ok=True))
    panel = {
        'id': 'Demo Phone',
        'title': 'Demo',
        'icon': 'call',
        'template': 'demo_panel.html',
        'template_dir': str(tmp_path),
        'config_keys': ('DEMO_DELAY',),
        'context': lambda: {'delay': '7'},
        'blueprint': extra,
        'hide': ['motors', 'led', 'audio-devices', 'wakeword', 'mic-gain'],
        'audio_test': {
            'open': lambda timeout: (True, ''),
            'close': lambda: None,
            'hint': 'Lift the handset.',
            'mic_label': 'Handset',
        },
        'fixed_settings': {
            'MIC_PREFERENCE': 'demo_mic',
            'SPEAKER_PREFERENCE': 'demo_out',
        },
    }
    module, system = load_device_panels(monkeypatch, panel)
    app = Flask(
        'device-panels',
        template_folder=str(ROOT / 'application/webconfig/templates'),
        static_folder=str(ROOT / 'application/webconfig/static'),
    )
    bp = Blueprint('settings_routes', __name__)
    page_context = Mock(
        return_value={'current_page': 'settings', 'config': {}, 'bluetooth_token': 't'}
    )
    definitions(
        'application/webconfig/app/routes/system.py',
        ['settings_page'],
        {
            'bp': bp,
            'abort': abort,
            'redirect': redirect,
            'render_template': render_template,
            '_page_context': page_context,
        },
    )
    app.register_blueprint(bp)
    module.install(app)
    return app, system


def test_panel_is_part_of_the_device_page(app_with_panel):
    app, system = app_with_panel
    client = app.test_client()

    html = client.get('/settings/hardware').text

    section = html[html.index('id="section-hardware"') :]
    section = section[: section.index('id="section-audio"')]
    assert '<span>Device</span>' in section
    assert 'id="device-panel-demo-phone"' in section
    assert 'name="DEMO_DELAY" value="7"' in section
    assert '<script data-device-panel>' in section
    assert 'href="/settings/demo-phone"' not in html
    saved_keys = system.DEVICE_CONFIG_KEYS
    assert saved_keys == {'DEMO_DELAY'}
    assert client.get('/device/demo/ping').json == {'ok': True}


def test_device_hides_hardware_it_lacks_and_fixes_audio(app_with_panel):
    app, _ = app_with_panel
    html = app.test_client().get('/settings/hardware').text

    def tag(feature):
        start = html.index(f'data-hardware-feature="{feature}"')
        return html[start : html.index('>', start)]

    assert 'hidden' in tag('motors')
    assert 'hidden' in tag('led')
    assert 'id="MIC_PREFERENCE"' not in html
    assert 'Microphone Device' not in html
    assert '<input type="hidden" name="MIC_PREFERENCE" value="demo_mic">' in html


def test_fixed_settings_win_on_save(monkeypatch):
    module, _ = load_device_panels(
        monkeypatch,
        {
            'id': 'x',
            'template': 't.html',
            'fixed_settings': {'MIC_PREFERENCE': 'demo_mic'},
        },
    )
    app = Flask('fixed')
    module.install(app)
    assert app.config['DEVICE_FIXED_SETTINGS'] == {'MIC_PREFERENCE': 'demo_mic'}


def test_old_panel_address_opens_the_device_page(app_with_panel):
    app, _ = app_with_panel
    response = app.test_client().get('/settings/demo-phone')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/settings/hardware')


def test_fish_page_shows_all_hardware(monkeypatch):
    module, _ = load_device_panels(monkeypatch, None)
    app = Flask('fish', template_folder=str(ROOT / 'application/webconfig/templates'))
    module.install(app)
    with app.app_context(), app.test_request_context():
        from flask import render_template_string

        html = render_template_string(
            '{% for f in ["motors", "led"] %}{{ f in device_hidden }}{% endfor %}'
            '{{ device_fixed_settings }}'
        )
    assert html == 'FalseFalse{}'


def test_unknown_category_still_404(app_with_panel):
    app, _ = app_with_panel
    assert app.test_client().get('/settings/other').status_code == 404


def test_no_device_means_no_panel(monkeypatch):
    module, system = load_device_panels(monkeypatch, None)
    app = Flask('no-panel')
    module.install(app)
    assert app.config['DEVICE_PANELS'] == []
    saved_keys = system.DEVICE_CONFIG_KEYS
    assert saved_keys == set()


def test_failing_panel_context_does_not_break_the_page(monkeypatch):
    module, _ = load_device_panels(monkeypatch, None)

    def broken():
        raise RuntimeError('boom')

    assert module._safe(broken)() == {}
    assert module._safe(None)() == {}


def test_device_hides_wake_word_tail_flap_and_gain(app_with_panel):
    app, _ = app_with_panel
    html = app.test_client().get('/settings/audio').text
    nav = html.rindex('<a', 0, html.index('id="nav-section-wakeword"'))
    assert 'hidden' in html[nav : html.index('>', nav)]
    # settings-navigation.js shows a menu item unless its panel has class hidden
    panel = html.rindex('<div', 0, html.index('id="section-wakeword"'))
    assert ' hidden"' in html[panel : html.index('>', panel)]
    tail = html.rindex('<div', 0, html.index('for="MIC_TIMEOUT_TAIL_FLAP"'))
    assert 'hidden' in html[tail : html.index('>', tail)]
    gain = html.index('data-hardware-feature="mic-gain"')
    assert 'hidden' in html[gain : html.index('>', gain)]
    assert 'data-audio-test-hint="Lift the handset."' in html
    assert 'data-mic-label="Handset"' in html


def test_audio_tests_open_and_release_the_device_path():
    import contextlib
    import threading

    from conftest import definitions

    f = definitions(
        'application/webconfig/app/routes/audio.py',
        ['_device_audio_open', '_device_audio_close'],
        {'contextlib': contextlib, 'threading': threading},
    )
    events = []
    hooks = {
        'open': lambda timeout: events.append(('open', timeout)) or (False, 'Lift it'),
        'close': lambda: events.append('close'),
    }
    assert f._device_audio_open(None) == (True, '')
    assert f._device_audio_open(hooks, 5) == (False, 'Lift it')
    f._device_audio_close(hooks)
    assert events == [('open', 5), 'close']

    def broken(timeout):
        raise RuntimeError('no phone adapter')

    assert f._device_audio_open({'open': broken}) == (False, 'no phone adapter')


def test_speaker_test_plays_on_a_named_alsa_device():
    import re
    from types import SimpleNamespace

    from conftest import definitions

    listing = 'null\n    Discard all samples\nbilly_phone_speaker\ndefault\n'
    f = definitions(
        'application/webconfig/app/routes/audio.py',
        ['_named_alsa_pcm'],
        {'re': re, 'subprocess': SimpleNamespace(check_output=lambda *a, **k: listing)},
    )
    assert f._named_alsa_pcm('billy_phone_speaker') == 'billy_phone_speaker'
    assert f._named_alsa_pcm('USB Audio (hw:2,0)') is None
    assert f._named_alsa_pcm('missing') is None
    assert f._named_alsa_pcm(None) is None
