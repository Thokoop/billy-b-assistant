"""ServiceEndpoints owns the hardware a Bluetooth session used to open itself.

Extracted rather than imported: core/__init__.py pulls in the whole package
(and its network dependencies) to reach one class, and the capture callback
under test needs neither sounddevice nor a real device.
"""

import ast
import contextlib
import queue
import sys
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import numpy as np


def load_endpoints():
    """Every class in the module, so subclasses find their base."""
    source = (Path(__file__).parents[2] / "core/bluetooth/endpoints.py").read_text()
    tree = ast.parse(source)
    classes = [item for item in tree.body if isinstance(item, ast.ClassDef)]
    namespace = {
        "queue": queue,
        "contextlib": contextlib,
        "MIC_QUEUE_BLOCKS": 3,
        "time": time,
        "sys": sys,
        "MIC_BLOCK_MS": 10,
        "MIC_OPEN_ATTEMPTS": 6,
        "MIC_OPEN_RETRY_SECONDS": 0.2,
    }
    exec(
        compile(
            ast.Module(body=classes, type_ignores=[]),
            "core/bluetooth/endpoints.py",
            "exec",
        ),
        namespace,
    )
    return namespace


ENDPOINTS = load_endpoints()
ServiceEndpoints = ENDPOINTS["ServiceEndpoints"]
SharedEndpoints = ENDPOINTS["SharedEndpoints"]
QUIET = SimpleNamespace(input_overflow=False)
OVERFLOWED = SimpleNamespace(input_overflow=True)


def block(value):
    return np.full((10, 1), value, dtype=np.int16)


class CaptureCallbackTests(unittest.TestCase):
    def setUp(self):
        self.endpoints = ServiceEndpoints(want_aec=False, want_microphone=True)

    def drain(self):
        out = []
        while True:
            try:
                out.append(int(self.endpoints.blocks.get_nowait()[1][0]))
            except queue.Empty:
                return out

    def test_blocks_reach_the_queue(self):
        self.endpoints._captured(block(7), 10, None, QUIET)
        self.assertEqual(self.drain(), [7])
        self.assertEqual(
            self.endpoints.stats, {"overflow": 0, "full": 0, "captured": 1, "sent": 0}
        )

    def test_portaudio_overflow_is_counted_but_the_block_still_arrives(self):
        """An overflow means audio was lost upstream, not that this block is bad."""
        self.endpoints._captured(block(3), 10, None, OVERFLOWED)
        self.assertEqual(self.drain(), [3])
        self.assertEqual(self.endpoints.stats["overflow"], 1)
        self.assertEqual(self.endpoints.stats["full"], 0)

    def test_a_full_queue_drops_the_oldest_block_not_the_newest(self):
        """The phone must hear live audio, not a queue grown into a delay."""
        for value in (1, 2, 3, 4, 5):
            self.endpoints._captured(block(value), 10, None, QUIET)
        # maxsize 3, so the two oldest are gone and the newest survived.
        self.assertEqual(self.drain(), [3, 4, 5])
        self.assertEqual(self.endpoints.stats["full"], 2)
        self.assertEqual(self.endpoints.stats["overflow"], 0)

    def test_the_copy_is_detached_from_portaudio_buffer_reuse(self):
        """PortAudio reuses its buffer; queuing a view would corrupt audio."""
        buffer = block(11)
        self.endpoints._captured(buffer, 10, None, QUIET)
        buffer[:] = 99
        self.assertEqual(self.drain(), [11])

    def test_microphone_can_be_declined_for_media_only_routes(self):
        media = ServiceEndpoints(want_aec=False, want_microphone=False)
        self.assertFalse(media._want_microphone)
        self.assertFalse(media._want_aec)


class SharedEndpointsTests(unittest.TestCase):
    """Inside billy.service the playback worker owns the only OutputStream."""

    def test_render_hands_the_frame_and_its_own_canceller_to_the_worker(self):
        posted = []
        shared = SharedEndpoints(want_aec=True, want_microphone=True)
        shared.aec = object()
        module = SimpleNamespace(playback_queue=SimpleNamespace(put=posted.append))
        with unittest.mock.patch.dict(
            "sys.modules", {"core": SimpleNamespace(audio=module)}
        ):
            shared.render(np.arange(4, dtype=np.int16))

        ((mode, payload, canceller),) = posted
        self.assertEqual(mode, "bluetooth")
        self.assertEqual(payload, np.arange(4, dtype=np.int16).tobytes())
        # Not core.audio's canceller: that one is built from AEC_ENABLED,
        # which is off by default, and a call needs cancellation anyway.
        self.assertIs(canceller, shared.aec)

    def test_both_placements_share_the_capture_path(self):
        self.assertIs(SharedEndpoints._captured, ServiceEndpoints._captured)


