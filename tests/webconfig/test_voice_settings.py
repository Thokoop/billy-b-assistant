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
