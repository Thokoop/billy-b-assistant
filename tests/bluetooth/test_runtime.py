"""Run the session loop against local pipes and fake hardware, without a Pi."""

import importlib.util
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest


PIPE_READERS = {}


@pytest.fixture
def runtime(monkeypatch):
    parent = ModuleType("bt_runtime_test_core")
    parent.__path__ = [str(Path(__file__).parents[2] / "core")]
    monkeypatch.setitem(sys.modules, parent.__name__, parent)
    name = "bt_runtime_test_core.bluetooth"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).parents[2] / "core/bluetooth")]
    monkeypatch.setitem(sys.modules, name, package)
    for child in ("diagnostics", "endpoints", "transport", "runtime"):
        spec = importlib.util.spec_from_file_location(
            f"{name}.{child}", Path(package.__path__[0]) / f"{child}.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    monkeypatch.setitem(
        sys.modules, "core.config", SimpleNamespace(BLUETOOTH_CALL_AEC=True)
    )
    monkeypatch.setattr(module, "UPLINK_CAPTURE", None)
    if sys.platform != 'linux':
        # Linux FIONREAD supports a pipe's write fd. Darwin reports that end's
        # own (empty) read buffer instead. Query the corresponding real read
        # fd here to emulate the Linux boundary, preserving real occupancy.
        original = module.pipe_queued_bytes
        monkeypatch.setattr(
            module, 'pipe_queued_bytes', lambda fd: original(PIPE_READERS.get(fd, fd))
        )
    return module


class PipeProcess:
    def __init__(self, stack, recording):
        reader, writer = os.pipe()
        PIPE_READERS[writer] = reader
        stack.callback(PIPE_READERS.pop, writer, None)
        self.stdout = stack.enter_context(os.fdopen(reader, "rb", buffering=0))
        self.stdin = stack.enter_context(os.fdopen(writer, "wb", buffering=0))
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, timeout):
        return 0


