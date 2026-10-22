"""A Bluetooth audio session, and a standalone way to run one.

session() is what billy.service runs in-process. main() drives it from a shell
with its own speaker, microphone and echo canceller, which is how this path was
first brought up on hardware and still the way to exercise it on its own:

    sudo systemctl stop billy
    venv/bin/python -m core.bluetooth.runtime

billy.service has to be stopped for that, because two processes cannot hold the
same microphone and motor pins. There is no systemd unit for it any more.
"""

import contextlib
import json
import logging
import math
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from .diagnostics import UplinkTiming, format_timing
from .endpoints import ServiceEndpoints
from .transport import (
    PCMFrames,
    command,
    discover,
    pipewire_env,
    select_route,
)
from .uplink_buffer import UplinkBuffer


STATUS_PATH = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "billy-bluetooth.json"
)
STOP = threading.Event()
_status_lock = threading.Lock()
_status_write_failed = False
_status_logger = logging.getLogger(__name__)
# A freshly configured A2DP transport exposes its node before AVDTP Open/Start
# completes, so the first pw-cat attach can lose a harmless race.
MAX_CONSECUTIVE_RESTARTS = 5
# A late block is normal under load and is dropped; only a sustained fraction
# of loss means the microphone is actually broken.
MAX_MIC_DROP_FRACTION = 0.4
# Diagnostic: set BLUETOOTH_UPLINK_CAPTURE to a path prefix and each session
# writes what the microphone produced and what was actually sent to the phone,
# as raw mono s16. Lets an uplink complaint be checked against the bytes that
# left Billy instead of against what the far end's client did with them.
UPLINK_CAPTURE = os.environ.get("BLUETOOTH_UPLINK_CAPTURE")
# pw-dump is a subprocess plus a large JSON parse, and it holds the GIL long
# enough to starve the mic pump. pw-cat exiting already signals a vanished node,
# so the periodic sweep is only a slow safety net, not the primary detector.
ROUTE_CHECK_TICKS = 5
# Requested PipeWire latency, not a guaranteed prefilled jitter buffer.
# Pump timing measures delivery to pw-cat only, not downstream SCO underruns.
MIC_SEND_LATENCY_MS = 60
MIC_CAPTURE_TIMEOUT_SECONDS = 1.0
# Two 10 ms packets plus a little space for partial consumer reads. Keeping
# this below 4096 also reduces pw-cat/stdin read-ahead bursts. The PCM
# reservoir, rather than a large pipe, absorbs the 40 ms capture cadence.
MIC_PIPE_LIMIT_BYTES = 2048


class StreamRestart(RuntimeError):
    """The PipeWire transport dropped while the route was still present."""


class TransportGone(StreamRestart):
    """The microphone-side pw-cat stopped accepting audio."""


def status(state, **details):
    """Publish optional UI telemetry without stopping Billy or his audio.

    The user runtime directory is managed by the OS and may not exist yet on
    an unprovisioned unit. Do not create it ourselves or make startup depend on
    it. A later status update retries once the Bluetooth environment is ready.
    """
    global _status_write_failed
    with _status_lock:
        temporary = None
        try:
            # A unique sibling avoids collisions with another process and a
            # failed write must leave the previous complete snapshot intact.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=STATUS_PATH.parent,
                prefix="billy-bluetooth-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(
                    json.dumps({"state": state, "updated": time.time(), **details})
                )
            temporary.replace(STATUS_PATH)
        except OSError as exc:
            # Report once per outage, not once per audio timing update.
            if not _status_write_failed:
                _status_logger.warning(
                    "Bluetooth status could not be saved to %s: %s. Billy will continue.",
                    STATUS_PATH,
                    exc,
                )
            _status_write_failed = True
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
        if _status_write_failed:
            _status_logger.info("Bluetooth status reporting restored.")
        _status_write_failed = False
        return True


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def pipe_queued_bytes(fd):
    """Unread bytes, independent of the kernel's minimum pipe page size."""
    import array
    import fcntl
    import termios

    size = array.array("i", [0])
    fcntl.ioctl(fd, termios.FIONREAD, size, True)
    return size[0]


