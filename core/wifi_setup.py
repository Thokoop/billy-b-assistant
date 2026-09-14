"""Physical Wi-Fi setup gesture and shared device information."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONBOARDING_FLAG = PROJECT_ROOT / "setup" / ".wifi_onboarding_active"


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
