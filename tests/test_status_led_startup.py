"""Optional LED dependencies must not prevent an older unit from booting."""

import builtins
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest


@pytest.fixture
def led_runtime(isolated, monkeypatch, tmp_path):
    isolated.config.STATUS_LED_ENABLED = True
    isolated.config.MOCKFISH = False
    isolated.config.STATUS_LED_BRIGHTNESS = 0.5
    isolated.config.STATUS_LED_BACKEND = 'spi'
    isolated.config.STATUS_LED_COUNT = 1
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.wifi_setup',
        SimpleNamespace(ONBOARDING_FLAG=tmp_path / 'onboarding'),
    )
    monkeypatch.setitem(
        sys.modules,
        'rpi_ws281x',
        SimpleNamespace(
            Color=Mock(),
            PixelStrip=Mock(),
            ws=Mock(),
        ),
    )
    calls = []
    real_import = builtins.__import__
    error = RuntimeError("The platform library 'gpiod' was not found")
    strip = MagicMock()
    board = SimpleNamespace(SPI=Mock())
    pixels = SimpleNamespace(NeoPixel_SPI=Mock(return_value=strip))

    def import_led(name, *args, **kwargs):
        if name in {'board', 'neopixel_spi'}:
            calls.append(name)
            if state.error:
                raise state.error
            return board if name == 'board' else pixels
        return real_import(name, *args, **kwargs)

    state = SimpleNamespace(
        error=error,
        imports=calls,
        board=board,
        pixels=pixels,
        strip=strip,
        config=isolated.config,
        logger=isolated.logger,
    )
    monkeypatch.setattr(builtins, '__import__', import_led)

    def load():
        module = isolated.load('status_led')
        monkeypatch.setattr(module.threading, 'Thread', Mock())
        monkeypatch.setattr(module, 'set_activity', Mock())
        state.module = module
        state.led = module.status_led
        return state

    state.load = load
    return state


@pytest.mark.parametrize('enabled,mock', [(False, False), (True, True)])
def test_disabled_or_mock_led_never_imports_blinka(led_runtime, enabled, mock):
    led_runtime.config.STATUS_LED_ENABLED = enabled
    led_runtime.config.MOCKFISH = mock
    runtime = led_runtime.load()
    runtime.led.initialize()
    runtime.led.set_state('listening')
    assert runtime.imports == []
    runtime.module.set_activity.assert_called_once_with('listening')
    runtime.logger.warning.assert_not_called()
    runtime.module.threading.Thread.assert_not_called()


@pytest.mark.parametrize(
    'error',
    [
        RuntimeError("The platform library 'gpiod' was not found"),
        ModuleNotFoundError("No module named 'board'"),
        OSError('GPIO library cannot be loaded'),
    ],
)
def test_spi_dependency_failure_warns_but_keeps_activity_working(led_runtime, error):
    led_runtime.error = error
    runtime = led_runtime.load()
    assert runtime.imports == []
    runtime.led.initialize()
    assert runtime.led._initialized is False
    assert runtime.led._strip is None
    assert any(
        str(error) in call.args[0] for call in runtime.logger.warning.call_args_list
    )
    runtime.led.set_state('listening')
    runtime.module.set_activity.assert_called_once_with('listening')
    runtime.led.cleanup()
    runtime.module.threading.Thread.assert_not_called()


def test_working_spi_initializes_once_and_cleans_up(led_runtime):
    runtime = led_runtime.load()
    runtime.error = None
    runtime.led.initialize()
    runtime.led.initialize()
    assert runtime.imports == ['board', 'neopixel_spi']
    runtime.board.SPI.assert_called_once_with()
    runtime.pixels.NeoPixel_SPI.assert_called_once_with(
        runtime.board.SPI.return_value,
        1,
        auto_write=False,
        brightness=1.0,
    )
    assert runtime.led._initialized is True
    assert runtime.led._backend == 'spi'
    runtime.strip.__setitem__.assert_called_with(0, (0, 0, 0))
    runtime.module.threading.Thread.return_value.start.assert_called_once_with()
    runtime.led.cleanup()
    runtime.strip.deinit.assert_called_once_with()


@pytest.mark.parametrize(
    'pi5,attempts',
    [
        (True, ['board']),
        (False, []),
    ],
)
def test_auto_keeps_backend_order_and_fallback(led_runtime, monkeypatch, pi5, attempts):
    runtime = led_runtime.load()
    runtime.config.STATUS_LED_BACKEND = 'auto'
    monkeypatch.setattr(runtime.module, '_is_raspberry_pi_5', lambda: pi5)

    def pwm():
        runtime.led._strip = runtime.strip

    monkeypatch.setattr(runtime.led, '_initialize_pwm', pwm)
    runtime.led.initialize()
    assert runtime.imports == attempts
    assert runtime.led._initialized is True
    assert runtime.led._backend == 'pwm'
    runtime.led.cleanup()
