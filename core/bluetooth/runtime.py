"""Isolated phase 1/2 audio service. AI service must be stopped by systemd first."""

import contextlib
import json
import math
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from .transport import PCMFrames, command, discover, select_route


STATUS_PATH = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    / "billy-bluetooth.json"
)
STOP = threading.Event()
# A freshly configured A2DP transport exposes its node before AVDTP Open/Start
# completes, so the first pw-cat attach can lose a harmless race.
MAX_CONSECUTIVE_RESTARTS = 5
# A late block is normal under load and is dropped; only a sustained fraction
# of loss means the microphone is actually broken.
MAX_MIC_DROP_FRACTION = 0.4
# The pump drains continuously, so this only has to absorb scheduler jitter.
MIC_QUEUE_BLOCKS = 8
# Something on this Pi stalls capture once or twice a second regardless of how
# the stream is opened, and each stall costs exactly one block. Audio lost is
# therefore block duration x stall rate, so keep blocks short: measured 1.8% at
# 40 ms versus 1.0% at 10 ms. 10 ms is also the AEC's own frame size.
MIC_BLOCK_MS = 10
# Diagnostic: set BLUETOOTH_UPLINK_CAPTURE to a path prefix and each session
# writes what the microphone produced and what was actually sent to the phone,
# as raw mono s16. Lets an uplink complaint be checked against the bytes that
# left Billy instead of against what the far end's client did with them.
UPLINK_CAPTURE = os.environ.get("BLUETOOTH_UPLINK_CAPTURE")
# pw-dump is a subprocess plus a large JSON parse, and it holds the GIL long
# enough to starve the mic pump. pw-cat exiting already signals a vanished node,
# so the periodic sweep is only a slow safety net, not the primary detector.
ROUTE_CHECK_TICKS = 5
# The outgoing pw-cat drains at its node latency. At 20 ms any Python stall
# longer than that starves it and PipeWire sends silence down the SCO link,
# which the far end hears as chop. Nothing is lost on our side, so the cure is
# a deeper buffer there, not more buffering here.
MIC_SEND_LATENCY_MS = 60


class StreamRestart(RuntimeError):
    """The PipeWire transport dropped while the route was still present."""


class TransportGone(StreamRestart):
    """The microphone-side pw-cat stopped accepting audio."""


def status(state, **details):
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"state": state, "updated": time.time(), **details})
    )
    temporary.replace(STATUS_PATH)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def write_pcm(process, data):
    """Never accumulate delayed microphone audio if Bluetooth stops consuming."""
    view = memoryview(data)
    deadline = time.monotonic() + 0.1
    while view:
        remaining = deadline - time.monotonic()
        if STOP.is_set() or remaining <= 0 or process.poll() is not None:
            raise TransportGone("Bluetooth microphone transport stopped")
        if not select.select([], [process.stdin], [], remaining)[1]:
            raise TransportGone("Bluetooth microphone transport stalled")
        try:
            written = os.write(process.stdin.fileno(), view)
        except BlockingIOError:
            continue
        view = view[written:]


