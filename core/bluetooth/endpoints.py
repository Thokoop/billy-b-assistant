"""What a Bluetooth session needs from Billy's audio hardware.

Run standalone from a shell (see runtime.main()), a session owns the speaker,
the microphone and the echo canceller outright, because billy.service has to
be stopped for it. Inside billy.service all three already exist and are
shared with the AI path, so a session there must be handed them rather than
open its own.

Keeping sounddevice and EchoCanceller out of the session loop is what makes
that swap possible without rewriting the loop. An implementation provides:

    aec                echo canceller; may be inactive (see .active)
    blocks             queue of (callback monotonic time, mono int16 samples)
    stats              cumulative {"overflow", "full", "captured", "sent"}
                       counters; snapshot differences avoid reset races
    mic_rate           microphone sample rate
    blocks_per_second  nominal block rate, for the loss budget
    open(stack)        acquire resources, registering teardown on the stack
    render(mono)       play one 48 kHz mono frame: speaker, AEC reference
                       and mouth articulation
    stop_mouth()       brake the mouth on the transition to silence
    stop_motors()      final teardown
"""

import contextlib
import queue
import sys
import time


# The pump drains continuously, so this only has to absorb scheduler jitter.
MIC_QUEUE_BLOCKS = 8
# Something on this Pi stalls capture occasionally, and each stall costs
# exactly one block, so audio lost is block duration x stall rate: measured
# 1.8% at 40 ms against 1.0% at 10 ms. 10 ms is also the AEC's own frame size.
MIC_BLOCK_MS = 10
# Inside billy.service a session opens the microphone moments after the AI
# session was stopped, and ALSA does not always have the capture handle back
# yet - core/button.py sleeps 0.3 s before reopening for the same reason.
# Failing the session over a handle that is about to become free would drop a
# call. The standalone service shares this path and never sees the error.
MIC_OPEN_ATTEMPTS = 6
MIC_OPEN_RETRY_SECONDS = 0.2