def write_pcm(process, data, stop=STOP):
    """Bound both pipe occupancy and waits; never accumulate stale speech."""
    view = memoryview(data)
    deadline = time.monotonic() + 0.1
    fd = process.stdin.fileno()
    while view:
        remaining = deadline - time.monotonic()
        if stop.is_set() or remaining <= 0 or process.poll() is not None:
            raise TransportGone("Bluetooth microphone transport stopped")
        chunk = view[:MIC_PIPE_LIMIT_BYTES]
        # F_SETPIPE_SZ alone cannot bound latency on kernels with 16 KiB pages.
        # Poll actual occupancy, not a nominal audio clock; leave a reservoir
        # in the pipe while the separate capture worker keeps processing.
        if pipe_queued_bytes(fd) + len(chunk) > MIC_PIPE_LIMIT_BYTES:
            stop.wait(min(0.002, remaining))
            continue
        try:
            # Linux can merge into a partially filled pipe page even when
            # select reports no free slots. Try before waiting for readiness.
            written = os.write(fd, chunk)
        except BlockingIOError:
            select.select([], [process.stdin], [], min(0.002, remaining))
            continue
        if written <= 0:
            raise TransportGone("Bluetooth microphone pipe accepted no data")
        view = view[written:]


def configure_uplink_pipe(process):
    """Validate occupancy reporting; use the same byte limit on 4/16 KiB pages."""
    import fcntl

    fd = process.stdin.fileno()
    os.set_blocking(fd, False)
    pipe_queued_bytes(fd)
    if hasattr(fcntl, "F_GETPIPE_SZ"):
        return fcntl.fcntl(fd, fcntl.F_GETPIPE_SZ)
    return None


