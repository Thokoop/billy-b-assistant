"""The route watcher: what it treats as a route, and how it hands audio over.

Extracted rather than imported: core/__init__.py pulls in the whole package
(and its network dependencies) to reach a module whose logic is a loop over
two functions.
"""

import ast
import sys
import threading
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace


def load_watcher():
    """The module's functions, with its imports replaced by doubles."""
    source = (Path(__file__).parents[2] / "core/bluetooth/watcher.py").read_text()
    tree = ast.parse(source)
    body = [
        item for item in tree.body if not isinstance(item, (ast.Import, ast.ImportFrom))
    ]
    namespace = {
        # The functions keep their own lazy imports, which is what keeps
        # core.bluetooth free of an import cycle; give them a package to
        # resolve those against.
        "__name__": "core.bluetooth.watcher",
        "__package__": "core.bluetooth",
        "threading": threading,
        "mode": SimpleNamespace(observe_route=lambda route: True),
        "logger": SimpleNamespace(
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
            verbose=lambda *a, **k: None,
        ),
    }
    exec(
        compile(ast.Module(body=body, type_ignores=[]), "watcher.py", "exec"),
        namespace,
    )
    return namespace


RUNNING_MEDIA = [
    {
        "name": "bluez_input.1C_3C_78_C9_7C_61.2",
        "kind": "Audio/Source",
        "device": "1C:3C:78:C9:7C:61",
        "state": "running",
    }
]
# What a phone streaming to Billy actually looks like before anything has
# attached to it. Measured on hardware: the node appears "suspended", and only
# reads "running" once something is consuming it - so state cannot be the
# trigger, because it only changes as a result of taking the route.
SUSPENDED_MEDIA = [dict(RUNNING_MEDIA[0], state="suspended")]
RUNNING_CALL = [
    {
        "name": "bluez_input.1C_3C_78_C9_7C_61.0",
        "kind": "Audio/Source",
        "device": "1C:3C:78:C9:7C:61",
        "state": "running",
    },
    {
        "name": "bluez_output.1C_3C_78_C9_7C_61.1",
        "kind": "Audio/Sink",
        "device": "1C:3C:78:C9:7C:61",
        "state": "running",
    },
]


class ActiveRouteTests(unittest.TestCase):
    def setUp(self):
        self.watcher = load_watcher()

    def route_from(self, nodes):
        def select_route(selected):
            sources = [n for n in selected if n["kind"] == "Audio/Source"]
            sinks = [n for n in selected if n["kind"] == "Audio/Sink"]
            if not sources:
                return None
            return sources[0]["name"], sinks[0]["name"] if sinks else None

        self.watcher["discover"] = lambda: nodes
        self.watcher["select_route"] = select_route
        return self.watcher["active_route"]()

    def test_a_streaming_phone_is_a_media_route(self):
        self.assertEqual(
            self.route_from(RUNNING_MEDIA),
            ("bluez_input.1C_3C_78_C9_7C_61.2", None),
        )

    def test_a_call_carries_a_sink_which_is_what_turns_on_the_microphone(self):
        self.assertEqual(
            self.route_from(RUNNING_CALL),
            (
                "bluez_input.1C_3C_78_C9_7C_61.0",
                "bluez_output.1C_3C_78_C9_7C_61.1",
            ),
        )

    def test_a_suspended_transport_is_still_a_route(self):
        # WirePlumber gives an unattached Bluetooth source back after about
        # five seconds, so a watcher that waits for "running" watches the node
        # appear and vanish forever without ever taking it.
        self.assertEqual(
            self.route_from(SUSPENDED_MEDIA),
            ("bluez_input.1C_3C_78_C9_7C_61.2", None),
        )

    def test_no_nodes_means_no_route(self):
        self.assertIsNone(self.route_from([]))

    def test_two_phones_stay_out_of_the_way_instead_of_crashing(self):
        def refuse(_nodes):
            raise RuntimeError("Disconnect other phones before starting")

        self.watcher["discover"] = lambda: RUNNING_CALL
        self.watcher["select_route"] = refuse
        self.assertIsNone(self.watcher["active_route"]())

    def test_a_broken_pw_dump_stays_out_of_the_way_too(self):
        def explode():
            raise OSError("pw-dump not found")

        self.watcher["discover"] = explode
        self.assertIsNone(self.watcher["active_route"]())


