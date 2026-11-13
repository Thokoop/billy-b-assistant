"""Provider-aware controls must agree before and after JavaScript loads."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from flask import Flask, render_template


ROOT = Path(__file__).parents[2]


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.controls = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.controls[attrs['id']] = (attrs, list(self.stack))
        if tag not in {'input', 'img', 'br', 'hr', 'link', 'meta'}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def available(self, name):
        _, parents = self.controls[name]
        return not any('hidden' in attrs for _, attrs in parents)


def render(config, category='voice', template='components/settings-form.html'):
    app = Flask(__name__, template_folder=str(ROOT / 'application/webconfig/templates'))
    with app.test_request_context('/settings'):
        return render_template(template, config=config, settings_category=category)


@pytest.mark.parametrize('provider', ['openai', 'xai', 'gpt-live'])
def test_only_applicable_conversation_controls_are_shown(provider):
    controls = Controls(render({'REALTIME_AI_PROVIDER': provider}))
    for name in [
        'TURN_EAGERNESS',
        'FOLLOW_UP_RETRY_LIMIT',
        'AEC_BARGE_IN_SNR_DB',
    ]:
        assert controls.available(name) == (provider != 'gpt-live')
        assert 'disabled' not in controls.controls[name][0]
    for name in ['OPENAI_LIVE_MODEL', 'OPENAI_LIVE_BACKEND_MODEL']:
        assert controls.available(name) == (provider == 'gpt-live')
    assert controls.available('OPENAI_MODEL') == (provider == 'openai')
    assert controls.available('XAI_MODEL') == (provider == 'xai')
    assert controls.available('OPENAI_API_KEY') == (provider != 'xai')
    assert controls.available('WEB_SEARCH_ENABLED') == (provider == 'openai')
    # Voice interruption needs echo cancellation under every provider.
    assert controls.available('AEC_ENABLED')
    assert controls.available('MOOD_INSTRUCTIONS_ENABLED')
    assert ('hidden' in controls.controls['mood-live-label'][0]) == (
        provider != 'gpt-live'
    )
    assert ('hidden' in controls.controls['mood-realtime-label'][0]) == (
        provider == 'gpt-live'
    )


@pytest.mark.parametrize('provider', ['openai', 'xai', 'gpt-live'])
def test_audio_settings_hide_only_what_live_never_uses(provider):
    controls = Controls(render({'REALTIME_AI_PROVIDER': provider}, 'audio'))
    # Live reads these itself: idle timeout and speech threshold.
    for name in ['MIC_TIMEOUT_SECONDS', 'SILENCE_THRESHOLD']:
        assert controls.available(name)
    # Live replaces the Realtime timeout loop that flaps the tail.
    assert controls.available('MIC_TIMEOUT_TAIL_FLAP') == (provider != 'gpt-live')
    assert 'disabled' not in controls.controls['MIC_TIMEOUT_TAIL_FLAP'][0]


def test_live_retains_hidden_realtime_values_and_selected_models():
    html = render({
        'REALTIME_AI_PROVIDER': 'gpt-live',
        'TURN_EAGERNESS': 'low',
        'FOLLOW_UP_RETRY_LIMIT': '4',
        'AEC_BARGE_IN_SNR_DB': '12',
        'WEB_SEARCH_ENABLED': 'true',
        'AEC_ENABLED': 'true',
        'OPENAI_LIVE_MODEL': 'custom-live',
        'OPENAI_LIVE_BACKEND_MODEL': 'gpt-6-sol',
    })
    assert '<option value="low" selected>' in html
    assert '<option value="12" selected>' in html
    assert Controls(html).controls['FOLLOW_UP_RETRY_LIMIT'][0]['value'] == '4'
    assert '<option value="custom-live" selected>' in html
    assert '<option value="gpt-6-sol" selected>' in html


def test_hiding_live_controls_does_not_disable_saving_their_realtime_values():
    controls = Controls(render({'REALTIME_AI_PROVIDER': 'gpt-live'}))
    for name in ['WEB_SEARCH_ENABLED']:
        assert not controls.available(name)
        assert 'disabled' not in controls.controls[name][0]
        assert controls.controls[name][0]['name'] == name


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_live_mood_control_still_saves_its_value(enabled):
    controls = Controls(
        render({
            'REALTIME_AI_PROVIDER': 'gpt-live',
            'MOOD_INSTRUCTIONS_ENABLED': enabled,
        })
    )
    toggle, _ = controls.controls['MOOD_INSTRUCTIONS_ENABLED_toggle']
    assert ('checked' in toggle) == (enabled == 'true')
    assert toggle['aria-labelledby'] == 'mood-setting-label'
    assert (
        controls.controls['MOOD_INSTRUCTIONS_ENABLED'][0]['name']
        == 'MOOD_INSTRUCTIONS_ENABLED'
    )


@pytest.mark.parametrize('provider', ['openai', 'xai', 'gpt-live'])
def test_live_conversation_style_is_only_shown_for_gpt_live(provider):
    controls = Controls(render({'REALTIME_AI_PROVIDER': provider}))
    for name in ['LIVE_BACKCHANNELS', 'LIVE_IGNORE_BACKGROUND']:
        assert controls.available(name) == (provider == 'gpt-live')
        assert controls.controls[name][0]['name'] == name
    assert 'LIVE_REPLY_LENGTH' not in controls.controls
    assert 'LIVE_BACKCHANNELS-bar' in controls.controls


def test_live_conversation_style_shows_saved_values():
    html = render({
        'REALTIME_AI_PROVIDER': 'gpt-live',
        'LIVE_BACKCHANNELS': 'off',
        'LIVE_IGNORE_BACKGROUND': 'false',
    })
    assert '<option value="off" selected>' in html
    toggle, _ = Controls(html).controls['LIVE_IGNORE_BACKGROUND_toggle']
    assert 'checked' not in toggle


class VoiceDocs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'data-voice-docs' in attrs:
            self.links[attrs['data-voice-docs']] = attrs


@pytest.mark.parametrize('provider', ['openai', 'xai', 'gpt-live'])
def test_voice_help_links_to_the_selected_providers_docs(provider):
    html = render(
        {'REALTIME_AI_PROVIDER': provider, 'VOICE_OPTIONS': ['marin']},
        template='components/persona-form.html',
    )
    links = VoiceDocs(html).links
    assert set(links) == {'openai', 'xai', 'gpt-live'}
    assert [name for name, attrs in links.items() if 'hidden' not in attrs] == [
        provider
    ]
    for attrs in links.values():
        assert attrs['href'].startswith((
            'https://developers.openai.com/',
            'https://docs.x.ai/',
        ))
        assert attrs['target'] == '_blank'
        assert 'noopener' in attrs['rel']


@pytest.mark.parametrize('provider', ['openai', 'gpt-live'])
def test_settings_form_divs_are_balanced(provider):
    # A stray </div> closes the settings panel early and moves the save bar
    # into the navigation column.
    html = render({'REALTIME_AI_PROVIDER': provider})
    assert html.count('<div') == html.count('</div>')
    _, parents = Controls(html).controls['settings-save-status']
    assert not any(attrs.get('data-settings-panel') is not None for _, attrs in parents)


def test_custom_voice_creation_panel_is_not_part_of_the_saved_settings():
    html = render({'REALTIME_AI_PROVIDER': 'gpt-live'})
    controls = Controls(html)
    toggle, _ = controls.controls['live-custom-voice-create-toggle']
    assert toggle['data-disclosure-target'] == 'live-custom-voice-create'
    assert toggle['aria-expanded'] == 'false'
    panel, _ = controls.controls['live-custom-voice-create']
    assert 'hidden' in panel and 'data-disclosure-panel' in panel
    for name in ['name', 'description', 'script']:
        # Unnamed: drafts, dirty tracking and /save leave them out.
        attrs, _ = controls.controls[f'live-custom-voice-create-{name}']
        assert 'name' not in attrs
    template = html.split('id="live-custom-voice-row"', 1)[1]
    assert 'data-custom-voice-description' in template


def custom_voice_app(monkeypatch, create, saved=()):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock

    from conftest import definitions
    from flask import Blueprint, jsonify, request

    class VoiceError(Exception):
        pass

    module = SimpleNamespace(MAX_VOICES=2, VoiceError=VoiceError, create=create)
    monkeypatch.setitem(sys.modules, 'core.live_voices', module)
    bp = Blueprint('voices', __name__)
    definitions(
        'application/webconfig/app/routes/system.py',
        ['create_custom_voice'],
        {
            'bp': bp,
            'jsonify': jsonify,
            'request': request,
            'logger': Mock(),
            '_custom_voices': lambda: list(saved),
            '_config_value_for_ui': lambda key: {'OPENAI_API_KEY': 'sk-test'}[key],
        },
    )
    app = Flask(__name__)
    app.register_blueprint(bp)
    return app.test_client(), VoiceError


def test_custom_voice_route_creates_with_the_saved_key(monkeypatch):
    from unittest.mock import Mock

    voice = {
        'name': 'Gil',
        'id': 'voice_gil1',
        'accent': '',
        'description': 'Gravelly.',
    }
    create = Mock(return_value=voice)
    client, _ = custom_voice_app(monkeypatch, create)
    response = client.post(
        '/voices/custom',
        json={'name': 'Gil', 'description': 'Gravelly.', 'script': 'Hi.'},
    )
    assert response.status_code == 200
    assert response.get_json() == {'voice': voice}
    create.assert_called_once_with('sk-test', 'Gil', 'Gravelly.', 'Hi.')


def test_custom_voice_route_rejects_forms_full_lists_and_reports_errors(monkeypatch):
    from unittest.mock import Mock

    create = Mock()
    client, error = custom_voice_app(monkeypatch, create)
    # A cross-site form post cannot send JSON without a CORS preflight.
    assert client.post('/voices/custom', data={'name': 'Gil'}).status_code == 415
    create.side_effect = error('OpenAI refused the API key')
    response = client.post('/voices/custom', json={'name': 'Gil'})
    assert response.status_code == 400
    assert response.get_json() == {'error': 'OpenAI refused the API key'}

    full = Mock()
    client, _ = custom_voice_app(monkeypatch, full, saved=[{}, {}])
    assert client.post('/voices/custom', json={'name': 'Gil'}).status_code == 400
    full.assert_not_called()


@pytest.mark.parametrize('provider', ['openai', 'xai', 'gpt-live'])
def test_voice_panel_groups_its_settings_in_section_cards(provider):
    # Like Device and Software: one card per group, the panel title in the first.
    controls = Controls(render({'REALTIME_AI_PROVIDER': provider}))

    def card(name):
        _, parents = controls.controls[name]
        cards = [
            attrs for _, attrs in parents if 'card' in attrs.get('class', '').split()
        ]
        assert len(cards) == 1, name
        return cards[0]

    panel, _ = controls.controls['section-voice']
    assert 'settings-card--sections' in panel['class']
    first = card('title-section-voice')
    assert card('REALTIME_AI_PROVIDER') is first
    assert card('OPENAI_LIVE_MODEL') is first
    custom = card('OPENAI_LIVE_CUSTOM_VOICES')
    assert custom is card('title-live-custom-voices') and custom is not first
    assert (custom['id'], custom['data-api-provider']) == (
        'live-custom-voices',
        'gpt-live',
    )
    assert controls.available('OPENAI_LIVE_CUSTOM_VOICES') == (provider == 'gpt-live')
    conversation = card('title-voice-conversation')
    for name in ['AEC_BARGE_IN_SNR_DB', 'LIVE_BACKCHANNELS', 'TURN_EAGERNESS']:
        assert card(name) is conversation
    assert len({id(first), id(custom), id(conversation)}) == 3


def test_ignore_background_talk_comes_before_listening_sounds():
    html = render({'REALTIME_AI_PROVIDER': 'gpt-live'})
    assert html.index('id="LIVE_IGNORE_BACKGROUND"') < html.index(
        'id="LIVE_BACKCHANNELS"'
    )


def test_custom_voice_rows_have_no_surface_of_their_own():
    # Design system 3.12: editable rows sit on the card, fields only.
    html = render({'REALTIME_AI_PROVIDER': 'gpt-live'})
    template = html.split('id="live-custom-voice-row"', 1)[1].split('</template>')[0]
    row = template.split('data-custom-voice-row', 1)[0]
    assert 'bg-' not in row and 'rounded' not in row