def session(route, endpoints=None, stop=None):
    """Run one Bluetooth audio session over an already-discovered route.

    endpoints supplies the speaker, microphone and echo canceller; see
    endpoints.py for why this loop must not open them itself. Defaults to the
    standalone service's own hardware.

    stop ends the session. The standalone service has exactly one session at a
    time and sets the module-level STOP from its signal handlers, which is the
    default. Inside billy.service there is no signal to key off and a session
    must be endable on its own - the route vanished, a takeover finished - so
    the caller passes a fresh Event per session instead.
    """
    import numpy as np
    from scipy.signal import resample_poly

    from core.config import BLUETOOTH_CALL_AEC

    stop = STOP if stop is None else stop
    source, sink = route
    if endpoints is None:
        # On by default: a call needs cancellation. BLUETOOTH_CALL_AEC=false
        # makes process_capture() pass the microphone through untouched, which
        # is how to tell AEC artefacts apart from transport loss.
        endpoints = ServiceEndpoints(
            want_aec=bool(sink) and BLUETOOTH_CALL_AEC,
            want_microphone=bool(sink),
        )

    framer = PCMFrames()
    try:
        with contextlib.ExitStack() as stack:
            endpoints.open(stack)
            aec = endpoints.aec
            mic_queue = endpoints.blocks
            mic_stats = endpoints.stats
            mic_rate = endpoints.mic_rate
            blocks_per_second = endpoints.blocks_per_second
            incoming = subprocess.Popen(
                command(source, record=True),
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
                env=pipewire_env(),
            )
            stack.callback(stop_process, incoming)
            stack.callback(incoming.stdout.close)
            outgoing = None
            pipe_bytes = None
            if sink:
                outgoing = subprocess.Popen(
                    command(sink, record=False, latency_ms=MIC_SEND_LATENCY_MS),
                    stdin=subprocess.PIPE,
                    stderr=None,
                    bufsize=0,
                    env=pipewire_env(),
                )
                stack.callback(stop_process, outgoing)
                stack.callback(outgoing.stdin.close)
                pipe_bytes = configure_uplink_pipe(outgoing)
            captures = None
            if UPLINK_CAPTURE and outgoing:
                captures = (
                    stack.enter_context(open(f"{UPLINK_CAPTURE}.mic.s16", "wb")),
                    stack.enter_context(open(f"{UPLINK_CAPTURE}.sent.s16", "wb")),
                )
                print(
                    f"bluetooth mic: capturing to {UPLINK_CAPTURE}.mic.s16 "
                    f"({int(mic_rate)} Hz) and .sent.s16 (48000 Hz)",
                    file=sys.stderr,
                    flush=True,
                )
            mic_error = []
            session_stop = threading.Event()
            last_frame = [time.monotonic()]
            timing = UplinkTiming(time.monotonic())
            uplink = UplinkBuffer() if outgoing else None
            if outgoing:
                pipe_ms = f"{pipe_bytes / 96:.1f}" if pipe_bytes else "unknown"
                print(
                    "bluetooth mic: "
                    f"buffer_prefill_ms={UplinkBuffer.prefill_samples / 48:.0f} "
                    f"buffer_target_ms={UplinkBuffer.target_samples / 48:.0f} "
                    f"buffer_limit_ms={UplinkBuffer.capacity / 48:.0f} "
                    f"pipe_capacity_ms={pipe_ms} "
                    f"pipe_limit_ms={MIC_PIPE_LIMIT_BYTES / 96:.1f}",
                    file=sys.stderr,
                    flush=True,
                )

            def pump():
                """Mic -> AEC -> bounded reservoir, off the render loop.

                The render path blocks on the ALSA write. Draining the mic queue
                behind that block stalled it long enough to overflow and drop
                frames, which the far end hears as stutter.
                """
                try:
                    last_capture = time.monotonic()
                    while not (stop.is_set() or session_stop.is_set()):
                        try:
                            captured_at, samples = mic_queue.get(timeout=0.05)
                        except queue.Empty:
                            if (
                                time.monotonic() - last_capture
                                > MIC_CAPTURE_TIMEOUT_SECONDS
                            ):
                                raise RuntimeError(
                                    "Bluetooth microphone capture stalled"
                                )
                            continue
                        last_capture = time.monotonic()
                        started_at = time.monotonic()
                        cleaned = aec.process_capture(
                            samples,
                            mic_rate,
                            render_active=time.monotonic() - last_frame[0] < 0.15,
                        )
                        # active is false when cancellation was never wanted,
                        # in which case process_capture passes audio through
                        # and healthy has nothing to say about it.
                        if aec.active and not aec.healthy:
                            raise RuntimeError(
                                "AEC processing failed; microphone transport stopped"
                            )
                        if int(mic_rate) == 48000:
                            # Already at the transport rate; resample_poly(1, 1)
                            # burns a polyphase filter per 10 ms frame to do
                            # nothing.
                            converted = cleaned.astype(np.float32)
                        else:
                            divisor = math.gcd(int(mic_rate), 48000)
                            converted = resample_poly(
                                cleaned.astype(np.float32),
                                48000 // divisor,
                                int(mic_rate) // divisor,
                            )
                        pcm = np.clip(converted, -32768, 32767).astype(np.int16)
                        processed_at = time.monotonic()
                        timing.record_capture(captured_at, started_at, processed_at)
                        uplink.push(pcm)
                        if captures:
                            captures[0].write(samples.astype(np.int16).tobytes())
                except BaseException as exc:  # re-raised by the render loop
                    mic_error.append(exc)
                    session_stop.set()

            def send_microphone():
                """Demand-paced output, including silence while the buffer fills.

                Do not sleep on a nominal 10 ms wall clock: pw-cat's pipe
                backpressure follows the actual consumer clock. Capture/AEC
                can continue while a write waits for space.
                """
                try:
                    while not (stop.is_set() or session_stop.is_set()):
                        payload = uplink.read()
                        started_at = time.monotonic()
                        write_pcm(outgoing, payload, session_stop)
                        sent_at = time.monotonic()
                        mic_stats["sent"] += 1
                        timing.record_send(started_at, sent_at, len(payload) / 96000)
                        if captures:
                            # Exact PCM accepted by pw-cat, including startup/
                            # recovery silence and clock correction; not on air.
                            captures[1].write(payload)
                except BaseException as exc:
                    if not (stop.is_set() or session_stop.is_set()):
                        mic_error.append(exc)
                    session_stop.set()

            if outgoing:
                workers = []

                def stop_pump():
                    session_stop.set()
                    for worker in workers:
                        worker.join(timeout=1)
                    # Surface a stuck native capture/processing call on teardown.
                    if any(worker.is_alive() for worker in workers):
                        raise RuntimeError("Bluetooth microphone worker did not stop")

                # Registered before starting either worker so partial startup
                # also cancels/joins them, before closing pipes and endpoints.
                stack.callback(stop_pump)
                for target, name in (
                    (pump, "bluetooth-microphone"),
                    (send_microphone, "bluetooth-uplink"),
                ):
                    worker = threading.Thread(target=target, name=name, daemon=True)
                    worker.start()
                    workers.append(worker)
            status(
                "call_audio" if sink else "media_audio",
                aec=aec.active,
                source=source,
                mic_rate=mic_rate if sink else None,
                send_latency_ms=MIC_SEND_LATENCY_MS if sink else None,
                uplink_prefill_ms=UplinkBuffer.prefill_samples / 48 if sink else None,
                uplink_target_ms=UplinkBuffer.target_samples / 48 if sink else None,
                uplink_pipe_bytes=pipe_bytes,
                uplink_pipe_limit_bytes=MIC_PIPE_LIMIT_BYTES if sink else None,
            )
            next_check = time.monotonic() + 1
            warmed = False
            ticks = 0
            mouth_moving = False
            while not stop.is_set():
                if incoming.poll() is not None or (
                    outgoing and outgoing.poll() is not None
                ):
                    if select_route(discover()) != route:
                        return
                    raise StreamRestart(
                        "PipeWire audio stream ended; reconnect or retry"
                    )
                if select.select([incoming.stdout], [], [], 0.01)[0]:
                    chunk = os.read(incoming.stdout.fileno(), 1920)
                    if not chunk:
                        return
                    for frame in framer.feed(chunk):
                        mono = np.frombuffer(frame, dtype=np.int16)
                        endpoints.render(mono)
                        last_frame[0] = time.monotonic()
                        mouth_moving = True
                if mouth_moving and time.monotonic() - last_frame[0] > 0.15:
                    # Once, on the transition to silence. This used to run on
                    # every pass of the loop - about 100 times a second, each
                    # one four lgpio operations - and with no incoming audio
                    # (any voice memo, and the silent half of a call) it never
                    # stopped. The webconfig level test records cleanly because
                    # that process touches no GPIO at all.
                    endpoints.stop_mouth()
                    mouth_moving = False
                if mic_error:
                    failure = mic_error[0]
                    if isinstance(failure, TransportGone) and (
                        stop.is_set() or select_route(discover()) != route
                    ):
                        return
                    raise failure
                if time.monotonic() >= next_check:
                    now = time.monotonic()
                    if outgoing:
                        metrics = timing.snapshot(
                            now, dict(mic_stats), mic_queue.qsize()
                        )
                        metrics.update(uplink.snapshot())
                        dropped = metrics["overflow"] + metrics["full"]
                        anomalous = (
                            dropped
                            or metrics["buffer_underruns"]
                            or metrics["buffer_overruns"]
                            or metrics["sent_per_s"] < blocks_per_second * 0.9
                            or metrics["send_gap_ms"] > MIC_SEND_LATENCY_MS
                            or metrics["since_send_ms"] > MIC_SEND_LATENCY_MS
                            or metrics["queue_ms"] > MIC_SEND_LATENCY_MS
                        )
                        # Always report the first interval, then every five
                        # seconds while healthy; anomalies remain visible sooner.
                        if not warmed or anomalous or ticks % 5 == 0:
                            print(
                                format_timing(metrics, blocks_per_second),
                                file=sys.stderr,
                                flush=True,
                            )
                        status(
                            "call_audio",
                            aec=aec.active,
                            source=source,
                            mic_rate=mic_rate,
                            send_latency_ms=MIC_SEND_LATENCY_MS,
                            uplink_prefill_ms=UplinkBuffer.prefill_samples / 48,
                            uplink_target_ms=UplinkBuffer.target_samples / 48,
                            uplink_pipe_bytes=pipe_bytes,
                            uplink_pipe_limit_bytes=MIC_PIPE_LIMIT_BYTES,
                            timing=metrics,
                        )
                        # Overflows count events, not an exact lost-sample count.
                        # Preserve the safety cutoff, scaled to actual elapsed time.
                        if warmed and dropped > (
                            blocks_per_second
                            * metrics["interval_s"]
                            * MAX_MIC_DROP_FRACTION
                        ):
                            raise RuntimeError(
                                "Microphone cannot keep up; audio test stopped"
                            )
                    warmed = True
                    next_check = now + 1
                    ticks += 1
                    if ticks % ROUTE_CHECK_TICKS == 0 and (
                        select_route(discover()) != route
                    ):
                        return
    finally:
        endpoints.stop_motors()


def main():
    status("starting", aec=False)
    load_dotenv()
    from core import audio, movements

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    movements.start_motor_watchdog()
    try:
        audio.detect_devices()
        # Validate mono capture on the selected physical mic before any call.
        restarts = 0
        while not STOP.is_set():
            route = select_route(discover())
            if not route:
                restarts = 0
                status("waiting", aec=False)
                STOP.wait(0.5)
                continue
            # Require the route to survive one settle interval: a node that
            # disappears again was never a usable transport.
            STOP.wait(0.3)
            if select_route(discover()) != route:
                continue
            try:
                session(route)
                restarts = 0
            except StreamRestart:
                restarts += 1
                if restarts >= MAX_CONSECUTIVE_RESTARTS:
                    raise
                status("waiting", aec=False)
                STOP.wait(0.5)
    except Exception as exc:
        status("error", error=str(exc), aec=False)
        raise
    finally:
        movements.cleanup_gpio()
        if STOP.is_set():
            status("stopped", aec=False)


if __name__ == "__main__":
    main()