class _Endpoints:
    """Microphone and echo canceller handling common to both placements.

    The microphone is safe to open in either: a standalone run requires
    billy.service to be stopped, and inside billy.service a call stops the
    AI session first, which closes it. The echo canceller is always
    this session's own - core.audio's is built from AEC_ENABLED, which
    governs the AI path and is off by default, while a call needs
    cancellation regardless of how the assistant is configured.
    """

    def __init__(self, *, want_aec, want_microphone):
        self._want_aec = want_aec
        self._want_microphone = want_microphone
        self.aec = None
        self.blocks = queue.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self.stats = {"overflow": 0, "full": 0, "captured": 0, "sent": 0}
        self.mic_rate = None
        self.blocks_per_second = 1
        self._speaker = None

    def _prepare(self):
        from core import audio
        from core.echo_canceller import EchoCanceller

        audio.reset_voice_band()
        self.aec = EchoCanceller(self._want_aec, track_playback_similarity=False)
        if self._want_aec and not self.aec.initialize():
            raise RuntimeError("Calling unavailable: WebRTC AEC3 could not start")
        self.mic_rate = audio.MIC_RATE

    def _open_microphone(self, stack):
        if not self._want_microphone:
            return
        import sounddevice as sd

        from core import audio

        self._select_capture_rate(sd, audio)
        # Channel count follows the device, as core.mic and the webconfig
        # level test do. Block size deliberately does not: see MIC_BLOCK_MS.
        microphone = self._open_capture_stream(stack, sd, audio)
        self.aec.set_capture_latency(microphone.latency)
        print(
            f"bluetooth mic: rate={self.mic_rate} Hz "
            f"block={self._block_frames} frames "
            f"capture_latency_ms={microphone.latency * 1000:.1f} "
            f"aec={self.aec.active} similarity=False",
            file=sys.stderr,
            flush=True,
        )

    def _select_capture_rate(self, sd, audio):
        # Session-local: never change MIC_RATE/CHUNK_SIZE used by Realtime AI.
        try:
            sd.check_input_settings(
                device=audio.MIC_DEVICE_INDEX,
                samplerate=48000,
                channels=audio.MIC_CHANNELS,
                dtype="int16",
            )
        except sd.PortAudioError as exc:
            self.mic_rate = audio.MIC_RATE
            print(
                f"bluetooth mic: 48000 Hz unavailable ({exc}); "
                f"falling back to {self.mic_rate} Hz with resampling",
                file=sys.stderr,
                flush=True,
            )
        else:
            self.mic_rate = 48000
        self._block_frames = max(1, int(self.mic_rate * MIC_BLOCK_MS / 1000))
        self.blocks_per_second = max(
            1, round(float(self.mic_rate) / self._block_frames)
        )

    def _open_capture_stream(self, stack, sd, audio):
        for attempt in range(MIC_OPEN_ATTEMPTS):
            try:
                return stack.enter_context(
                    sd.InputStream(
                        device=audio.MIC_DEVICE_INDEX,
                        samplerate=self.mic_rate,
                        channels=audio.MIC_CHANNELS,
                        dtype="int16",
                        blocksize=self._block_frames,
                        callback=self._captured,
                    )
                )
            except Exception as exc:
                if attempt == MIC_OPEN_ATTEMPTS - 1:
                    raise
                print(
                    f"bluetooth mic: capture device busy ({exc}); "
                    f"retry {attempt + 1}/{MIC_OPEN_ATTEMPTS - 1}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(MIC_OPEN_RETRY_SECONDS)
        raise RuntimeError("unreachable")

    def _captured(self, data, frames, timing, flags):
        # Drop the oldest block rather than refuse the newest: the phone should
        # hear live audio, not a queue that has grown into a delay. overflow is
        # audio PortAudio lost before we saw it; full is our pump falling
        # behind. They need opposite fixes, so count them apart.
        captured_at = time.monotonic()
        self.stats["captured"] += 1
        if flags.input_overflow:
            self.stats["overflow"] += 1
        block = (captured_at, data[:, 0].copy())
        try:
            self.blocks.put_nowait(block)
        except queue.Full:
            self.stats["full"] += 1
            with contextlib.suppress(queue.Empty):
                self.blocks.get_nowait()
            with contextlib.suppress(queue.Full):
                self.blocks.put_nowait(block)

    def stop_mouth(self):
        from core import movements

        movements.stop_mouth()

    def stop_motors(self):
        from core import movements

        movements.stop_all_motors()


class ServiceEndpoints(_Endpoints):
    """Hardware owned outright by a session running on its own.

    billy.service is stopped, so nothing else holds the speaker and this opens
    it directly. See runtime.main().
    """

    def open(self, stack):
        import sounddevice as sd

        from core import audio

        self._prepare()
        self._speaker = stack.enter_context(
            sd.OutputStream(
                device=audio.OUTPUT_DEVICE_INDEX,
                samplerate=48000,
                channels=2,
                dtype="int16",
                latency="low",
            )
        )
        self.aec.set_render_latency(self._speaker.latency)
        self._open_microphone(stack)

    def render(self, mono):
        from core import audio

        audio.render_external_pcm(self._speaker, mono, self.aec)


class SharedEndpoints(_Endpoints):
    """Endpoints for a session running inside billy.service.

    The playback worker holds the process's only OutputStream for its whole
    life, so rendering goes through its queue rather than opening a second
    one. The queue item carries this session's canceller, so the worker feeds
    the right render reference and drives mouth articulation exactly as it
    does for songs and speech.
    """

    def open(self, stack):
        from core import audio
        from core.config import CHUNK_MS

        self._prepare()
        audio.ensure_playback_worker_started(CHUNK_MS)
        # The worker publishes the real figure once its stream is open; before
        # that zero is the honest answer rather than a guess.
        self.aec.set_render_latency(
            getattr(audio, "_playback_output_latency_seconds", 0.0)
        )
        self._open_microphone(stack)

    def render(self, mono):
        from core import audio

        # The incoming transport paces this at real time, so the queue stays
        # shallow; it is a handoff to the speaker's owner, not a buffer.
        audio.playback_queue.put(("bluetooth", mono.tobytes(), self.aec))
