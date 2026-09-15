"""What a Bluetooth session needs from Billy's audio hardware.

The standalone billy-bluetooth service owns the speaker, the microphone and
the echo canceller outright, because systemd guarantees billy.service is not
running at the same time. Inside billy.service all three already exist and
are shared with the AI path, so a session there must be handed them rather
than open its own.

Keeping sounddevice and EchoCanceller out of the session loop is what makes
that swap possible without rewriting the loop. An implementation provides:

    aec                echo canceller; may be inactive (see .active)
    blocks             queue of mono int16 microphone blocks
    stats              {"overflow", "full", "sent"} counters, reset by the
                       caller once a second
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


# The pump drains continuously, so this only has to absorb scheduler jitter.
MIC_QUEUE_BLOCKS = 8
# Something on this Pi stalls capture occasionally, and each stall costs
# exactly one block, so audio lost is block duration x stall rate: measured
# 1.8% at 40 ms against 1.0% at 10 ms. 10 ms is also the AEC's own frame size.
MIC_BLOCK_MS = 10


class ServiceEndpoints:
    """Hardware owned outright by the standalone billy-bluetooth service."""

    def __init__(self, *, want_aec, want_microphone):
        self._want_aec = want_aec
        self._want_microphone = want_microphone
        self.aec = None
        self.blocks = queue.Queue(maxsize=MIC_QUEUE_BLOCKS)
        self.stats = {"overflow": 0, "full": 0, "sent": 0}
        self.mic_rate = None
        self.blocks_per_second = 1
        self._speaker = None

    def open(self, stack):
        import sounddevice as sd

        from core import audio
        from core.echo_canceller import EchoCanceller

        audio.reset_voice_band()
        self.aec = EchoCanceller(self._want_aec)
        if self._want_aec and not self.aec.initialize():
            raise RuntimeError("Calling unavailable: WebRTC AEC3 could not start")

        self.mic_rate = audio.MIC_RATE
        block_frames = max(1, int(audio.MIC_RATE * MIC_BLOCK_MS / 1000))
        self.blocks_per_second = max(1, round(float(audio.MIC_RATE) / block_frames))

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

        if self._want_microphone:
            # Channel count follows the device, as core.mic and the webconfig
            # level test do. Block size deliberately does not: see MIC_BLOCK_MS.
            microphone = stack.enter_context(
                sd.InputStream(
                    device=audio.MIC_DEVICE_INDEX,
                    samplerate=audio.MIC_RATE,
                    channels=audio.MIC_CHANNELS,
                    dtype="int16",
                    blocksize=block_frames,
                    callback=self._captured,
                )
            )
            self.aec.set_capture_latency(microphone.latency)

    def _captured(self, data, frames, timing, flags):
        # Drop the oldest block rather than refuse the newest: the phone should
        # hear live audio, not a queue that has grown into a delay. overflow is
        # audio PortAudio lost before we saw it; full is our pump falling
        # behind. They need opposite fixes, so count them apart.
        if flags.input_overflow:
            self.stats["overflow"] += 1
        try:
            self.blocks.put_nowait(data[:, 0].copy())
        except queue.Full:
            self.stats["full"] += 1
            with contextlib.suppress(queue.Empty):
                self.blocks.get_nowait()
            with contextlib.suppress(queue.Full):
                self.blocks.put_nowait(data[:, 0].copy())

    def render(self, mono):
        from core import audio

        audio.render_external_pcm(self._speaker, mono, self.aec)

    def stop_mouth(self):
        from core import movements

        movements.stop_mouth()

    def stop_motors(self):
        from core import movements

        movements.stop_all_motors()
