"""Exercise provider registration and service startup without Pi hardware or APIs."""

import importlib
import importlib.util
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import ROOT
from flask import Blueprint


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def providers(monkeypatch, tmp_path):
    prefix = '_provider_startup_test'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    package = load_module(prefix, ROOT / 'application/core/__init__.py')
    config = SimpleNamespace(
        ENV_PATH=str(tmp_path / '.env'),
        OPENAI_API_KEY='test-openai',
        OPENAI_MODEL='configured-openai-model',
        OPENAI_LIVE_MODEL='configured-gpt-live-model',
        OPENAI_LIVE_BACKEND_MODEL='configured-live-backend',
        XAI_API_KEY='test-xai',
        XAI_MODEL='configured-xai-model',
        REALTIME_AI_PROVIDER='xai',
    )
    logger = Mock()
    monkeypatch.setitem(sys.modules, f'{prefix}.config', config)
    monkeypatch.setitem(sys.modules, f'{prefix}.logger', SimpleNamespace(logger=logger))
    module = importlib.import_module(f'{prefix}.providers')
    realtime = importlib.import_module(f'{prefix}.providers.realtime')
    wakeword = importlib.import_module(f'{prefix}.wakeword.provider')
    assert realtime.voice_provider_registry.get_available_providers() == []
    assert wakeword.wakeword_provider_registry.get_available_providers() == []
    try:
        yield SimpleNamespace(
            package=package,
            module=module,
            config=config,
            logger=logger,
            voice=realtime.voice_provider_registry,
            wakeword=wakeword.wakeword_provider_registry,
        )
    finally:
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.'):
                sys.modules.pop(name)


