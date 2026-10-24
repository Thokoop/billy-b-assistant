"""PipeWire tools need the user session's socket, which a service does not inherit.

Loaded by path rather than imported: transport.py deliberately depends on
nothing in core, and core/__init__.py would pull in the whole package.
"""

import importlib.util
import os
import unittest
import unittest.mock
from pathlib import Path


def load_transport():
    path = Path(__file__).parents[2] / "application/core/bluetooth/transport.py"
    spec = importlib.util.spec_from_file_location("billy_bluetooth_transport", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PipeWireEnvTests(unittest.TestCase):
    def setUp(self):
        self.transport = load_transport()

    def test_a_service_without_a_user_session_gets_one_filled_in(self):
        # billy.service inherits neither variable, and pw-dump then exits 255
        # with nothing on stdout on every single poll.
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            env = self.transport.pipewire_env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], f"/run/user/{os.getuid()}")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"],
            f"unix:path=/run/user/{os.getuid()}/bus",
        )

    def test_an_existing_session_is_left_alone(self):
        # A shell, or the standalone service, which sets both itself.
        supplied = {
            "XDG_RUNTIME_DIR": "/run/user/4242",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/somewhere/else/bus",
        }
        with unittest.mock.patch.dict(os.environ, supplied, clear=True):
            env = self.transport.pipewire_env()
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/somewhere/else/bus"
        )

    def test_a_half_set_session_keeps_the_part_it_has(self):
        with unittest.mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}, clear=True
        ):
            env = self.transport.pipewire_env()
        # The bus follows the runtime dir it was given, not this process's uid.
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/4242/bus"
        )

    def test_the_rest_of_the_environment_survives(self):
        with unittest.mock.patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            env = self.transport.pipewire_env()
        self.assertEqual(env["PATH"], "/usr/bin")


if __name__ == "__main__":
    unittest.main()
