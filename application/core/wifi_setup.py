"""Physical Wi-Fi setup gesture and shared device information."""

from pathlib import Path

from billy_runtime.paths import INSTALL_ROOT
from billy_runtime.storage import runtime_directory


PROJECT_ROOT = INSTALL_ROOT
ONBOARDING_FLAG = PROJECT_ROOT / "setup" / ".wifi_onboarding_active"


def _setup_after_client_marker() -> Path:
    # RAM, shared by Billy and the web interface, gone after a reboot.
    return runtime_directory(PROJECT_ROOT) / "wifi-setup-after-client"


def mark_setup_after_client() -> None:
    """Record that the setup hotspot replaced a client connection.

    After being connected and then an access point, the Pi's Wi-Fi chip
    cannot join a network again until a reboot. Saving Wi-Fi checks this to
    reboot instead of failing. Only a boot clears it, as only a boot fixes it.
    """
    marker = _setup_after_client_marker()
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        pass


def setup_follows_client() -> bool:
    try:
        return _setup_after_client_marker().exists()
    except OSError:
        return False


def wifi_mac_address(device: str = "wlan0") -> str:
    """Read the Wi-Fi interface address, including while disconnected."""
    if not device or Path(device).name != device or device in {".", ".."}:
        return ""
    try:
        address = (Path("/sys/class/net") / device / "address").read_text().strip()
    except OSError:
        return ""
    return address.upper()


class SetupHoldGesture:
    """Fire once per continuous ten-second press; require release to re-arm."""

    def __init__(self):
        self.pressed_since: float | None = None
        self.fired = False

    def observe(self, pressed: bool, now: float) -> bool:
        if not pressed:
            self.pressed_since = None
            self.fired = False
            return False
        if self.pressed_since is None:
            self.pressed_since = now
        if not self.fired and now - self.pressed_since >= 10.0:
            self.fired = True
            return True
        return False
