import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "bt_diagnostics_test",
    Path(__file__).parents[2] / "application/core/bluetooth/diagnostics.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_separates_queue_processing_and_pipe_delays():
    timing = module.UplinkTiming(0)
    timing.record(0.01, 0.03, 0.035, 0.045, 0.01)
    timing.record(0.02, 0.06, 0.07, 0.09, 0.01)
    result = timing.snapshot(0.1, dict(captured=2, sent=2, overflow=0, full=0), 0)
    for name, expected in {
        "capture_gap_ms": 10,
        "queue_ms": 40,
        "process_ms": 10,
        "write_ms": 20,
        "send_gap_ms": 45,
        "since_send_ms": 10,
        "audio_ms_per_s": 200,
        "sent_per_s": 20,
    }.items():
        assert result[name] == pytest.approx(expected)


def test_uses_actual_interval_and_never_resets_callback_counters():
    timing = module.UplinkTiming(0)
    counters = dict(captured=110, sent=100, overflow=2, full=3)
    first = timing.snapshot(2, counters, 7)
    assert first["sent_per_s"] == 50
    assert first["overflow"] == 2
    assert counters == dict(captured=110, sent=100, overflow=2, full=3)
    counters.update(captured=210, sent=200, overflow=3)
    second = timing.snapshot(3, counters, 2)
    assert second["sent_per_s"] == 100
    assert second["overflow"] == 1
    assert second["full"] == 0


def test_reports_current_stall_and_gap_across_report_boundaries():
    timing = module.UplinkTiming(0)
    timing.record(0, 0, 0, 0.01, 0.01)
    counts = dict(captured=1, sent=1, overflow=0, full=0)
    timing.snapshot(1, counts, 0)
    result = timing.snapshot(2, counts, 0)
    assert result["since_send_ms"] == pytest.approx(1990)
    timing.record(2, 2, 2, 2.01, 0.01)
    counts.update(captured=2, sent=2)
    result = timing.snapshot(3, counts, 0)
    assert result["send_gap_ms"] == pytest.approx(2000)
    assert "delivery to phone unmeasured" in module.format_timing(result, 100)
