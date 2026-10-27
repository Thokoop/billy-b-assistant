"""Local checks for individual UI script delivery."""

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import Mock

import pytest
from conftest import definitions
from flask import Blueprint, Flask, abort, redirect, render_template


ROOT = Path(__file__).parents[2]


def fixture():
    app = Flask(
        __name__,
        template_folder=str(ROOT / 'application/webconfig/templates'),
        static_folder=str(ROOT / 'application/webconfig/static'),
    )
    with app.test_request_context('/settings'):
        html = render_template(
            'settings.html', current_page='settings', config={}, bluetooth_token='test'
        )
    urls = re.findall(r'<script src="([^"]+)"', html)
    return app, html, urls


def test_individual_scripts_available_in_dependency_order():
    app, html, urls = fixture()
    assert len(urls) == len(set(urls)) == 26
    assert urls.index('/static/js/live-captions.js') < urls.index(
        '/static/js/websocket.js'
    )
    assert urls.index('/static/js/settings-navigation.js') < urls.index(
        '/static/js/page-router.js'
    )
    assert urls[-2:] == ['/static/js/page-router.js', '/static/js/init.js']
    assert urls.index('/static/js/ui-helpers.js') < urls.index(
        '/static/js/service-status.js'
    )
    assert urls.index('/static/js/websocket.js') < urls.index('/static/js/bluetooth.js')
    assert '/ui/assets/' not in html
    assert html.index('window.ScriptLoadRecovery =') < html.index('<script src=')
    assert html.rindex('<script src=') < html.index(
        'window.ScriptLoadRecovery.complete()'
    )
    client = app.test_client()
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200
        assert 'no-cache' in response.headers['Cache-Control']
        assert 'immutable' not in response.headers['Cache-Control']


CATEGORIES = {
    'voice': 'section-voice',
    'audio': 'section-audio',
    'wakeword': 'section-wakeword',
    'network': 'section-internet',
    'bluetooth': 'section-bluetooth',
    'mqtt': 'section-mqtt',
    'home-assistant': 'section-ha',
    'hardware': 'section-hardware',
    'software': 'section-software',
    'advanced': 'section-advanced-settings',
}


class SettingsMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.panels = {}
        self.navigation = {}
        self.ids = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            assert attrs['id'] not in self.ids, attrs['id']
            self.ids.add(attrs['id'])
        if 'data-settings-panel' in attrs:
            self.panels[attrs['id']] = attrs
        if 'data-settings-target' in attrs:
            self.navigation[attrs['data-settings-target']] = attrs


@pytest.fixture
def settings_routes():
    app = Flask(
        'settings-categories',
        template_folder=str(ROOT / 'application/webconfig/templates'),
        static_folder=str(ROOT / 'application/webconfig/static'),
    )
    bp = Blueprint('settings_routes', __name__)
    page_context = Mock(
        return_value={
            'current_page': 'settings',
            'config': {'HA_HOST': 'https://saved.example.test'},
            'bluetooth_token': 'synthetic-token',
        }
    )
    definitions(
        'application/webconfig/app/routes/system.py',
        ['settings_page', 'captive_portal_fallback'],
        {
            'bp': bp,
            'abort': abort,
            'render_template': render_template,
            '_page_context': page_context,
            '_is_unified_wifi_hotspot_onboarding': lambda: True,
            '_captive_portal_redirect': lambda: redirect('/settings'),
        },
    )
    app.register_blueprint(bp)
    return app.test_client(), page_context


@pytest.mark.parametrize('category', [None, *CATEGORIES])
def test_settings_paths_render_the_requested_category_with_complete_navigation(
    settings_routes,
    category,
):
    client, page_context = settings_routes
    path = '/settings' + (f'/{category}' if category else '')
    response = client.get(path)
    assert response.status_code == 200
    page_context.assert_called_once_with('settings')
    assert 'https://saved.example.test' in response.text
    markup = SettingsMarkup(response.text)
    assert set(markup.panels) == set(CATEGORIES.values())
    assert [key for key, attrs in markup.panels.items() if 'hidden' not in attrs] == [
        CATEGORIES[category or 'voice']
    ]
    for slug, panel in CATEGORIES.items():
        control = markup.navigation[panel]
        assert control['href'] == f'/settings/{slug}'
        assert control['data-settings-path'] == f'/settings/{slug}'
        assert control['aria-controls'] == panel
        assert markup.panels[panel]['aria-labelledby'] in markup.ids


@pytest.mark.parametrize(
    'path',
    [
        '/settings/unknown',
        '/settings/SOFTWARE',
        '/settings/software/extra',
        '/settings/software-version',
        '/settings/home_assistant',
    ],
)
def test_unknown_settings_paths_are_404_even_during_onboarding(settings_routes, path):
    client, page_context = settings_routes
    response = client.get(path)
    assert response.status_code == 404
    assert 'Location' not in response.headers
    page_context.assert_not_called()


def test_captive_portal_fallback_still_redirects_unrelated_paths(settings_routes):
    client, page_context = settings_routes
    response = client.get('/captive-browser-probe')
    assert response.status_code == 302
    assert response.location == '/settings'
    assert client.get('/static/not-a-real-file').status_code == 404
    page_context.assert_not_called()


@pytest.mark.parametrize(
    'port,location',
    [
        ('80', 'http://10.42.0.1/settings/network'),
        ('8080', 'http://10.42.0.1:8080/settings/network'),
    ],
)
def test_captive_portal_opens_network_settings(port, location):
    from types import SimpleNamespace

    routes = definitions(
        'application/webconfig/app/routes/system.py',
        ['_captive_portal_redirect'],
        {
            'redirect': redirect,
            '_get_wlan0_ip_address': lambda: '10.42.0.1',
            'core_config': SimpleNamespace(FLASK_PORT=port),
        },
    )
    with Flask('captive-portal').test_request_context():
        response = routes._captive_portal_redirect()
    assert response.status_code == 302
    assert response.location == location


if __name__ == '__main__':
    app, html, urls = fixture()
    print(
        json.dumps({
            'html': html,
            'scripts': {
                url: (ROOT / 'application/webconfig' / url.lstrip('/')).read_text()
                for url in urls
            },
        })
    )