class CaptureSetupTests(unittest.TestCase):
    def setUp(self):
        self.audio = SimpleNamespace(
            MIC_RATE=24000,
            MIC_CHANNELS=2,
            MIC_DEVICE_INDEX=7,
            CHUNK_SIZE=960,
            reset_voice_band=unittest.mock.Mock(),
        )
        self.sd = SimpleNamespace(
            PortAudioError=RuntimeError,
            check_input_settings=unittest.mock.Mock(),
            InputStream=unittest.mock.MagicMock(),
        )
        self.mic = self.sd.InputStream.return_value.__enter__.return_value
        self.mic.latency = 0.025
        self.endpoints = ServiceEndpoints(want_aec=True, want_microphone=True)
        self.endpoints.aec = unittest.mock.Mock(active=True)

    def open_mic(self):
        with (
            unittest.mock.patch.dict(
                sys.modules,
                {"core": SimpleNamespace(audio=self.audio), "sounddevice": self.sd},
            ),
            contextlib.ExitStack() as stack,
        ):
            self.endpoints._open_microphone(stack)

    def test_call_uses_48k_10ms_and_preserves_ai_configuration(self):
        self.open_mic()
        options = self.sd.InputStream.call_args.kwargs
        self.assertEqual(options["samplerate"], 48000)
        self.assertEqual(options["blocksize"], 480)
        self.assertEqual(options["device"], 7)
        self.assertEqual(options["channels"], 2)
        self.assertEqual(self.audio.MIC_RATE, 24000)
        self.assertEqual(self.audio.CHUNK_SIZE, 960)
        self.endpoints.aec.set_capture_latency.assert_called_once_with(0.025)

    def test_unsupported_48k_retains_working_device_rate(self):
        self.sd.check_input_settings.side_effect = RuntimeError("Unsupported rate")
        self.open_mic()
        options = self.sd.InputStream.call_args.kwargs
        self.assertEqual(options["samplerate"], 24000)
        self.assertEqual(options["blocksize"], 240)
        self.assertEqual(self.endpoints.blocks_per_second, 100)
        self.assertEqual(self.audio.MIC_RATE, 24000)

    def test_busy_device_retries_same_48k_configuration(self):
        stream = self.sd.InputStream.return_value
        self.sd.InputStream.side_effect = [RuntimeError("Busy"), stream]
        with unittest.mock.patch.object(time, "sleep"):
            self.open_mic()
        self.assertEqual(self.sd.InputStream.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["samplerate"] == 48000
                for call in self.sd.InputStream.call_args_list
            )
        )

    def test_media_does_not_probe_or_open_the_microphone(self):
        self.endpoints._want_microphone = False
        self.open_mic()
        self.sd.check_input_settings.assert_not_called()
        self.sd.InputStream.assert_not_called()

    def test_call_keeps_aec_but_opts_out_of_ai_similarity(self):
        factory = unittest.mock.Mock()
        with unittest.mock.patch.dict(
            sys.modules,
            {
                "core": SimpleNamespace(audio=self.audio),
                "core.echo_canceller": SimpleNamespace(EchoCanceller=factory),
            },
        ):
            self.endpoints._prepare()
        factory.assert_called_once_with(True, track_playback_similarity=False)
        factory.return_value.initialize.assert_called_once()

    def test_aec_failure_does_not_silently_send_uncancelled_audio(self):
        factory = unittest.mock.Mock()
        factory.return_value.initialize.return_value = False
        with (
            unittest.mock.patch.dict(
                sys.modules,
                {
                    "core": SimpleNamespace(audio=self.audio),
                    "core.echo_canceller": SimpleNamespace(EchoCanceller=factory),
                },
            ),
            self.assertRaisesRegex(RuntimeError, "AEC3 could not start"),
        ):
            self.endpoints._prepare()


if __name__ == "__main__":
    unittest.main()
