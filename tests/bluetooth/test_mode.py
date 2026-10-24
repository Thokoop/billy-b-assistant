"""The flag that tells every audio entry point a Bluetooth session owns the hardware.

Loaded by path rather than imported: core/__init__.py pulls in the whole
package (and its network dependencies) to reach a module that deliberately
depends on nothing.
"""

import importlib.util
import threading
import unittest
from pathlib import Path


def load_mode():
    path = Path(__file__).parents[2] / "application/core/bluetooth/mode.py"
    spec = importlib.util.spec_from_file_location("billy_bluetooth_mode", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BluetoothModeTests(unittest.TestCase):
    def setUp(self):
        self.mode = load_mode()

    def test_idle_until_claimed(self):
        self.assertEqual(self.mode.state(), self.mode.IDLE)
        self.assertFalse(self.mode.active())

    def test_a_claim_is_active_before_audio_flows(self):
        # The handover window - stopping the AI session, waiting for ALSA to
        # release the capture handle - is exactly what this has to cover.
        with self.mode.claim() as running:
            self.assertEqual(self.mode.state(), self.mode.STARTING)
            self.assertTrue(self.mode.active())
            running()
            self.assertEqual(self.mode.state(), self.mode.RUNNING)
        self.assertFalse(self.mode.active())

    def test_a_failed_session_does_not_leave_the_ai_path_locked_out(self):
        with self.assertRaises(RuntimeError), self.mode.claim():
            raise RuntimeError("endpoints could not open the microphone")
        self.assertEqual(self.mode.state(), self.mode.IDLE)

    def test_claiming_twice_is_refused(self):
        # Entered left to right: the first claim takes the mode, the second
        # finds it taken, and assertRaises sits between them to catch that.
        with self.mode.claim(), self.assertRaises(RuntimeError), self.mode.claim():
            pass
        self.assertFalse(self.mode.active())

    def test_state_is_readable_from_another_thread(self):
        seen = []
        with self.mode.claim():
            reader = threading.Thread(target=lambda: seen.append(self.mode.active()))
            reader.start()
            reader.join(timeout=2)
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
