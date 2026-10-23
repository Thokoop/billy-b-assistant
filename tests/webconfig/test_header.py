"""Hostname rendering must use the device, not the browser URL."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions
from jinja2 import Environment, FileSystemLoader, select_autoescape


def test_context_uses_os_hostname():
    system = SimpleNamespace(
        uname=Mock(return_value=SimpleNamespace(nodename='billy-kitchen'))
    )
    context = definitions(
        'webconfig/app/__init__.py', ['header_context'], {'os': system}
    )
    assert context.header_context() == {'device_hostname': 'billy-kitchen'}


@pytest.mark.parametrize(
    'hostname,expected',
    [
        ('billy', 'Billy'),
        ('billy-kitchen', 'Billy-kitchen'),
        ('bILLY', 'BILLY'),
        ('', 'Billy'),
        ('<fish>', '&lt;fish&gt;'),
    ],
)
def test_header_name_capitalizes_first_letter_and_escapes(hostname, expected):
    templates = Path(__file__).parents[2] / 'webconfig/templates'
    env = Environment(
        loader=FileSystemLoader(templates), autoescape=select_autoescape()
    )
    html = env.get_template('components/header.html').render(device_hostname=hostname)
    assert f'title="{expected}">{expected}</span>' in html
    assert f'aria-label="{expected} home"' in html
    assert (
        html.index('billy-brand-name')
        < html.index('id="header-bluetooth"')
        < html.index('billy-state-line')
    )
