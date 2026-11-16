"""Load real modules with hardware boundaries replaced before import.

Load components in isolation without service startup or device state.
Each test gets an isolated package, temporary working directory and fake GPIO,
PortAudio, logger and config. No API keys, sound devices or Pi are needed.
"""

import ast
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _use_test_installation():
    """Keep the suite away from the checkout's own installation data.

    In a checkout INSTALL_ROOT is the repository itself, so core.config would
    load the developer's real .env (API keys, MQTT broker) into os.environ and
    tests could read or write its profiles and state. billy_runtime.paths
    resolves INSTALL_ROOT once, at import: point it at an empty temporary
    installation for this process, then remove the variable again so Billy
    workers that tests start still resolve their own root.
    """
    if 'billy_runtime.paths' in sys.modules:
        raise RuntimeError('billy_runtime.paths was imported before conftest.py')
    root = Path(tempfile.mkdtemp(prefix='billy-test-install-'))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    # Shipped with every installation; startup copies it as the default.
    shutil.copy2(ROOT / 'persona.ini.example', root / 'persona.ini.example')
    previous = os.environ.get('BILLY_INSTALL_ROOT')
    os.environ['BILLY_INSTALL_ROOT'] = str(root)
    try:
        import billy_runtime.paths  # noqa: F401  (resolves INSTALL_ROOT now)
    finally:
        if previous is None:
            os.environ.pop('BILLY_INSTALL_ROOT', None)
        else:
            os.environ['BILLY_INSTALL_ROOT'] = previous
    return root


TEST_INSTALL_ROOT = _use_test_installation()


def definitions(relative, names, namespace, *, cls=None):
    tree = ast.parse((ROOT / relative).read_text())
    nodes = tree.body
    if cls:
        nodes = next(n for n in nodes if getattr(n, 'name', None) == cls).body
    body = [n for n in nodes if getattr(n, 'name', None) in names]
    assert len(body) == len(names), (relative, names)
    future = ast.ImportFrom(
        module='__future__', names=[ast.alias(name='annotations')], level=0
    )
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[future, *body], type_ignores=[])
            ),
            str(ROOT / relative),
            'exec',
        ),
        namespace,
    )
    return SimpleNamespace(**{name: namespace[name] for name in names})


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    package = ModuleType('isolated_core')
    package.__path__ = [str(ROOT / 'application/core')]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    config = SimpleNamespace(
        AEC_ENABLED=False,
        CHUNK_MS=40,
        MIC_PREFERENCE='mic',
        PLAYBACK_VOLUME=0.5,
        SPEAKER_PREFERENCE='speaker',
        TEXT_ONLY_MODE=False,
        BILLY_PINS='new',
        MOCKFISH=True,
        MOUTH_ARTICULATION=1,
        MOUTH_BOOST=1,
        is_classic_billy=lambda: False,
    )
    monkeypatch.setitem(sys.modules, 'isolated_core.config', config)
    logger = Mock()
    monkeypatch.setitem(
        sys.modules, 'isolated_core.logger', SimpleNamespace(logger=logger)
    )
    # Even on a Pi the real lgpio module cannot be selected (MOCKFISH=True).
    monkeypatch.setattr(atexit, 'register', Mock())

    def load(name):
        qualified = f'isolated_core.{name}'
        spec = importlib.util.spec_from_file_location(
            qualified, ROOT / 'application/core' / (name.replace('.', '/') + '.py')
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, qualified, module)
        spec.loader.exec_module(module)
        return module

    return SimpleNamespace(load=load, config=config, logger=logger, package=package)


@pytest.fixture
def audio_module(isolated, monkeypatch):
    movements = Mock(head_out=False)
    monkeypatch.setitem(sys.modules, 'isolated_core.movements', movements)
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.echo_canceller',
        SimpleNamespace(EchoCanceller=Mock()),
    )
    sd = SimpleNamespace(OutputStream=MagicMock())
    stream = sd.OutputStream.return_value.__enter__.return_value
    stream.latency = 0.02
    monkeypatch.setitem(sys.modules, 'sounddevice', sd)
    module = isolated.load('audio')
    module.echo_canceller.begin_render_generation.return_value = 1
    module.echo_canceller.is_current_render_generation.return_value = True
    module.echo_canceller.heard_audio_ms.return_value = 0
    module.song_start_time = 0.0
    module.test_stream = stream
    return module
