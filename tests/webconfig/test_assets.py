"""Local checks for individual UI script delivery."""

import json
import re
from pathlib import Path

from flask import Flask, render_template


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
    assert len(urls) == len(set(urls)) == 25
    assert urls.index('/static/js/live-captions.js') < urls.index(
        '/static/js/websocket.js'
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