def test_imports_do_not_initialize_providers_or_load_configuration(tmp_path):
    # -S also makes third-party libraries unavailable: importing core must not
    # require audio/AI dependencies, read .env or discover hardware.
    result = subprocess.run(
        [
            sys.executable,
            '-I',
            '-S',
            '-c',
            (
                f'import sys; sys.path.insert(0, {str(ROOT / "application")!r}); '
                'import core; assert "core.providers" not in sys.modules; '
                'import core.providers; import core.wakeword; '
                'assert {n for n in sys.modules if n.startswith("core.")} '
                '== {"core.providers", "core.wakeword"}'
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    'openai,xai', [(False, False), (True, False), (False, True), (True, True)]
)
@pytest.mark.parametrize('requested', ['', 'openai', 'xai', 'gpt-live', 'unavailable'])
def test_registration_preserves_provider_selection_and_wakewords(
    providers, openai, xai, requested
):
    providers.config.OPENAI_API_KEY = 'test-openai' if openai else ''
    providers.config.XAI_API_KEY = 'test-xai' if xai else ''
    providers.config.REALTIME_AI_PROVIDER = requested
    providers.module.initialize_providers()
    available = (['openai', 'gpt-live'] if openai else []) + (['xai'] if xai else [])
    assert providers.voice.get_available_providers() == available
    if available:
        expected = requested if requested in available else available[0]
        assert providers.voice.get_provider().get_provider_name() == expected
        for name in available:
            instance = providers.voice.get_provider(name)
            assert (
                instance.api_key == f"test-{'openai' if name == 'gpt-live' else name}"
            )
            assert instance.model == f'configured-{name}-model'
    else:
        assert providers.voice.default_provider is None
        assert (
            'At least one provider API key'
            in providers.logger.warning.call_args.args[0]
        )
    assert providers.wakeword.get_available_providers() == ['porcupine', 'openwakeword']
    assert providers.wakeword.default_provider == 'porcupine'
    assert (
        providers.wakeword.providers['porcupine'].__name__ == 'PorcupineWakeWordBackend'
    )
    assert (
        providers.wakeword.providers['openwakeword'].__name__ == 'OpenWakeWordBackend'
    )


def test_repeated_initialization_preserves_instances_and_runtime_selection(providers):
    providers.module.initialize_providers()
    original = dict(providers.voice.providers)
    providers.voice.set_default_provider('openai')
    providers.wakeword.set_default_provider('openwakeword')
    providers.config.OPENAI_API_KEY = 'changed-after-startup'
    providers.module.initialize_providers()
    assert all(
        providers.voice.get_provider(name) is value for name, value in original.items()
    )
    assert providers.voice.default_provider == 'openai'
    assert providers.wakeword.default_provider == 'openwakeword'


def expose_core(providers, monkeypatch):
    monkeypatch.setitem(sys.modules, 'core', providers.package)
    monkeypatch.setitem(sys.modules, 'core.providers', providers.module)


@pytest.mark.parametrize('with_keys', [True, False])
def test_core_initializes_before_profile_audio_bluetooth_and_realtime_loop(
    providers, monkeypatch, with_keys
):
    expose_core(providers, monkeypatch)
    if not with_keys:
        providers.config.OPENAI_API_KEY = providers.config.XAI_API_KEY = ''
    events = []

    def started(name):
        assert providers.wakeword.get_available_providers() == [
            'porcupine',
            'openwakeword',
        ]
        assert providers.voice.get_available_providers() == (
            ['openai', 'gpt-live', 'xai'] if with_keys else []
        )
        events.append(name)

    button = SimpleNamespace(
        start_loop=lambda on_ready: (started('button'), on_ready())
    )
    monkeypatch.setattr(providers.package, 'button', button, raising=False)
    modules = {
        'core.button': button,
        'core.audio': SimpleNamespace(
            initialize_devices=lambda: started('audio'), shutdown_playback_worker=Mock()
        ),
        'core.bluetooth': SimpleNamespace(
            watcher=SimpleNamespace(start=lambda: started('bluetooth'))
        ),
        'core.logger': SimpleNamespace(
            logger=Mock(), reload_log_level=lambda: SimpleNamespace(name='INFO')
        ),
        'core.movements': SimpleNamespace(
            start_motor_watchdog=lambda: started('motors')
        ),
        'core.mqtt': SimpleNamespace(
            start_mqtt=lambda: started('mqtt'), stop_mqtt=Mock()
        ),
        'core.readiness': SimpleNamespace(
            clear_ready=Mock(), mark_ready=lambda: started('ready')
        ),
        'core.status_led': SimpleNamespace(
            cleanup_status_led=Mock(),
            initialize_status_led=Mock(),
            set_status_led_state=Mock(),
        ),
        'core.profile_manager': SimpleNamespace(
            user_manager=SimpleNamespace(load_default_user=lambda: started('profile'))
        ),
        'billy_runtime.worker': SimpleNamespace(ready=lambda: started('runtime-ready')),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    module_name = '_provider_test_main'
    try:
        main = load_module(module_name, ROOT / 'application/core/application.py')
        assert providers.voice.get_available_providers() == []
        assert providers.wakeword.get_available_providers() == []
        monkeypatch.setattr(main.signal, 'signal', Mock())
        monkeypatch.setattr(
            main.threading, 'Thread', lambda target, **_: SimpleNamespace(start=target)
        )
        main.main()
        assert events == [
            'profile',
            'audio',
            'motors',
            'button',
            'bluetooth',
            'mqtt',
            'ready',
            'runtime-ready',
        ]
        main.main()
        if not with_keys:
            assert providers.logger.warning.call_count == 1
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize('factory_first', [False, True])
def test_web_factory_and_server_initialize_before_profiles_routes_and_background_tasks(
    providers, monkeypatch, factory_first
):
    expose_core(providers, monkeypatch)
    prefix = '_provider_test_web'
    events = []
    instance = []

    def assert_initialized(stage):
        assert providers.voice.default_provider == 'xai'
        assert providers.wakeword.get_available_providers() == [
            'porcupine',
            'openwakeword',
        ]
        events.append(stage)
        instance.append(providers.voice.get_provider())

    try:
        web = load_module(prefix, ROOT / 'application/webconfig/app/__init__.py')
        for name, attribute in [
            ('audio', 'bp'),
            ('bluetooth', 'bp'),
            ('knowledge', 'knowledge_bp'),
            ('misc', 'bp'),
            ('persona', 'bp'),
            ('profiles', 'profiles_bp'),
            ('songs', 'songs_bp'),
            ('system', 'bp'),
        ]:
            route = ModuleType(f'{prefix}.routes.{name}')
            setattr(route, attribute, Blueprint(name, __name__))
            if name == 'audio':
                route.apply_configured_mic_gain = Mock()
            monkeypatch.setitem(sys.modules, route.__name__, route)
        monkeypatch.setitem(sys.modules, f'{prefix}.websocket', ModuleType('websocket'))
        monkeypatch.setitem(
            sys.modules,
            f'{prefix}.state',
            SimpleNamespace(bootstrap_versions_and_release_note=Mock()),
        )
        monkeypatch.setattr(web, 'SOCK_AVAILABLE', False)
        monkeypatch.setattr(
            web.threading,
            'Thread',
            lambda **_: SimpleNamespace(start=lambda: assert_initialized('background')),
        )
        monkeypatch.setitem(
            sys.modules,
            'core.bluetooth.provisioning',
            SimpleNamespace(start_setup=lambda: assert_initialized('bluetooth-setup')),
        )
        monkeypatch.setitem(
            sys.modules,
            'core.profile_manager',
            SimpleNamespace(
                user_manager=SimpleNamespace(
                    load_default_user=lambda: assert_initialized('profile')
                )
            ),
        )
        # The standalone app factory must also initialize providers.
        if factory_first:
            app = web.create_app()
            assert len(app.blueprints) == 8
            assert events == ['background', 'background', 'bluetooth-setup']
            events.clear()
        monkeypatch.setitem(sys.modules, 'webconfig.app', web)
        server = load_module(
            '_provider_test_server', ROOT / 'application/webconfig/startup.py'
        )
        app = server.normal_app()
        assert events == ['profile', 'background', 'background', 'bluetooth-setup']
        assert all(value is instance[0] for value in instance)
        assert app.test_client().get('/__runtime_health').status_code == 200
    finally:
        sys.modules.pop('_provider_test_server', None)
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.'):
                sys.modules.pop(name)


@pytest.mark.parametrize('backend', ['porcupine', 'openwakeword'])
def test_grouped_listener_uses_registered_backend_without_opening_hardware(
    providers, monkeypatch, tmp_path, backend
):
    providers.config.ROOT_DIR = str(tmp_path)
    audio = SimpleNamespace(
        RawInputStream=Mock(side_effect=AssertionError('opened mic'))
    )
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    providers.module.initialize_providers()
    module = importlib.import_module(f'{providers.package.__name__}.wakeword.listener')
    listener = module.LocalWakeWordListener(
        backend=backend,
        callback=Mock(),
        is_session_active=lambda: False,
        porcupine_access_key='test-key',
        porcupine_keyword_path='hey-billy.ppn',
        porcupine_sensitivity=0.4,
        openwakeword_model_path='hey-billy.onnx',
        openwakeword_threshold=0.6,
    )
    implementation = listener._build_backend()
    assert type(implementation) is providers.wakeword.providers[backend]
    assert implementation.backend_name == backend
    assert implementation.root_dir == str(tmp_path)
    assert implementation._model is None
    if backend == 'porcupine':
        assert implementation.keyword_path_raw == 'hey-billy.ppn'
        assert implementation.sensitivity == 0.4
    else:
        assert implementation.model_path_raw == 'hey-billy.onnx'
        assert implementation.threshold == 0.6
    audio.RawInputStream.assert_not_called()


@pytest.mark.parametrize(
    'value,relative',
    [
        ('hey-billy.ppn', 'wakewords/hey-billy.ppn'),
        ('hey-billy.onnx', 'wakewords/hey-billy.onnx'),
        ('models/custom.onnx', 'models/custom.onnx'),
    ],
)
def test_shared_wakeword_lookup_preserves_filename_and_relative_paths(
    providers, tmp_path, value, relative
):
    paths = importlib.import_module(f'{providers.package.__name__}.wakeword.paths')
    expected = tmp_path / relative
    assert paths.resolve_keyword_path(str(tmp_path), value) == expected
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.touch()
    assert paths.resolve_keyword_path(str(tmp_path), value) == expected
    assert paths.resolve_keyword_path(str(tmp_path), str(expected)) == expected