@pytest.mark.parametrize("rate", [48000, 24000])
def test_pump_sends_cleaned_pcm_at_transport_rate(runtime, monkeypatch, rate):
    import contextlib

    import scipy.signal

    stop = threading.Event()
    samples = np.arange(rate // 100, dtype=np.int16)
    endpoints = Mock(
        mic_rate=rate,
        blocks=queue.Queue(),
        blocks_per_second=100,
        stats=dict(captured=1, sent=0, overflow=0, full=0),
    )
    endpoints.aec = Mock(active=True, healthy=True)
    endpoints.aec.process_capture.return_value = samples // 2
    for _ in range(12):
        endpoints.blocks.put((time.monotonic(), samples))
    payloads = []
    commands = []

    def write(process, payload, passed_stop):
        assert isinstance(passed_stop, threading.Event)
        if any(payload):
            payloads.append(payload)
            stop.set()
        else:
            time.sleep(0.001)

    if rate == 48000:
        monkeypatch.setattr(
            scipy.signal,
            "resample_poly",
            Mock(side_effect=AssertionError("48 kHz must not be resampled")),
        )
    with contextlib.ExitStack() as stack:

        def popen(command, **kwargs):
            commands.append(command)
            return PipeProcess(stack, "--record" in command)

        monkeypatch.setattr(runtime.subprocess, "Popen", popen)
        monkeypatch.setattr(runtime, "write_pcm", write)
        monkeypatch.setattr(runtime, "status", Mock())
        runtime.session(("phone-in", "phone-out"), endpoints, stop)
    assert len(payloads) == 1
    result = np.frombuffer(payloads[0], dtype=np.int16)
    assert len(result) == 480
    if rate == 48000:
        np.testing.assert_array_equal(result, samples // 2)
    assert endpoints.stats["sent"] >= 1
    assert "--latency=60ms" in commands[1]
    endpoints.stop_motors.assert_called_once()
    assert not any(
        t.name == "bluetooth-microphone" and t.is_alive() for t in threading.enumerate()
    )


def test_reports_timing_even_when_the_pump_stops_getting_samples(runtime, monkeypatch):
    import contextlib

    stop = threading.Event()
    endpoints = Mock(
        mic_rate=48000,
        blocks=queue.Queue(),
        blocks_per_second=100,
        stats=dict(captured=1, sent=0, overflow=0, full=0),
    )
    endpoints.aec = Mock(active=True, healthy=True)
    endpoints.aec.process_capture.return_value = np.zeros(480, dtype=np.int16)
    endpoints.blocks.put((time.monotonic(), np.zeros(480, dtype=np.int16)))
    reports = []
    monkeypatch.setattr(runtime, 'MIC_CAPTURE_TIMEOUT_SECONDS', 2)

    def status(state, **details):
        reports.append(details)
        if "timing" in details:
            stop.set()

    # A watchdog makes failure bounded if the reporting loop regresses.
    watchdog = threading.Timer(3, stop.set)
    watchdog.start()
    try:
        with contextlib.ExitStack() as stack:
            monkeypatch.setattr(
                runtime.subprocess,
                "Popen",
                lambda command, **kwargs: PipeProcess(stack, "--record" in command),
            )
            monkeypatch.setattr(runtime, "status", status)

            def write(process, payload, passed_stop):
                passed_stop.wait(0.01)

            monkeypatch.setattr(runtime, 'write_pcm', write)
            runtime.session(("phone-in", "phone-out"), endpoints, stop)
    finally:
        watchdog.cancel()
    metrics = next(r["timing"] for r in reports if "timing" in r)
    assert metrics["sent"] > 50
    assert metrics["startup_silence_ms"] > 500
    assert metrics["buffering"]
    assert metrics["write_ms"] >= 0
    endpoints.stop_motors.assert_called_once()


def test_media_never_starts_an_uplink(runtime, monkeypatch):
    import contextlib

    stop = threading.Event()
    endpoints = Mock(
        mic_rate=None,
        blocks=queue.Queue(),
        blocks_per_second=1,
        stats=dict(captured=0, sent=0, overflow=0, full=0),
    )
    endpoints.aec = Mock(active=False)
    processes = []

    def status(state, **details):
        assert state == "media_audio"
        assert details["mic_rate"] is None
        stop.set()

    with contextlib.ExitStack() as stack:

        def popen(command, **kwargs):
            processes.append(command)
            return PipeProcess(stack, True)

        monkeypatch.setattr(runtime.subprocess, "Popen", popen)
        monkeypatch.setattr(runtime, "status", status)
        runtime.session(("phone-in", None), endpoints, stop)
    assert len(processes) == 1
    assert "--record" in processes[0]
    endpoints.aec.process_capture.assert_not_called()


@pytest.mark.parametrize('call', [False, True])
def test_shared_output_failure_stops_transport_and_capture(
    runtime, audio_module, monkeypatch, call
):
    """A real failed playback queue must propagate through SharedEndpoints."""
    import contextlib

    endpoints_module = sys.modules['bt_runtime_test_core.bluetooth.endpoints']
    monkeypatch.setitem(sys.modules, 'core', SimpleNamespace(audio=audio_module))
    audio_module.playback_queue.fail(OSError('speaker unplugged'))
    shared = endpoints_module.SharedEndpoints(want_aec=call, want_microphone=call)
    shared.aec = Mock(active=False)
    endpoints = Mock(
        mic_rate=48000,
        blocks=queue.Queue(),
        blocks_per_second=100,
        stats=dict(captured=0, sent=0, overflow=0, full=0),
        aec=shared.aec,
    )
    endpoints.render.side_effect = shared.render
    stop = threading.Event()
    processes = []
    watchdog = threading.Timer(3, stop.set)
    watchdog.start()
    try:
        with contextlib.ExitStack() as stack:

            def popen(command, **kwargs):
                process = PipeProcess(stack, '--record' in command)
                if '--record' in command:
                    process.stdin.write(np.zeros(960, dtype=np.int16).tobytes())
                processes.append(process)
                return process

            monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
            monkeypatch.setattr(runtime, 'status', Mock())
            with pytest.raises(audio_module.PlaybackError, match='unplugged'):
                runtime.session(
                    ('phone-in', 'phone-out' if call else None), endpoints, stop
                )
    finally:
        watchdog.cancel()
    assert all(process.ended for process in processes)
    endpoints.stop_motors.assert_called_once()
    assert audio_module.playback_queue.unfinished_tasks == 0
    assert not any(
        t.name == 'bluetooth-microphone' and t.is_alive() for t in threading.enumerate()
    )


def assert_no_microphone_workers():
    assert not any(
        t.name in ('bluetooth-microphone', 'bluetooth-uplink') and t.is_alive()
        for t in threading.enumerate()
    )


def make_endpoints():
    endpoints = Mock(
        mic_rate=48000,
        blocks=queue.Queue(maxsize=8),
        blocks_per_second=100,
        stats=dict(captured=0, sent=0, overflow=0, full=0),
    )
    endpoints.aec = Mock(active=True, healthy=True)
    endpoints.aec.process_capture.side_effect = lambda samples, *a, **k: samples
    return endpoints


@pytest.mark.parametrize("buffered_reader", [False, True])
def test_real_pipe_backpressure_and_bursty_capture(
    runtime, monkeypatch, buffered_reader
):
    import contextlib
    import select

    stop = threading.Event()
    endpoints = make_endpoints()
    received = bytearray()
    reader_threads = []
    reports = []
    errors = []
    beginning = time.monotonic()

    def capture():
        next_burst = beginning
        try:
            while not stop.is_set():
                for _ in range(4):
                    endpoints.blocks.put_nowait((
                        time.monotonic(),
                        np.full(480, 1234, np.int16),
                    ))
                    endpoints.stats['captured'] += 1
                next_burst += 0.04
                stop.wait(max(0, next_burst - time.monotonic()))
        except BaseException as exc:
            errors.append(exc)
            stop.set()

    def consume(process):
        # Real pipe, with either direct reads or stdio-style 4096-byte
        # read-ahead, modelling pw-cat's buffered stdin at a 512/48000 quantum.
        next_tick = time.monotonic()
        try:
            with os.fdopen(
                os.dup(process.stdout.fileno()),
                'rb',
                buffering=4096 if buffered_reader else 0,
            ) as reader:
                while not stop.is_set():
                    if (
                        not buffered_reader
                        and not select.select([reader], [], [], 0.05)[0]
                    ):
                        continue
                    chunk = reader.read(1024)
                    if not chunk:
                        return
                    received.extend(chunk)
                    next_tick += len(chunk) / 96000
                    stop.wait(max(0, next_tick - time.monotonic()))
        except (OSError, ValueError) as exc:
            if not stop.is_set():
                errors.append(exc)
                stop.set()

    def status(state, **details):
        reports.append(details)
        if time.monotonic() - beginning >= 2:
            stop.set()

    producer = threading.Thread(target=capture)
    watchdog = threading.Timer(4, stop.set)
    producer.start()
    watchdog.start()
    try:
        with contextlib.ExitStack() as stack:

            def popen(command, **kwargs):
                process = PipeProcess(stack, '--record' in command)
                if '--playback' in command:
                    reader = threading.Thread(target=consume, args=(process,))
                    reader.start()
                    reader_threads.append(reader)
                return process

            monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
            monkeypatch.setattr(runtime, 'status', status)
            try:
                runtime.session(('phone-in', 'phone-out'), endpoints, stop)
            finally:
                stop.set()
                for reader in reader_threads:
                    reader.join(1)
    finally:
        stop.set()
        watchdog.cancel()
        producer.join(1)
    assert not errors
    pcm = np.frombuffer(received, dtype=np.int16)
    first_voice = np.flatnonzero(pcm)
    assert len(first_voice) > 24000, len(first_voice)
    assert (pcm[first_voice[0] :] == 1234).all()  # no holes or repeated fading
    metrics = [r['timing'] for r in reports if 'timing' in r]
    assert metrics
    assert all(m['buffer_underruns'] == m['buffer_overruns'] == 0 for m in metrics)
    assert_no_microphone_workers()


@pytest.mark.parametrize('failure', ['capture', 'aec', 'transport'])
def test_failure_stops_both_workers_and_releases_devices(runtime, monkeypatch, failure):
    import contextlib

    stop = threading.Event()
    endpoints = make_endpoints()
    monkeypatch.setattr(runtime, 'MIC_CAPTURE_TIMEOUT_SECONDS', 0.12)
    if failure == 'aec':
        endpoints.aec.process_capture.side_effect = RuntimeError('AEC engine failed')
        endpoints.blocks.put((time.monotonic(), np.ones(480, np.int16)))
    processes = []
    with contextlib.ExitStack() as stack:

        def popen(command, **kwargs):
            process = PipeProcess(stack, '--record' in command)
            processes.append(process)
            return process

        def write(process, payload, passed_stop):
            if failure == 'transport':
                raise runtime.TransportGone('pipe closed')
            passed_stop.wait(0.01)

        monkeypatch.setattr(runtime.subprocess, 'Popen', popen)
        monkeypatch.setattr(runtime, 'write_pcm', write)
        monkeypatch.setattr(runtime, 'status', Mock())
        monkeypatch.setattr(runtime, 'discover', lambda: [])
        monkeypatch.setattr(
            runtime, 'select_route', lambda _: ('phone-in', 'phone-out')
        )
        with pytest.raises((RuntimeError, runtime.TransportGone)):
            runtime.session(('phone-in', 'phone-out'), endpoints, stop)
    assert all(p.ended for p in processes)
    endpoints.stop_motors.assert_called_once()
    assert_no_microphone_workers()


def test_repeated_takeovers_stop_even_when_pipe_is_not_consumed(runtime, monkeypatch):
    import contextlib

    for _ in range(8):
        stop = threading.Event()
        endpoints = make_endpoints()
        with contextlib.ExitStack() as stack:
            monkeypatch.setattr(
                runtime.subprocess,
                'Popen',
                lambda command, **kwargs: PipeProcess(stack, '--record' in command),
            )
            monkeypatch.setattr(runtime, 'status', lambda *a, **k: stop.set())
            runtime.session(('phone-in', 'phone-out'), endpoints, stop)
        endpoints.stop_motors.assert_called_once()
        assert_no_microphone_workers()


def test_pipe_occupancy_is_measured_even_with_large_kernel_pages(runtime, monkeypatch):
    import fcntl

    monkeypatch.setattr(fcntl, 'F_GETPIPE_SZ', 1032, raising=False)
    monkeypatch.setattr(fcntl, 'fcntl', lambda *args: 16384)
    reader, writer = os.pipe()
    monkeypatch.setitem(PIPE_READERS, writer, reader)
    try:
        process = SimpleNamespace(stdin=SimpleNamespace(fileno=lambda: writer))
        assert runtime.configure_uplink_pipe(process) == 16384
        assert not os.get_blocking(writer)
        os.write(writer, b'a' * 960)
        assert runtime.pipe_queued_bytes(writer) == 960
        assert runtime.MIC_PIPE_LIMIT_BYTES == 2048
    finally:
        os.close(reader)
        os.close(writer)


def test_nonblocking_write_fills_pipe_before_waiting(runtime, monkeypatch):
    """A one-page Linux pipe may be mergeable even when poll says not writable."""
    process = Mock()
    process.poll.return_value = None
    process.stdin.fileno.return_value = 123
    monkeypatch.setattr(runtime, 'pipe_queued_bytes', lambda fd: 0)
    writes = Mock(side_effect=[3, 3])
    monkeypatch.setattr(runtime.os, 'write', writes)
    monkeypatch.setattr(
        runtime.select, 'select', Mock(side_effect=AssertionError('premature wait'))
    )
    runtime.write_pcm(process, b'abcdef', threading.Event())
    assert writes.call_count == 2
    assert bytes(writes.call_args_list[1].args[1]) == b'def'


def test_nonblocking_write_waits_only_when_full_and_obeys_stop(runtime, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.stdin.fileno.return_value = 123
    monkeypatch.setattr(runtime, 'pipe_queued_bytes', lambda fd: 0)
    stop = threading.Event()
    monkeypatch.setattr(runtime.os, 'write', Mock(side_effect=BlockingIOError()))

    def wait(*args):
        stop.set()
        return ([], [process.stdin], [])

    monkeypatch.setattr(runtime.select, 'select', wait)
    with pytest.raises(runtime.TransportGone, match='stopped'):
        runtime.write_pcm(process, b'abc', stop)


def test_zero_length_pipe_write_is_an_error(runtime, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.stdin.fileno.return_value = 123
    monkeypatch.setattr(runtime, 'pipe_queued_bytes', lambda fd: 0)
    monkeypatch.setattr(runtime.os, 'write', lambda *a: 0)
    with pytest.raises(runtime.TransportGone, match='accepted no data'):
        runtime.write_pcm(process, b'abc', threading.Event())


def test_pipe_high_water_waits_for_actual_consumption(runtime, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    process.stdin.fileno.return_value = 123
    stop = Mock()
    stop.is_set.return_value = False
    # A large-capacity pipe still has to wait when the configured byte limit would be exceeded.
    limit = runtime.MIC_PIPE_LIMIT_BYTES
    levels = iter([limit - 256, limit - 256, limit - 960])
    monkeypatch.setattr(runtime, 'pipe_queued_bytes', lambda fd: next(levels))
    writes = Mock(return_value=960)
    monkeypatch.setattr(runtime.os, 'write', writes)
    runtime.write_pcm(process, b'a' * 960, stop)
    assert stop.wait.call_count == 2
    writes.assert_called_once()


def test_full_pipe_stops_on_deadline_without_capture_blocking(runtime, monkeypatch):
    reader, writer = os.pipe()
    monkeypatch.setitem(PIPE_READERS, writer, reader)
    try:
        process = SimpleNamespace(
            stdin=SimpleNamespace(fileno=lambda: writer), poll=lambda: None
        )
        runtime.configure_uplink_pipe(process)
        os.write(writer, b'a' * 3840)
        started = time.monotonic()
        with pytest.raises(runtime.TransportGone):
            runtime.write_pcm(process, b'b' * 960, threading.Event())
        assert time.monotonic() - started < 0.3
        assert runtime.pipe_queued_bytes(writer) == 3840
    finally:
        os.close(reader)
        os.close(writer)


def test_status_missing_runtime_directory_is_nonfatal_and_recovers(
    runtime, monkeypatch, tmp_path, capsys
):
    import json

    target = tmp_path / 'user-runtime' / 'billy-bluetooth.json'
    monkeypatch.setattr(runtime, 'STATUS_PATH', target)
    assert runtime.status('idle') is False
    assert runtime.status('waiting') is False
    assert (
        not target.parent.exists()
    )  # Only the OS should create its runtime directory.
    assert capsys.readouterr().out.count('Billy will continue') == 1
    target.parent.mkdir()
    assert runtime.status('media_audio', source='bluez_input.phone') is True
    assert json.loads(target.read_text())['state'] == 'media_audio'
    target.unlink()
    target.parent.rmdir()
    assert runtime.status('idle') is False
    assert capsys.readouterr().out.count('Billy will continue') == 1


@pytest.mark.parametrize('failure', ['create', 'write', 'replace'])
def test_status_io_errors_preserve_last_snapshot_and_cleanup(
    runtime, monkeypatch, tmp_path, failure
):
    import contextlib
    import errno
    import json

    target = tmp_path / 'billy-bluetooth.json'
    monkeypatch.setattr(runtime, 'STATUS_PATH', target)
    runtime.status('idle')
    original = target.read_bytes()
    real_temporary = runtime.tempfile.NamedTemporaryFile

    @contextlib.contextmanager
    def broken_file(*args, **kwargs):
        if failure == 'create':
            raise PermissionError(errno.EACCES, 'Permission denied')
        with real_temporary(*args, **kwargs) as stream:

            def failed_write(text):
                stream.write(text[:5])
                raise OSError(errno.ENOSPC, 'No space left')

            yield SimpleNamespace(name=stream.name, write=failed_write)

    with monkeypatch.context() as patch:
        if failure == 'replace':

            def failed_replace(*args):
                raise PermissionError(errno.EACCES, 'Permission denied')

            patch.setattr(Path, 'replace', failed_replace)
        else:
            patch.setattr(runtime.tempfile, 'NamedTemporaryFile', broken_file)
        assert runtime.status('call_audio', aec=True) is False
        assert target.read_bytes() == original
        assert list(tmp_path.glob('*.tmp')) == []
    assert runtime.status('call_audio', aec=True) is True
    assert json.loads(target.read_text())['aec'] is True
    assert list(tmp_path.glob('*.tmp')) == []


def test_watcher_start_continues_without_runtime_directory(
    runtime, monkeypatch, tmp_path
):
    import ast

    namespace = 'missing_runtime_test'
    package = ModuleType(namespace)
    package.__path__ = []
    bluetooth = ModuleType(f'{namespace}.bluetooth')
    bluetooth.__path__ = []
    config = SimpleNamespace(BLUETOOTH_AUTO_SWITCH=True, TEXT_ONLY_MODE=False)
    autoconnect = SimpleNamespace(start=Mock(return_value=None))
    monkeypatch.setitem(sys.modules, namespace, package)
    monkeypatch.setitem(sys.modules, f'{namespace}.config', config)
    monkeypatch.setitem(sys.modules, f'{namespace}.bluetooth', bluetooth)
    monkeypatch.setitem(sys.modules, f'{namespace}.bluetooth.autoconnect', autoconnect)
    monkeypatch.setattr(
        runtime, 'STATUS_PATH', tmp_path / 'not-created' / 'billy-bluetooth.json'
    )
    source = (Path(__file__).parents[2] / 'core/bluetooth/watcher.py').read_text()
    tree = ast.parse(source)
    start = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'start'
    )
    thread = Mock()
    thread.is_alive.return_value = True
    thread_factory = Mock(return_value=thread)
    env = dict(
        __name__=f'{namespace}.bluetooth.watcher',
        __package__=f'{namespace}.bluetooth',
        _thread=None,
        _connect_thread=None,
        _stop=threading.Event(),
        runtime=runtime,
        mode=SimpleNamespace(IDLE='idle'),
        logger=Mock(),
        threading=SimpleNamespace(Thread=thread_factory),
        _run=Mock(),
    )
    exec(compile(ast.Module(body=[start], type_ignores=[]), 'watcher.py', 'exec'), env)
    env[
        'start'
    ]()  # This call used to abort main.ready() before readiness was reported.
    thread.start.assert_called_once()
    autoconnect.start.assert_called_once_with(env['_stop'])
    env['start']()
    thread_factory.assert_called_once()