def session(route):
    import numpy as np
    import sounddevice as sd
    from scipy.signal import resample_poly

    from core import audio, movements
    from core.config import BLUETOOTH_CALL_AEC
    from core.echo_canceller import EchoCanceller

    source, sink = route
    audio.reset_voice_band()
    # On by default: a call needs cancellation. Setting BLUETOOTH_CALL_AEC=false
    # makes process_capture() pass the microphone through untouched, which is
    # how to tell AEC artefacts apart from transport loss.
    use_aec = bool(sink) and BLUETOOTH_CALL_AEC
    aec = EchoCanceller(use_aec)
    if use_aec and not aec.initialize():
        raise RuntimeError("Calling unavailable: WebRTC AEC3 could not start")
    # Channel count follows the device, as core.mic and the webconfig level
    # test do; forcing mono pushed ALSA through a conversion layer neither of
    # those touches. Block size deliberately does NOT follow them: see
    # MIC_BLOCK_MS above.
    block_frames = max(1, int(audio.MIC_RATE * MIC_BLOCK_MS / 1000))
    blocks_per_second = max(1, round(float(audio.MIC_RATE) / block_frames))
    mic_queue = queue.Queue(maxsize=MIC_QUEUE_BLOCKS)
    # overflow: lost by PortAudio before we saw it (capture side too slow).
    # full: our pump could not keep up. They need opposite fixes, so count
    # them apart rather than as one "drops" number.
    mic_stats = {"overflow": 0, "full": 0, "sent": 0}

    def capture(data, frames, timing, flags):
        # Drop the oldest frame rather than refuse the newest: the phone should
        # hear live audio, not a queue that has grown into a delay.
        if flags.input_overflow:
            mic_stats["overflow"] += 1
        try:
            mic_queue.put_nowait(data[:, 0].copy())
        except queue.Full:
            mic_stats["full"] += 1
            with contextlib.suppress(queue.Empty):
                mic_queue.get_nowait()
            with contextlib.suppress(queue.Full):
                mic_queue.put_nowait(data[:, 0].copy())

    framer = PCMFrames()
    try:
        with contextlib.ExitStack() as stack:
            incoming = subprocess.Popen(
                command(source, record=True),
                stdout=subprocess.PIPE,
                stderr=None,
                bufsize=0,
            )
            stack.callback(stop_process, incoming)
            stack.callback(incoming.stdout.close)
            outgoing = None
            if sink:
                outgoing = subprocess.Popen(
                    command(sink, record=False, latency_ms=MIC_SEND_LATENCY_MS),
                    stdin=subprocess.PIPE,
                    stderr=None,
                    bufsize=0,
                )
                stack.callback(stop_process, outgoing)
                stack.callback(outgoing.stdin.close)
                os.set_blocking(outgoing.stdin.fileno(), False)
            speaker = stack.enter_context(
                sd.OutputStream(
                    device=audio.OUTPUT_DEVICE_INDEX,
                    samplerate=48000,
                    channels=2,
                    dtype="int16",
                    latency="low",
                )
            )
            aec.set_render_latency(speaker.latency)
            if sink:
                mic = stack.enter_context(
                    sd.InputStream(
                        device=audio.MIC_DEVICE_INDEX,
                        samplerate=audio.MIC_RATE,
                        channels=audio.MIC_CHANNELS,
                        dtype="int16",
                        blocksize=block_frames,
                        callback=capture,
                    )
                )
                aec.set_capture_latency(mic.latency)
            captures = None
            if UPLINK_CAPTURE and outgoing:
                captures = (
                    stack.enter_context(open(f"{UPLINK_CAPTURE}.mic.s16", "wb")),
                    stack.enter_context(open(f"{UPLINK_CAPTURE}.sent.s16", "wb")),
                )
                print(
                    f"bluetooth mic: capturing to {UPLINK_CAPTURE}.mic.s16 "
                    f"({int(audio.MIC_RATE)} Hz) and .sent.s16 (48000 Hz)",
                    file=sys.stderr,
                    flush=True,
                )
            mic_error = []
            session_stop = threading.Event()
            last_frame = [time.monotonic()]

            def pump():
                """Mic -> AEC -> phone, off the render loop.

                The render path blocks on the ALSA write. Draining the mic queue
                behind that block stalled it long enough to overflow and drop
                frames, which the far end hears as stutter.
                """
                try:
                    while not (STOP.is_set() or session_stop.is_set()):
                        try:
                            samples = mic_queue.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        cleaned = aec.process_capture(
                            samples,
                            audio.MIC_RATE,
                            render_active=time.monotonic() - last_frame[0] < 0.15,
                        )
                        if use_aec and not aec.healthy:
                            raise RuntimeError(
                                "AEC processing failed; microphone transport stopped"
                            )
                        if int(audio.MIC_RATE) == 48000:
                            # Already at the transport rate; resample_poly(1, 1)
                            # burns a polyphase filter per 10 ms frame to do
                            # nothing.
                            converted = cleaned.astype(np.float32)
                        else:
                            divisor = math.gcd(int(audio.MIC_RATE), 48000)
                            converted = resample_poly(
                                cleaned.astype(np.float32),
                                48000 // divisor,
                                int(audio.MIC_RATE) // divisor,
                            )
                        payload = (
                            np
                            .clip(converted, -32768, 32767)
                            .astype(np.int16)
                            .tobytes()
                        )
                        if captures:
                            # Before AEC and after, so an artefact can be
                            # attributed to the microphone or to processing.
                            captures[0].write(samples.astype(np.int16).tobytes())
                            captures[1].write(payload)
                        write_pcm(outgoing, payload)
                        mic_stats["sent"] += 1
                except BaseException as exc:  # re-raised by the render loop
                    mic_error.append(exc)

            if outgoing:
                pump_thread = threading.Thread(
                    target=pump, name="bluetooth-microphone", daemon=True
                )
                pump_thread.start()

                def stop_pump():
                    session_stop.set()
                    pump_thread.join(timeout=1)

                # Registered last so it runs first: the pump must be gone before
                # the pipe it writes to is closed.
                stack.callback(stop_pump)
            status(
                "call_audio" if sink else "media_audio",
                aec=aec.active,
                source=source,
            )
            next_check = time.monotonic() + 1
            warmed = False
            ticks = 0
            mouth_moving = False
            while not STOP.is_set():
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
                        audio.render_external_pcm(speaker, mono, aec)
                        last_frame[0] = time.monotonic()
                        mouth_moving = True
                if mouth_moving and time.monotonic() - last_frame[0] > 0.15:
                    # Once, on the transition to silence. This used to run on
                    # every pass of the loop - about 100 times a second, each
                    # one four lgpio operations - and with no incoming audio
                    # (any voice memo, and the silent half of a call) it never
                    # stopped. The webconfig level test records cleanly because
                    # that process touches no GPIO at all.
                    movements.stop_mouth()
                    mouth_moving = False
                if mic_error:
                    failure = mic_error[0]
                    if isinstance(failure, TransportGone) and (
                        STOP.is_set() or select_route(discover()) != route
                    ):
                        return
                    raise failure
                if time.monotonic() >= next_check:
                    dropped = mic_stats["overflow"] + mic_stats["full"]
                    if outgoing and (
                        dropped or mic_stats["sent"] < blocks_per_second * 0.9
                    ):
                        # Anything below the nominal block rate, or any loss, is
                        # what the far end hears as chop.
                        print(
                            f"bluetooth mic: sent={mic_stats['sent']}/"
                            f"{blocks_per_second} per s "
                            f"overflow={mic_stats['overflow']} "
                            f"full={mic_stats['full']} "
                            f"queued={mic_queue.qsize()}",
                            file=sys.stderr,
                            flush=True,
                        )
                    # Skip the first tick: AEC and stream start-up cost blocks.
                    if warmed and dropped > blocks_per_second * MAX_MIC_DROP_FRACTION:
                        raise RuntimeError(
                            "Microphone cannot keep up; audio test stopped"
                        )
                    warmed = True
                    mic_stats.update(overflow=0, full=0, sent=0)
                    next_check = time.monotonic() + 1
                    ticks += 1
                    if ticks % ROUTE_CHECK_TICKS == 0 and (
                        select_route(discover()) != route
                    ):
                        return
    finally:
        movements.stop_all_motors()


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
