"""Bounded, read-only journal access for the Debug panel.

Only fixed service groups are accepted. User audio units are scoped to the
webservice's Unix user (the same Billy user that owns the audio session).
"""

import os
import subprocess


SOURCES = {
    "billy": ("billy.service",),
    "web": ("billy-webconfig.service",),
    "bluetooth": ("bluetooth.service",),
    "pipewire": (),  # Retained for already-open Debug clients.
    "all": ("billy.service", "billy-webconfig.service", "bluetooth.service"),
}
AUDIO_UNITS = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")
MAX_LOG_CHARS = 400_000


def read_logs(source="billy"):
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("Unknown log source")
    matches = [[f"_SYSTEMD_UNIT={unit}"] for unit in SOURCES[source]]
    if source in {"bluetooth", "pipewire", "all"}:
        matches += [
            [f"_SYSTEMD_USER_UNIT={unit}", f"_UID={os.getuid()}"]
            for unit in AUDIO_UNITS
        ]
    args = ["journalctl", "--no-pager", "--output=short-precise", "-b", "-n", "200"]
    for index, group in enumerate(matches):
        if index:
            args.append("+")
        args.extend(group)
    result = {"source": source, "logs": "", "error": ""}
    try:
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=2,
        )
        result["logs"] = process.stdout[-MAX_LOG_CHARS:]
        # journalctl can return 0 with partial access. Keep that hint visible;
        # empty output alone is not proof that the services are healthy.
        if process.returncode or process.stderr.strip():
            result["error"] = (
                process.stderr.strip()[-2000:] or "Could not read service logs."
            )
    except FileNotFoundError:
        result["error"] = (
            "Service logs are only available on a Billy running Linux with systemd."
        )
    except subprocess.TimeoutExpired:
        result["error"] = (
            "Reading service logs took too long. Retrying while Debug is open."
        )
    except OSError:
        result["error"] = (
            "Service logs could not be read. Check the web service's journal permissions."
        )
    return result