class HandoverTests(unittest.TestCase):
    """The session takes the audio path and always gives it back."""

    def setUp(self):
        self.watcher = load_watcher()
        self.calls = []
        mode_module = self._mode_module()
        self.watcher["mode"] = mode_module
        self.mode = mode_module
        self.button = SimpleNamespace(
            stop_for_takeover=lambda reason: self.calls.append(f"stop:{reason}"),
            resume_after_takeover=lambda: self.calls.append("resume"),
        )
        self.reported = []
        self.watcher["runtime"] = SimpleNamespace(
            session=self._session,
            status=lambda state, **details: self.reported.append(state),
            StreamRestart=RuntimeError,
            MAX_CONSECUTIVE_RESTARTS=5,
        )
        self.session_raises = None
        self.published = []
        self.patcher = unittest.mock.patch.dict(
            sys.modules,
            {
                "core": SimpleNamespace(button=self.button),
                "core.bluetooth": SimpleNamespace(),
                "core.mqtt": SimpleNamespace(
                    mqtt_publish=lambda topic, payload: self.published.append((
                        topic,
                        payload,
                    ))
                ),
                "core.config": SimpleNamespace(BLUETOOTH_CALL_AEC=True),
                "core.bluetooth.endpoints": SimpleNamespace(
                    SharedEndpoints=lambda **kwargs: self.calls.append(
                        f"endpoints:{sorted(kwargs.items())}"
                    )
                ),
            },
        )

    def _mode_module(self):
        import contextlib

        state = {"claimed": False, "running": False}

        @contextlib.contextmanager
        def claim(route=None):
            state["claimed"] = True
            try:
                yield lambda: state.update(running=True)
            finally:
                state["claimed"] = False

        return SimpleNamespace(
            claim=claim, state=state, IDLE="idle", media_allowed=lambda route: True
        )

    def _session(self, route, endpoints, stop=None):
        self.calls.append("session")
        self.assertTrue(self.mode.state["claimed"])
        self.assertTrue(self.mode.state["running"])
        if self.session_raises is not None:
            raise self.session_raises

    def run_session(self, route):
        # The watcher imports core.button and the endpoints lazily, inside the
        # function, which is what keeps core.bluetooth free of an import cycle.
        with self.patcher:
            self.watcher["_run_session"](route)

    def test_a_call_stops_the_ai_session_first_and_resumes_after(self):
        self.run_session(("source", "sink"))
        self.assertEqual(
            [call for call in self.calls if not call.startswith("endpoints:")],
            ["stop:bluetooth", "session", "resume"],
        )
        self.assertFalse(self.mode.state["claimed"])

    def test_dismissal_during_takeover_does_not_open_audio(self):
        self.mode.media_allowed = lambda route: False
        self.run_session(("source", None))
        self.assertNotIn("session", self.calls)
        self.assertIn("resume", self.calls)
        self.assertFalse(self.mode.state["claimed"])

    def test_a_call_wants_the_microphone_and_cancellation(self):
        self.run_session(("source", "sink"))
        endpoints = next(c for c in self.calls if c.startswith("endpoints:"))
        self.assertIn("('want_aec', True)", endpoints)
        self.assertIn("('want_microphone', True)", endpoints)

    def test_media_needs_neither(self):
        self.run_session(("source", None))
        endpoints = next(c for c in self.calls if c.startswith("endpoints:"))
        self.assertIn("('want_aec', False)", endpoints)
        self.assertIn("('want_microphone', False)", endpoints)

    def test_the_state_it_publishes_is_what_colours_the_led(self):
        # mqtt_publish() routes billy/state into set_status_led_state(), so
        # this one call is both the Home Assistant state and the LED colour.
        self.run_session(("source", "sink"))
        self.assertEqual(self.published, [("billy/state", "in_call")])

    def test_media_and_a_call_are_told_apart(self):
        self.run_session(("source", None))
        self.assertEqual(self.published, [("billy/state", "bt_media")])

    def test_settings_is_told_when_the_audio_path_goes_quiet(self):
        # runtime.session() reports media_audio/call_audio while it runs, and
        # webconfig reads that file to show what Billy is doing - so something
        # has to say when it stops, or Settings claims a finished call forever.
        self.run_session(("source", "sink"))
        self.assertEqual(self.reported, ["idle"])

    def test_a_failed_session_says_so_too(self):
        self.session_raises = RuntimeError("AEC could not start")
        with self.assertRaises(RuntimeError):
            self.run_session(("source", "sink"))
        self.assertEqual(self.reported, ["idle"])

    def test_a_failed_session_still_gives_the_audio_path_back(self):
        self.session_raises = RuntimeError("AEC could not start")
        with self.assertRaises(RuntimeError):
            self.run_session(("source", "sink"))
        self.assertIn("resume", self.calls)
        self.assertFalse(self.mode.state["claimed"])


if __name__ == "__main__":
    unittest.main()
