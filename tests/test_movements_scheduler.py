"""_StopScheduler replaces a threading.Timer per motor stop.

Mouth flaps fire several times a second in the same process as the audio
callbacks, so the thread churn mattered: braking on every quiet frame alone
cost ~1.7% of captured microphone audio. These tests extract the class
rather than importing core.movements, which pulls in the whole core package
(and its network dependencies) just to reach one scheduler.
"""

import ast
import threading
import time
import unittest
from pathlib import Path


def load_scheduler():
    source = (Path(__file__).parents[1] / "core/movements.py").read_text()
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "_StopScheduler"
    )
    namespace = {"threading": threading, "time": time}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "core/movements.py", "exec"),
        namespace,
    )
    return namespace["_StopScheduler"]


_StopScheduler = load_scheduler()


class StopSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = _StopScheduler()

    def test_runs_the_action_after_the_delay(self):
        fired = threading.Event()
        self.scheduler.schedule(1, 0.05, fired.set)
        self.assertFalse(fired.is_set())
        self.assertTrue(fired.wait(1.0))

    def test_rescheduling_a_pin_replaces_the_pending_stop(self):
        """A flap arriving mid-hold extends it instead of racing the old one."""
        order = []
        self.scheduler.schedule(1, 0.02, lambda: order.append("first"))
        self.scheduler.schedule(1, 0.15, lambda: order.append("second"))
        time.sleep(0.4)
        self.assertEqual(order, ["second"])

    def test_separate_pins_keep_their_own_deadlines(self):
        order = []
        self.scheduler.schedule(2, 0.15, lambda: order.append("late"))
        self.scheduler.schedule(1, 0.02, lambda: order.append("early"))
        time.sleep(0.4)
        self.assertEqual(order, ["early", "late"])

    def test_cancel_drops_a_pending_stop(self):
        fired = threading.Event()
        self.scheduler.schedule(1, 0.05, fired.set)
        self.scheduler.cancel(1)
        self.assertFalse(fired.wait(0.2))

    def test_one_thread_regardless_of_how_many_stops(self):
        """The whole point: flaps must not create a thread each."""
        before = threading.active_count()
        for index in range(50):
            self.scheduler.schedule(index % 3, 0.01, lambda: None)
        peak = threading.active_count()
        time.sleep(0.3)
        self.assertLessEqual(peak - before, 1)

    def test_a_sooner_deadline_wakes_a_waiting_thread(self):
        """Scheduling nearer work must not wait behind a far-off deadline."""
        far = []
        self.scheduler.schedule(1, 5.0, lambda: far.append("far"))
        started = time.monotonic()
        done = threading.Event()
        self.scheduler.schedule(2, 0.05, done.set)
        self.assertTrue(done.wait(1.0))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(far, [])


if __name__ == "__main__":
    unittest.main()
