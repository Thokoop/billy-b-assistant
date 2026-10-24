"""Exercise the real shared AEC wrapper with a deterministic DSP substitute."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


@pytest.fixture
def echo_module(monkeypatch):
    package = ModuleType("echo_test_package")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(
        sys.modules, "echo_test_package.logger", SimpleNamespace(logger=Mock())
    )
    spec = importlib.util.spec_from_file_location(
        "echo_test_package.echo_canceller",
        Path(__file__).parents[1] / "application/core/echo_canceller.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Processor:
    def __init__(self, sample_rate):
        assert sample_rate == 48000
        self.render = []
        self.capture = []
        self.delays = []

    def process_render(self, frame):
        self.render.append(frame.copy())

    def process_capture(self, frame):
        self.capture.append(frame.copy())
        return (frame // 2).astype(np.int16)

    def voice_detected(self):
        return True

    def set_stream_delay(self, delay):
        self.delays.append(delay)


def test_bluetooth_still_cancels_echo_but_skips_similarity(echo_module):
    aec = echo_module.EchoCanceller(True, Processor, track_playback_similarity=False)
    aec._playback_similarity_locked = Mock(side_effect=AssertionError("Unused AI work"))
    samples = np.arange(480, dtype=np.int16)
    aec.feed_render(samples, 48000)
    result = aec.process_capture(samples, 48000)
    np.testing.assert_array_equal(result, samples // 2)
    np.testing.assert_array_equal(aec._processor.render[0], samples)
    assert len(aec._processor.capture) == 1
    assert aec.healthy
    assert aec.voice_detected is True
    assert aec.playback_similarity is None
    assert not aec._render_history
    aec._playback_similarity_locked.assert_not_called()


def test_default_ai_keeps_similarity_vad_generation_and_latency(echo_module):
    aec = echo_module.EchoCanceller(True, Processor)
    generation = aec.begin_render_generation()
    samples = np.arange(1920, dtype=np.int16)
    aec.feed_render(samples, 48000, generation=generation)
    aec.set_capture_latency(0.02)
    aec.set_render_latency(0.03)
    result = aec.process_capture(samples, 48000)
    np.testing.assert_array_equal(result, samples // 2)
    assert aec.render_ready
    assert len(aec._render_history) == 4
    assert aec.playback_similarity is not None
    assert aec.voice_detected is True
    assert aec._processor.delays[-1] == 50
    aec.invalidate_render_generation()
    aec.feed_render(samples, 48000, generation=generation)
    assert not aec.render_ready
    assert aec.playback_similarity is None


def test_ai_24k_capture_stays_24k_with_original_chunk_length(echo_module):
    aec = echo_module.EchoCanceller(True, Processor)
    samples = np.arange(960, dtype=np.int16)  # Existing 40 ms Realtime mic path.
    result = aec.process_capture(samples, 24000, render_active=False)
    assert result.dtype == np.int16
    assert result.shape == samples.shape
    assert len(aec._processor.capture) == 4
    assert len(aec._processor.render) == 4
    assert aec.voice_detected is True
    assert aec.healthy


def test_disabled_aec_is_still_pass_through(echo_module):
    factory = Mock(side_effect=AssertionError("AEC disabled"))
    aec = echo_module.EchoCanceller(False, factory)
    samples = np.arange(960, dtype=np.int16)
    np.testing.assert_array_equal(aec.process_capture(samples, 24000), samples)
    factory.assert_not_called()
