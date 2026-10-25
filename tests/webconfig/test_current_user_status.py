"""The web worker follows the user Billy's core switched to, without a reload."""

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import definitions
from flask import Flask, jsonify


def test_live_status_follows_current_user_saved_by_billy(tmp_path, monkeypatch):
    import billy_runtime.paths as paths

    monkeypatch.setattr(paths, 'INSTALL_ROOT', tmp_path)
    status = definitions(
        'application/webconfig/app/websocket.py',
        ['saved_current_user'],
        {'_current_user_cache': {'stamp': None, 'value': None}},
    )
    env = tmp_path / '.env'
    env.write_text('CURRENT_USER=guest\n')
    assert status.saved_current_user() == 'guest'
    os.environ['CURRENT_USER'] = 'guest'  # what load_dotenv() kept seeing
    try:
        env.write_text('CURRENT_USER=tom\n')
        os.utime(env, ns=(time.time_ns() + 10**9,) * 2)
        assert status.saved_current_user() == 'tom'
        env.write_text('CURRENT_USER=\n')
        os.utime(env, ns=(time.time_ns() + 2 * 10**9,) * 2)
        assert status.saved_current_user() == 'guest'
    finally:
        os.environ.pop('CURRENT_USER', None)


@pytest.fixture
def current_user_route(tmp_path, monkeypatch):
    profiles = {
        name: SimpleNamespace(
            name=name,
            data={'USER_INFO': {'preferred_persona': 'default'}},
            get_memories=lambda _n: [],
            get_context_string=lambda: '',
        )
        for name in ('Fred', 'Tom')
    }
    manager = SimpleNamespace(current=profiles['Fred'])
    manager.get_current_user = lambda: manager.current

    def identify(name, _confidence):
        manager.current = profiles[name.title()]
        return manager.current

    manager.identify_user = Mock(side_effect=identify)
    monkeypatch.setitem(
        sys.modules, 'core.config', SimpleNamespace(DEFAULT_USER='guest')
    )
    monkeypatch.setitem(
        sys.modules,
        'core.persona_manager',
        SimpleNamespace(persona_manager=SimpleNamespace(switch_persona=Mock())),
    )
    monkeypatch.setitem(
        sys.modules, 'core.profile_manager', SimpleNamespace(user_manager=manager)
    )
    env = tmp_path / '.env'

    def load_environment(path, override=False):
        for line in path.read_text().splitlines():
            key, _, value = line.partition('=')
            if override or key not in os.environ:
                os.environ[key] = value

    route = definitions(
        'application/webconfig/app/routes/profiles.py',
        ['get_current_user'],
        {
            'profiles_bp': SimpleNamespace(route=lambda *a, **k: lambda f: f),
            '_get_env_path': lambda: env,
            'load_environment': load_environment,
            'os': os,
            'jsonify': jsonify,
            'logger': Mock(),
        },
    )
    yield SimpleNamespace(env=env, manager=manager, get=route.get_current_user)
    os.environ.pop('CURRENT_USER', None)


@pytest.mark.parametrize(
    'saved,expected', [('tom', 'Tom'), ('guest', None), ('fred', 'Fred')]
)
def test_current_user_page_reports_the_saved_user_not_an_older_one(
    current_user_route, saved, expected
):
    current_user_route.env.write_text(f'CURRENT_USER={saved}\n')
    with Flask(__name__).test_request_context():
        user = current_user_route.get().get_json()['user']
    assert (user and user['name']) == expected
