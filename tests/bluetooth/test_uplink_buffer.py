"""Virtual clocks exercise the reservoir without a Pi or real-time sleeps."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest


spec = importlib.util.spec_from_file_location(
    'bt_uplink_buffer_test',
    Path(__file__).parents[2] / 'application/core/bluetooth/uplink_buffer.py',
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
UplinkBuffer = module.UplinkBuffer


def read(buffer):
    return np.frombuffer(buffer.read(), dtype=np.int16)


def test_prefill_preserves_first_words_and_normal_frame_boundaries():
    buffer = UplinkBuffer()
    original = np.arange(buffer.prefill_samples, dtype=np.int16)
    frames = buffer.prefill_samples // 480
    for offset in range(0, len(original) - 480, 480):
        buffer.push(original[offset : offset + 480])
        assert not read(buffer).any()
    buffer.push(original[-480:])
    actual = np.concatenate([read(buffer) for _ in range(frames)])
    np.testing.assert_array_equal(actual, original)
    metrics = buffer.snapshot()
    assert metrics['startup_silence_ms'] == (frames - 1) * 10
    assert metrics['buffer_underruns'] == 0


def simulate(seconds, ppm, burst_ms=40, output_frames=2, phase=0.02, jitter=0):
    buffer = UplinkBuffer()
    producer_period = burst_ms / 1000 / (1 + ppm / 1e6)
    capture_time, output_time = 0.0, phase
    capture_index = 0
    rng = np.random.default_rng(314159)
    samples = np.full(round(48 * burst_ms), 1000, dtype=np.int16)
    while output_time < seconds:
        # Model stdio/pipe consumption in bursts, not an ideal 10 ms Python
        # scheduler. The lower pipe limit admits two frames at a time; the
        # previous setting admitted four. Capture has an independent clock.
        while capture_time <= output_time:
            buffer.push(samples)
            capture_index += 1
            capture_time = capture_index * producer_period + rng.uniform(0, jitter)
        for _ in range(output_frames):
            assert len(buffer.read()) == 960
        output_time += output_frames * 0.01
    return buffer.snapshot()


@pytest.mark.parametrize('ppm', [-1000, -500, 0, 500, 1000])
def test_ten_minute_calls_with_burst_capture_and_independent_clocks(ppm):
    metrics = simulate(600, ppm)
    assert metrics['buffer_underruns'] == 0, metrics
    assert metrics['buffer_overruns'] == 0, metrics
    assert metrics['rebuffer_silence_ms'] == 0, metrics
    assert 0 < metrics['buffer_ms'] < 150, metrics
    assert abs(metrics['clock_adjust_ppm']) <= 2000
    if ppm:
        assert np.sign(metrics['clock_adjust_ppm']) == np.sign(ppm), metrics


def test_starvation_rebuffers_once_and_resumes_without_replaying_speech():
    buffer = UplinkBuffer()
    buffer.push(np.full(buffer.prefill_samples, 1000))
    for _ in range(buffer.prefill_samples // 480):
        assert (read(buffer) == 1000).all()
    fade = read(buffer)
    assert fade[0] == 1000 and fade[239] == 0 and not fade[240:].any()
    for _ in range(20):
        assert not read(buffer).any()
    buffer.push(np.full(buffer.prefill_samples, 2000))
    resumed = read(buffer)
    assert resumed[0] == 0 and (resumed[240:] == 2000).all()
    metrics = buffer.snapshot()
    assert metrics['buffer_underruns'] == 1
    assert metrics['rebuffer_silence_ms'] == 210
    assert not metrics['buffering']


def test_overrun_keeps_bounded_recent_audio_and_reports_discontinuity():
    buffer = UplinkBuffer()
    buffer.push(np.full(12000, 3000))
    metrics = buffer.snapshot()
    assert metrics['buffer_ms'] == 200
    assert metrics['buffer_overruns'] == 1
    assert metrics['buffer_dropped_ms'] == 50
    assert (read(buffer)[240:] == 3000).all()
    # Independent calls do not retain queued speech or servo state.
    other = UplinkBuffer()
    assert not read(other).any()
    assert other.snapshot()['clock_adjust_ppm'] == 0


def test_clock_correction_preserves_continuity_across_frame_boundaries():
    buffer = UplinkBuffer()
    original = np.arange(9600, dtype=np.int16)
    buffer.push(original)
    read(buffer)
    buffer._frames = 201
    buffer._level = buffer.target_samples + 960
    output = np.concatenate([read(buffer) for _ in range(10)])
    assert np.max(np.abs(np.diff(output.astype(int)))) <= 2
    assert np.all(np.diff(output.astype(int)) >= 0)


@pytest.mark.parametrize('offset', [-960, 960])
def test_fractional_resampling_tracks_a_continuous_signal(offset):
    buffer = UplinkBuffer()
    original = np.rint(
        12000 * np.sin(2 * np.pi * 1000 * np.arange(9600) / 48000)
    ).astype(np.int16)
    buffer.push(original)
    read(buffer)
    buffer._frames = 201
    buffer._level = buffer.target_samples + offset
    position = 480.0
    for _ in range(10):
        output = read(buffer).astype(float)
        step = 1 + buffer._adjust_ppm / 1e6
        expected = np.interp(
            position + np.arange(480) * step, np.arange(len(original)), original
        )
        assert np.max(np.abs(output - expected)) <= 0.501
        position += 480 * step


def test_temporary_capture_pause_is_visible_and_returns_to_normal():
    buffer = UplinkBuffer()
    for tick in range(1000):
        # Pause for 200 ms once, then resume continuous capture. Output keeps
        # delivering fixed-size frames and reports recovery instead of hanging.
        if not 300 <= tick < 320:
            buffer.push(np.full(480, 5000))
        read(buffer)
    metrics = buffer.snapshot()
    assert metrics['buffer_underruns'] == 1
    assert metrics['rebuffer_silence_ms'] > 0
    assert not metrics['buffering']
    assert metrics['buffer_overruns'] == 0


@pytest.mark.parametrize('phase', [0.0, 0.005, 0.01, 0.015])
@pytest.mark.parametrize('ppm', [-1000, 1000])
def test_lower_latency_with_capture_jitter_and_different_clock_phases(phase, ppm):
    metrics = simulate(180, ppm, phase=phase, jitter=0.002)
    assert metrics['buffer_underruns'] == 0, metrics
    assert metrics['buffer_overruns'] == 0, metrics


@pytest.mark.parametrize('ppm', [-1000, 0, 1000])
def test_previous_larger_pipe_profile_remains_covered(monkeypatch, ppm):
    monkeypatch.setattr(UplinkBuffer, 'target_samples', 3840)
    monkeypatch.setattr(UplinkBuffer, 'prefill_samples', 5760)
    metrics = simulate(180, ppm, output_frames=4)
    assert metrics['buffer_underruns'] == 0, metrics
    assert metrics['buffer_overruns'] == 0, metrics
