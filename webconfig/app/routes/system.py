import os
import shutil
import subprocess

# Import logger
import sys
import threading
import time
import uuid
from base64 import b64decode
from glob import glob
from pathlib import Path

from dotenv import dotenv_values, find_dotenv, set_key
from flask import Blueprint, jsonify, redirect, render_template, request
from packaging.version import parse as parse_version
from werkzeug.utils import secure_filename

from core.news_manager import load_news_sources, save_news_sources
from core.vision import describe_scene

from ..core_imports import core_config, voice_provider_registry
from ..state import (
    PROJECT_ROOT,
    RELEASE_NOTE,
    get_current_version,
    load_versions,
    save_versions,
)


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.logger import logger


bp = Blueprint("system", __name__)

# Find .env file in project root (not webconfig directory)
ENV_PATH = find_dotenv(usecwd=True)
if not ENV_PATH or not os.path.exists(ENV_PATH):
    # Fallback: look for .env in project root
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    ENV_PATH = os.path.join(project_root, ".env")
CONFIG_KEYS = [
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "OPENAI_MODEL",
    "XAI_MODEL",
    "REALTIME_AI_PROVIDER",
    "BILLY_MODEL",
    "BILLY_PINS",
    "MIC_TIMEOUT_SECONDS",
    "SILENCE_THRESHOLD",
    "MQTT_HOST",
    "MQTT_PORT",
    "MQTT_USERNAME",
    "MQTT_PASSWORD",
    "HA_HOST",
    "HA_TOKEN",
    "HA_LANG",
    "MIC_PREFERENCE",
    "SPEAKER_PREFERENCE",
    "FLASK_PORT",
    "RUN_MODE",
    "SHOW_SUPPORT",
    "TURN_EAGERNESS",
    "FORCE_PASS_CHANGE",
    "MOUTH_ARTICULATION",
    "LOG_LEVEL",
    "DEFAULT_USER",
    "CURRENT_USER",
    "SHOW_RC_VERSIONS",
    "FLAP_ON_BOOT",
    "NEWS_REQUEST_TIMEOUT_SECONDS",
    "CAMERA_HARDWARE",
    "CAMERA_DEVICE_INDEX",
    "CAMERA_ROTATION",
    "FOLLOW_UP_RETRY_LIMIT",
    "WAKE_WORD_ENABLED",
    "WAKE_WORD_BACKEND",
    "WAKE_WORD_COOLDOWN_SECONDS",
    "PORCUPINE_ACCESS_KEY",
    "WAKE_WORD_PORCUPINE_KEYWORD_PATH",
    "WAKE_WORD_PORCUPINE_SENSITIVITY",
    "WAKE_WORD_OPENWAKEWORD_MODEL_PATH",
    "WAKE_WORD_OPENWAKEWORD_THRESHOLD",
    "STATUS_LED_ENABLED",
    "STATUS_LED_BRIGHTNESS",
    "WIFI_COUNTRY",
    "WIFI_ONBOARDING_MODE",
]
WAKEWORD_REL_ROOT = Path("wakewords")
WIFI_ONBOARDING_FLAG = Path(PROJECT_ROOT) / "setup" / ".wifi_onboarding_active"
WIFI_TEST_PREFIX = "billy-test-"
WIFI_CAPTIVE_PORTAL_DNSMASQ_CONF = "/etc/dnsmasq.d/billy-captive-portal.conf"
WIFI_NM_CAPTIVE_PORTAL_DNSMASQ_CONF = (
    "/etc/NetworkManager/dnsmasq-shared.d/billy-captive-portal.conf"
)
WIFI_UNIFIED_HOTSPOT_CON_NAME = "Billy-Onboarding-Hotspot"
WIFI_UNIFIED_HOTSPOT_SSID = "Billy_Bassistant"
WIFI_COUNTRIES = [
    ("AU", "Australia"),
    ("BR", "Brazil"),
    ("CA", "Canada"),
    ("CN", "China"),
    ("DE", "Germany"),
    ("ES", "Spain"),
    ("FR", "France"),
    ("GB", "United Kingdom"),
    ("IN", "India"),
    ("IT", "Italy"),
    ("JP", "Japan"),
    ("KR", "South Korea"),
    ("NL", "Netherlands"),
    ("US", "United States"),
]


def _detect_rpi_camera_available() -> bool:
    camera_bin = str(getattr(core_config, "LIBCAMERA_STILL_BIN", "libcamera-still"))
    try:
        result = subprocess.run(
            [camera_bin, "--list-cameras"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except FileNotFoundError:
        return False
    except Exception:
        return False

    combined = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "no cameras available" in combined:
        return False
    if "available cameras" in combined:
        return True
    # Fallback: some builds may still expose camera lines without the header.
    return "camera" in combined and "/base/" in combined


def _is_v4l2_capture_capable(sys_video_dir: Path) -> bool:
    """Return True only for nodes with video-capture capability."""
    # V4L2 capability bits (videodev2.h)
    v4l2_cap_video_capture = 0x00000001
    v4l2_cap_video_capture_mplane = 0x00001000
    v4l2_cap_meta_capture = 0x00800000

    caps_path = sys_video_dir / "device_caps"
    try:
        if not caps_path.exists():
            # Keep backward-compatible behavior on platforms without device_caps.
            return True
        caps = int(caps_path.read_text(encoding="utf-8").strip(), 16)
    except Exception:
        return True

    has_capture = bool(caps & (v4l2_cap_video_capture | v4l2_cap_video_capture_mplane))
    has_only_metadata = bool(caps & v4l2_cap_meta_capture) and not has_capture
    return has_capture and not has_only_metadata


def _is_primary_v4l2_node(sys_video_dir: Path) -> bool:
    """Prefer primary node index 0 to skip common metadata/helper siblings."""
    index_path = sys_video_dir / "index"
    try:
        if not index_path.exists():
            return True
        return index_path.read_text(encoding="utf-8").strip() == "0"
    except Exception:
        return True


def _detect_usb_video_nodes() -> list[dict[str, str | int]]:
    nodes: list[dict[str, str | int]] = []
    for path in sorted(glob("/dev/video*")):
        name = os.path.basename(path)
        suffix = name.removeprefix("video")
        if not suffix.isdigit():
            continue
        index = int(suffix)
        sys_video_dir = Path("/sys/class/video4linux") / name
        sys_name_path = sys_video_dir / "name"
        sys_device_path = sys_video_dir / "device"

        device_name = ""
        try:
            if sys_name_path.exists():
                device_name = sys_name_path.read_text(encoding="utf-8").strip()
        except Exception:
            device_name = ""

        # Keep the dropdown focused on real USB webcams only.
        try:
            real_device_path = os.path.realpath(str(sys_device_path))
        except Exception:
            real_device_path = ""
        is_usb_backed = "/usb" in real_device_path.lower()
        if not is_usb_backed:
            continue

        lower_name = device_name.lower()
        if "metadata" in lower_name:
            continue
        if not _is_v4l2_capture_capable(sys_video_dir):
            continue
        if not _is_primary_v4l2_node(sys_video_dir):
            continue

        if device_name:
            label = f"{device_name} ({path}) (#{index})"
        else:
            label = f"USB Webcam ({path}) (#{index})"
        nodes.append({
            "path": path,
            "index": index,
            "label": label,
        })
    return nodes


def _list_available_wakeword_files(*suffixes: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    abs_root = Path(PROJECT_ROOT) / WAKEWORD_REL_ROOT
    if not abs_root.exists():
        return paths
    normalized_suffixes = tuple(
        suffix if suffix.startswith(".") else f".{suffix}" for suffix in suffixes
    )
    for path in sorted(abs_root.rglob("*")):
        if not path.is_file():
            continue
        if path.parent != abs_root:
            continue
        if normalized_suffixes and path.suffix.lower() not in normalized_suffixes:
            continue
        filename = path.name
        if filename in seen:
            continue
        seen.add(filename)
        paths.append(filename)
    return paths


def _normalize_source_payload(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    topics_raw = data.get("topics", [])

    if not name:
        raise ValueError("Source name is required")
    if not url:
        raise ValueError("Source URL is required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Source URL must start with http:// or https://")
    return {
        "name": name,
        "url": url,
        "topics": _normalize_topics(topics_raw),
    }


def _normalize_topics(raw_topics) -> list[str]:
    if isinstance(raw_topics, str):
        source = [part.strip().lower() for part in raw_topics.split(",")]
    elif isinstance(raw_topics, list):
        source = [str(part).strip().lower() for part in raw_topics]
    else:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for topic in source:
        if not topic or topic in seen:
            continue
        seen.add(topic)
        normalized.append(topic)
    return normalized


def _is_wifi_onboarding_active() -> bool:
    return WIFI_ONBOARDING_FLAG.exists()


def _is_legacy_wifi_hotspot_onboarding() -> bool:
    return (
        _is_wifi_onboarding_active()
        and str(getattr(core_config, "WIFI_ONBOARDING_MODE", "legacy")).strip().lower()
        != "unified"
    )


def _is_unified_wifi_hotspot_onboarding() -> bool:
    return (
        _is_wifi_onboarding_active()
        and str(getattr(core_config, "WIFI_ONBOARDING_MODE", "legacy")).strip().lower()
        == "unified"
    )


def _set_wifi_onboarding_active(active: bool) -> None:
    try:
        if active:
            WIFI_ONBOARDING_FLAG.touch(exist_ok=True)
        elif WIFI_ONBOARDING_FLAG.exists():
            WIFI_ONBOARDING_FLAG.unlink()
    except Exception as exc:
        logger.warning(f"[wifi] Failed to update onboarding flag: {exc}")


def _run_wifi_command(
    args: list[str], *, check: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
    )


def _list_active_wifi_connections() -> list[dict[str, str]]:
    try:
        result = _run_wifi_command([
            "nmcli",
            "-t",
            "-f",
            "NAME,TYPE,DEVICE",
            "connection",
            "show",
            "--active",
        ])
        if result.returncode != 0:
            return []
        active: list[dict[str, str]] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 3:
                continue
            name, conn_type, device = parts[0], parts[1], parts[2]
            if conn_type not in {"wifi", "802-11-wireless"}:
                continue
            active.append({"name": name, "device": device})
        return active
    except Exception as exc:
        logger.warning(f"[wifi] Failed to list active Wi-Fi connections: {exc}")
        return []


def _get_active_wifi_connection() -> dict[str, str] | None:
    active = _list_active_wifi_connections()
    for entry in active:
        if entry.get("device") == "wlan0":
            target = entry
            break
    else:
        target = active[0] if active else None

    if not target:
        return None

    name = str(target.get("name") or "")
    if name.startswith(WIFI_TEST_PREFIX):
        try:
            result = _run_wifi_command([
                "nmcli",
                "-g",
                "802-11-wireless.ssid",
                "connection",
                "show",
                name,
            ])
            ssid = (result.stdout or "").strip()
            if ssid:
                target = target.copy()
                target["name"] = ssid
        except Exception as exc:
            logger.warning(
                f"[wifi] Failed to resolve test Wi-Fi SSID for {name}: {exc}"
            )

    return target


def _delete_wifi_connection(name: str) -> None:
    if not name:
        return
    _run_wifi_command(["sudo", "nmcli", "connection", "delete", name], check=False)


def _activate_wifi_connection(name: str) -> tuple[bool, str]:
    if not name:
        return False, "Missing connection name"
    result = _run_wifi_command([
        "sudo",
        "nmcli",
        "connection",
        "up",
        name,
        "ifname",
        "wlan0",
    ])
    if result.returncode != 0:
        return (
            False,
            result.stderr.strip()
            or result.stdout.strip()
            or "Failed to activate Wi-Fi connection",
        )
    return True, name


def _cleanup_test_wifi_connections() -> None:
    try:
        result = _run_wifi_command(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        if result.returncode != 0:
            return
        for raw_name in result.stdout.splitlines():
            name = raw_name.strip()
            if name.startswith(WIFI_TEST_PREFIX):
                _delete_wifi_connection(name)
    except Exception as exc:
        logger.warning(f"[wifi] Failed to clean test connections: {exc}")


def _set_wifi_country(country: str) -> None:
    normalized = str(country or "").strip().upper() or "US"
    if not normalized:
        return
    _run_wifi_command(["sudo", "iw", "reg", "set", normalized], check=False)
    set_key(ENV_PATH, "WIFI_COUNTRY", normalized, quote_mode="never")


def _get_wlan0_ip_address() -> str:
    try:
        result = _run_wifi_command([
            "nmcli",
            "-t",
            "-f",
            "IP4.ADDRESS",
            "device",
            "show",
            "wlan0",
        ])
        if result.returncode != 0:
            return ""
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or ":" not in line:
                continue
            _, value = line.split(":", 1)
            value = value.strip()
            if value:
                return value.split("/", 1)[0]
    except Exception:
        return ""
    return ""


def _stop_wifi_onboarding_services() -> list[str]:
    errors: list[str] = []
    is_unified = _is_unified_wifi_hotspot_onboarding()
    stop_commands = [
        [
            "sudo",
            "rm",
            "-f",
            WIFI_CAPTIVE_PORTAL_DNSMASQ_CONF,
            WIFI_NM_CAPTIVE_PORTAL_DNSMASQ_CONF,
        ],
        ["sudo", "systemctl", "stop", "billy-wifi-setup.service"],
    ]
    if is_unified:
        stop_commands.extend([
            ["sudo", "nmcli", "connection", "down", WIFI_UNIFIED_HOTSPOT_CON_NAME],
            ["sudo", "systemctl", "reload", "NetworkManager"],
        ])
    else:
        stop_commands.extend([
            ["sudo", "systemctl", "stop", "hostapd"],
            ["sudo", "systemctl", "stop", "dnsmasq"],
        ])

    for args in stop_commands:
        try:
            result = _run_wifi_command(args)
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                if stderr:
                    errors.append(stderr)
        except Exception as exc:
            errors.append(str(exc))
    _set_wifi_onboarding_active(False)
    return errors


def _restore_unified_wifi_onboarding_hotspot() -> None:
    if not _is_unified_wifi_hotspot_onboarding():
        return

    _set_wifi_onboarding_active(True)
    for args in (
        ["sudo", "systemctl", "stop", "hostapd"],
        ["sudo", "systemctl", "stop", "dnsmasq"],
        ["sudo", "systemctl", "mask", "--runtime", "hostapd"],
        ["sudo", "systemctl", "mask", "--runtime", "dnsmasq"],
        ["sudo", "systemctl", "start", "NetworkManager"],
        ["sudo", "nmcli", "radio", "wifi", "on"],
        ["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"],
        ["sudo", "nmcli", "connection", "down", WIFI_UNIFIED_HOTSPOT_CON_NAME],
        ["sudo", "nmcli", "device", "disconnect", "wlan0"],
        ["sudo", "nmcli", "connection", "up", WIFI_UNIFIED_HOTSPOT_CON_NAME],
    ):
        try:
            _run_wifi_command(args, check=False)
        except Exception as exc:
            logger.warning(
                f"[wifi] Failed to restore unified onboarding hotspot: {exc}"
            )


def _wifi_connect(
    ssid: str,
    password: str,
    country: str,
    *,
    connection_name: str,
    autoconnect: bool,
) -> tuple[bool, str]:
    try:
        _set_wifi_country(country)
        _run_wifi_command(["sudo", "systemctl", "start", "NetworkManager"], check=False)
        _run_wifi_command(
            ["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"], check=False
        )
        _run_wifi_command(
            ["sudo", "nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"], check=False
        )
        time.sleep(2)
        _run_wifi_command(
            ["sudo", "nmcli", "dev", "wifi", "list", "ifname", "wlan0"], check=False
        )
        time.sleep(2)

        _delete_wifi_connection(connection_name)

        if password:
            create_result = _run_wifi_command([
                "sudo",
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                "wlan0",
                "con-name",
                connection_name,
                "ssid",
                ssid,
                "wifi-sec.key-mgmt",
                "wpa-psk",
                "wifi-sec.psk",
                password,
            ])
        else:
            create_result = _run_wifi_command([
                "sudo",
                "nmcli",
                "connection",
                "add",
                "type",
                "wifi",
                "ifname",
                "wlan0",
                "con-name",
                connection_name,
                "ssid",
                ssid,
            ])

        if create_result.returncode != 0:
            error = (
                create_result.stderr.strip()
                or create_result.stdout.strip()
                or "Wi-Fi connection profile creation failed"
            )
            if "Secrets were required" in error and not password:
                error = "This network requires a password."
            _delete_wifi_connection(connection_name)
            return False, error

        modify_result = _run_wifi_command([
            "sudo",
            "nmcli",
            "connection",
            "modify",
            connection_name,
            "connection.autoconnect",
            "yes" if autoconnect else "no",
        ])
        if modify_result.returncode != 0:
            return False, (
                modify_result.stderr.strip()
                or modify_result.stdout.strip()
                or "Failed to update Wi-Fi connection settings"
            )

        up_result = _run_wifi_command([
            "sudo",
            "nmcli",
            "connection",
            "up",
            connection_name,
            "ifname",
            "wlan0",
        ])
        if up_result.returncode != 0:
            error = (
                up_result.stderr.strip()
                or up_result.stdout.strip()
                or "Wi-Fi connection failed"
            )
            _delete_wifi_connection(connection_name)
            return False, error

        return True, connection_name
    except Exception as exc:
        return False, str(exc)


def _remove_path(path: Path, removed: list[str]) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    removed.append(str(path))


def _remove_env_and_versions() -> dict:
    removed: dict[str, bool] = {"env": False, "versions": False}
    errors: list[str] = []

    env_path = Path(ENV_PATH)
    example_path = Path(PROJECT_ROOT) / ".env.example"
    if env_path.exists():
        try:
            env_path.unlink()
        except Exception as exc:
            errors.append(f"Failed to delete .env: {exc}")
    try:
        if example_path.exists():
            shutil.copy(example_path, env_path)
        else:
            env_path.touch()
        removed["env"] = True
    except Exception as exc:
        errors.append(f"Failed to reset .env: {exc}")

    versions_path = Path(PROJECT_ROOT) / "versions.ini"
    if versions_path.exists():
        try:
            versions_path.unlink()
            removed["versions"] = True
        except Exception as exc:
            errors.append(f"Failed to delete versions.ini: {exc}")

    return {"removed": removed, "errors": errors}


def _remove_custom_profiles() -> dict:
    removed: list[str] = []
    errors: list[str] = []

    profiles_dir = Path(PROJECT_ROOT) / "profiles"
    if profiles_dir.exists():
        for profile_file in profiles_dir.glob("*.ini"):
            if (
                profile_file.name.lower() == "guest.ini"
                or profile_file.stem.lower() == "guest"
            ):
                continue
            try:
                _remove_path(profile_file, removed)
            except Exception as exc:
                errors.append(f"Failed to delete profile {profile_file}: {exc}")

    return {"removed": removed, "errors": errors}


def _remove_custom_personas() -> dict:
    removed: list[str] = []
    errors: list[str] = []

    default_persona = Path(PROJECT_ROOT) / "persona.ini"
    personas_dir = Path(PROJECT_ROOT) / "personas"
    if personas_dir.exists():
        for persona_file in personas_dir.glob("*.ini"):
            if persona_file.stem.lower() == "default":
                continue
            try:
                _remove_path(persona_file, removed)
            except Exception as exc:
                errors.append(f"Failed to delete persona file {persona_file}: {exc}")
        for persona_dir in personas_dir.iterdir():
            if not persona_dir.is_dir():
                continue
            if persona_dir.name.lower() == "default":
                continue
            try:
                _remove_path(persona_dir, removed)
            except Exception as exc:
                errors.append(f"Failed to delete persona folder {persona_dir}: {exc}")

    # Always keep the default persona.ini in the project root
    if default_persona.exists():
        pass

    return {"removed": removed, "errors": errors}


def _clear_service_logs() -> dict:
    errors: list[str] = []
    units = ["billy.service", "billy-webconfig.service"]
    for unit in units:
        for args in (
            ["sudo", "journalctl", "-u", unit, "--rotate"],
            ["sudo", "journalctl", "-u", unit, "--vacuum-time=1s"],
        ):
            try:
                result = subprocess.run(
                    args, check=False, capture_output=True, text=True
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip() or result.stdout.strip()
                    errors.append(f"{unit}: {' '.join(args[1:])} failed: {stderr}")
            except FileNotFoundError:
                errors.append("journalctl not available")
                break
            except Exception as exc:
                errors.append(f"{unit}: {' '.join(args[1:])} failed: {exc}")

    # Clear CLI history for the user running this service.
    try:
        import pwd

        current_user = pwd.getpwuid(os.getuid())
        current_home = Path(current_user.pw_dir)
        for history_file in (
            current_home / ".bash_history",
            current_home / ".zsh_history",
        ):
            if history_file.exists():
                history_file.write_text("")
    except Exception as exc:
        errors.append(f"Failed to clear CLI history: {exc}")
    return {"errors": errors}


def _remove_active_wifi_connections() -> dict:
    removed: list[str] = []
    errors: list[str] = []
    try:
        logger.info(
            "[factory_reset] Checking active NetworkManager connections...", "📡"
        )
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            errors.append(f"nmcli list failed: {stderr}")
            logger.warning(f"[factory_reset] nmcli list failed: {stderr}")
            return {"removed": removed, "errors": errors}
        logger.info(f"[factory_reset] nmcli active output: {result.stdout.strip()}")
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            name, conn_type = parts[0], parts[1]
            if conn_type not in {"wifi", "802-11-wireless"} or not name:
                continue
            logger.info(f"[factory_reset] Deleting Wi-Fi connection: {name}")
            delete_result = subprocess.run(
                ["sudo", "nmcli", "connection", "delete", name],
                check=False,
                capture_output=True,
                text=True,
            )
            if delete_result.returncode != 0:
                stderr = delete_result.stderr.strip() or delete_result.stdout.strip()
                errors.append(f"Failed to delete Wi-Fi '{name}': {stderr}")
                logger.warning(
                    f"[factory_reset] Failed to delete Wi-Fi '{name}': {stderr}"
                )
                continue
            removed.append(name)
            logger.info(f"[factory_reset] Deleted Wi-Fi connection: {name}")
    except FileNotFoundError:
        errors.append("nmcli not available")
        logger.warning("[factory_reset] nmcli not available")
    except Exception as exc:
        errors.append(f"Wi-Fi removal failed: {exc}")
        logger.warning(f"[factory_reset] Wi-Fi removal failed: {exc}")
    return {"removed": removed, "errors": errors}


def _reset_git_worktree() -> dict:
    errors: list[str] = []
    details: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            errors.append(f"git rev-parse failed: {stderr}")
            return {"errors": errors, "details": details}
        head = result.stdout.strip()
        details["head"] = head

        reset_result = subprocess.run(
            ["git", "reset", "--hard", head],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if reset_result.returncode != 0:
            stderr = reset_result.stderr.strip() or reset_result.stdout.strip()
            errors.append(f"git reset --hard failed: {stderr}")
    except FileNotFoundError:
        errors.append("git not available")
    except Exception as exc:
        errors.append(f"git reset failed: {exc}")

    return {"errors": errors, "details": details}


def delayed_restart():
    time.sleep(1.5)
    subprocess.run(["sudo", "systemctl", "restart", "billy-webconfig.service"])
    subprocess.run(["sudo", "systemctl", "restart", "billy.service"])


@bp.route("/")
def index():
    return render_template(
        "index.html",
        config={k: str(getattr(core_config, k, "")) for k in CONFIG_KEYS}
        | {
            "VOICE_OPTIONS": [
                "alloy",
                "ash",
                "ballad",
                "coral",
                "echo",
                "sage",
                "shimmer",
                "verse",
                "marin",
                "cedar",
            ],
            "WIFI_COUNTRIES": WIFI_COUNTRIES,
            "WIFI_ONBOARDING_ACTIVE": _is_wifi_onboarding_active(),
        },
    )


def _captive_portal_redirect():
    host = _get_wlan0_ip_address() or "192.168.4.1"
    port = str(getattr(core_config, "FLASK_PORT", "80"))
    target = f"http://{host}/" if port == "80" else f"http://{host}:{port}/"
    return redirect(target, code=302)


@bp.route("/generate_204", methods=["GET", "HEAD"])
@bp.route("/gen_204", methods=["GET", "HEAD"])
@bp.route("/hotspot-detect.html", methods=["GET", "HEAD"])
@bp.route("/canonical.html", methods=["GET", "HEAD"])
@bp.route("/ncsi.txt", methods=["GET", "HEAD"])
@bp.route("/connecttest.txt", methods=["GET", "HEAD"])
@bp.route("/success.txt", methods=["GET", "HEAD"])
@bp.route("/library/test/success.html", methods=["GET", "HEAD"])
@bp.route("/kindle-wifi/wifistub.html", methods=["GET", "HEAD"])
def captive_portal_probe():
    if _is_unified_wifi_hotspot_onboarding():
        return _captive_portal_redirect()
    return "", 404


@bp.route("/<path:path>", methods=["GET", "HEAD"])
def captive_portal_fallback(path: str):
    if not _is_unified_wifi_hotspot_onboarding():
        return "", 404

    if path.startswith("static/"):
        return "", 404

    return _captive_portal_redirect()


@bp.route("/version")
def version_info():
    versions = load_versions()
    # Always check the current checked-out version from git
    current = get_current_version()
    latest = versions["version"].get("latest", "unknown")

    logger.verbose(
        f"[version_info] Detected current version: {current}, stored: {versions['version'].get('current', 'unknown')}, latest: {latest}"
    )

    # Update the stored current version if it's different
    stored_current = versions["version"].get("current", "unknown")
    if current != stored_current:
        logger.info(
            f"[version_info] Updating stored version from {stored_current} to {current}"
        )
        save_versions(current, latest)

    try:
        update_available = (
            current != "unknown"
            and latest != "unknown"
            and parse_version(latest.lstrip("v")) > parse_version(current.lstrip("v"))
        )
    except Exception as e:
        logger.warning(f"[version_info] Error checking update availability: {e}")
        update_available = False

    response = {
        "current": current,
        "latest": latest,
        "update_available": update_available,
    }
    logger.verbose(f"[version_info] Returning: {response}")
    return jsonify(response)


@bp.route("/update", methods=["POST"])
def perform_update():
    versions = load_versions()
    current = versions["version"].get("current", "unknown")
    latest = versions["version"].get("latest", "unknown")
    if current == latest or latest == "unknown":
        return jsonify({"status": "up-to-date", "version": current})
    try:
        subprocess.check_output(["git", "remote", "-v"], cwd=PROJECT_ROOT, text=True)
        subprocess.check_call(["git", "fetch", "--tags"], cwd=PROJECT_ROOT)
        subprocess.check_call(
            ["git", "checkout", "--force", f"tags/{latest}"], cwd=PROJECT_ROOT
        )
        venv_pip = os.path.join(PROJECT_ROOT, "venv", "bin", "pip")
        output = subprocess.check_output(
            [venv_pip, "install", "--upgrade", "-r", "requirements.txt"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info(f"📦 Pip install output:\n{output}")
        # Refresh current version from git after checkout to ensure accuracy
        actual_current = get_current_version()
        save_versions(actual_current, latest)
        threading.Thread(
            target=lambda: (
                time.sleep(2),
                subprocess.run([
                    "sudo",
                    "systemctl",
                    "restart",
                    "billy-webconfig.service",
                ]),
            )
        ).start()
        threading.Thread(
            target=lambda: (
                time.sleep(2),
                subprocess.run(["sudo", "systemctl", "restart", "billy.service"]),
            )
        ).start()
        return jsonify({"status": "updated", "version": latest})
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route("/update-simulate", methods=["POST"])
def simulate_update():
    """Run update workflow against currently checked-out revision."""
    try:
        versions = load_versions()
        latest = versions["version"].get("latest", "unknown")

        # Keep current code version: reinstall deps for current checkout and restart services.
        current_ref = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
        subprocess.check_call(
            ["git", "checkout", "--force", current_ref], cwd=PROJECT_ROOT
        )

        venv_pip = os.path.join(PROJECT_ROOT, "venv", "bin", "pip")
        output = subprocess.check_output(
            [venv_pip, "install", "--upgrade", "-r", "requirements.txt"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info(f"📦 Reinstall current version pip output:\n{output}")

        actual_current = get_current_version()
        save_versions(actual_current, latest)

        threading.Thread(
            target=lambda: (
                time.sleep(2),
                subprocess.run([
                    "sudo",
                    "systemctl",
                    "restart",
                    "billy-webconfig.service",
                ]),
            )
        ).start()
        threading.Thread(
            target=lambda: (
                time.sleep(2),
                subprocess.run(["sudo", "systemctl", "restart", "billy.service"]),
            )
        ).start()

        return jsonify({
            "status": "restarting",
            "message": "Reinstall complete. Restarting services...",
            "version": actual_current,
        })
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route("/release-note")
def release_note():
    return jsonify(RELEASE_NOTE)


@bp.route("/save", methods=["POST"])
def save():
    data = request.json
    existing_env = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
    old_port = os.getenv("FLASK_PORT", "80")
    changed_port = False
    audio_restart_required = False
    audio_restart_keys = {
        "MIC_PREFERENCE",
        "SPEAKER_PREFERENCE",
        "MIC_TIMEOUT_SECONDS",
        "SILENCE_THRESHOLD",
    }
    for key, value in data.items():
        if key in CONFIG_KEYS:
            if key == "FOLLOW_UP_RETRY_LIMIT":
                try:
                    parsed = int(str(value).strip())
                except (TypeError, ValueError):
                    parsed = 1
                value = str(max(0, min(5, parsed)))
            elif key == "STATUS_LED_BRIGHTNESS":
                try:
                    parsed = float(str(value).strip())
                except (TypeError, ValueError):
                    parsed = 0.2
                value = str(max(0.0, min(1.0, parsed)))
            old_value = str(existing_env.get(key, ""))
            new_value = str(value)
            set_key(ENV_PATH, key, value, quote_mode='never')
            if key == "FLASK_PORT" and str(value) != str(old_port):
                changed_port = True
            if key in audio_restart_keys and new_value != old_value:
                audio_restart_required = True
    response = {"status": "ok"}
    if audio_restart_required:
        response["audio_restart_required"] = True
    if changed_port:
        response["port_changed"] = True
        threading.Thread(target=delayed_restart).start()
    return jsonify(response)


@bp.route("/wifi/status", methods=["GET"])
def wifi_status():
    active = _get_active_wifi_connection()
    active_name = str(active.get("name") or "") if active else ""
    hotspot_active = active_name == WIFI_UNIFIED_HOTSPOT_CON_NAME
    return jsonify({
        "connected": bool(active),
        "ssid": active.get("name") if active else "",
        "device": active.get("device") if active else "",
        "onboarding_active": _is_wifi_onboarding_active(),
        "onboarding_mode": str(getattr(core_config, "WIFI_ONBOARDING_MODE", "legacy")),
        "country": str(getattr(core_config, "WIFI_COUNTRY", "US")),
        "hotspot_active": hotspot_active,
    })


@bp.route("/wifi/networks", methods=["GET"])
def wifi_networks():
    if _is_legacy_wifi_hotspot_onboarding():
        return jsonify({
            "error": "Legacy Wi-Fi setup is active. Open billy.local:8080 to scan and connect without dropping the Billy_Bassistant hotspot."
        }), 409
    try:
        _run_wifi_command(["sudo", "systemctl", "start", "NetworkManager"], check=False)
        _run_wifi_command(["sudo", "nmcli", "radio", "wifi", "on"], check=False)
        result = _run_wifi_command([
            "nmcli",
            "-t",
            "-f",
            "SSID,SIGNAL,SECURITY,IN-USE",
            "dev",
            "wifi",
            "list",
            "ifname",
            "wlan0",
            "--rescan",
            "yes",
        ])
        if result.returncode != 0:
            error = (
                result.stderr.strip() or result.stdout.strip() or "Wi-Fi scan failed"
            )
            return jsonify({"error": error}), 500

        networks: list[dict[str, str | int | bool]] = []
        seen: set[str] = set()
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) < 4:
                continue
            ssid = parts[0].strip()
            if not ssid or ssid in seen:
                continue
            if ssid == WIFI_UNIFIED_HOTSPOT_SSID:
                continue
            seen.add(ssid)
            signal = parts[1].strip()
            security = parts[2].strip()
            in_use = parts[3].strip() == "*"
            try:
                signal_value = int(signal)
            except ValueError:
                signal_value = 0
            networks.append({
                "ssid": ssid,
                "signal": signal_value,
                "security": security or "open",
                "active": in_use,
            })

        networks.sort(
            key=lambda item: (
                not item["active"],
                -int(item["signal"]),
                str(item["ssid"]).lower(),
            )
        )
        return jsonify({"networks": networks})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/wifi/test", methods=["POST"])
def wifi_test():
    if _is_legacy_wifi_hotspot_onboarding():
        return jsonify({
            "ok": False,
            "error": "Legacy Wi-Fi setup is active. Open billy.local:8080 to test the connection without disconnecting from Billy_Bassistant.",
        }), 409
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid") or "").strip()
    password = str(data.get("password") or "")
    country = (
        str(data.get("country") or getattr(core_config, "WIFI_COUNTRY", "US"))
        .strip()
        .upper()
    )
    if not ssid:
        return jsonify({"error": "SSID is required"}), 400

    active_before = _get_active_wifi_connection()
    previous_connection_name = (
        str(active_before.get("name") or "").strip() if active_before else ""
    )
    _cleanup_test_wifi_connections()
    connection_name = f"{WIFI_TEST_PREFIX}{uuid.uuid4().hex[:8]}"
    success, detail = _wifi_connect(
        ssid,
        password,
        country,
        connection_name=connection_name,
        autoconnect=False,
    )
    if not success:
        _delete_wifi_connection(connection_name)
        if previous_connection_name and previous_connection_name != connection_name:
            _activate_wifi_connection(previous_connection_name)
        return jsonify({"ok": False, "error": detail}), 400

    return jsonify({
        "ok": True,
        "message": f"Connected to {ssid}. Save to keep it after reboot.",
        "test_connection": detail,
        "ssid": ssid,
        "country": country,
    })


@bp.route("/wifi/save", methods=["POST"])
def wifi_save():
    if _is_legacy_wifi_hotspot_onboarding():
        return jsonify({
            "ok": False,
            "error": "Legacy Wi-Fi setup is active. Open billy.local:8080 to save Wi-Fi credentials safely.",
        }), 409
    data = request.get_json(silent=True) or {}
    ssid = str(data.get("ssid") or "").strip()
    password = str(data.get("password") or "")
    country = (
        str(data.get("country") or getattr(core_config, "WIFI_COUNTRY", "US"))
        .strip()
        .upper()
    )
    test_connection = str(data.get("test_connection") or "").strip()

    if not ssid:
        return jsonify({"error": "SSID is required"}), 400

    active_before = _get_active_wifi_connection()
    previous_connection_name = (
        str(active_before.get("name") or "").strip() if active_before else ""
    )
    target_name = ssid
    finalize_warnings: list[str] = []

    try:
        if test_connection:
            if (
                active_before
                and active_before.get("name") == target_name
                and target_name != test_connection
            ):
                _delete_wifi_connection(target_name)
            _run_wifi_command([
                "sudo",
                "nmcli",
                "connection",
                "modify",
                test_connection,
                "connection.id",
                target_name,
                "connection.autoconnect",
                "yes",
            ])
            up_result = _run_wifi_command([
                "sudo",
                "nmcli",
                "connection",
                "up",
                target_name,
                "ifname",
                "wlan0",
            ])
            if up_result.returncode != 0:
                if _is_unified_wifi_hotspot_onboarding():
                    _restore_unified_wifi_onboarding_hotspot()
                error = (
                    up_result.stderr.strip()
                    or up_result.stdout.strip()
                    or "Wi-Fi save failed"
                )
                return jsonify({"error": error}), 400
        else:
            attempt_connection_name = (
                f"{WIFI_TEST_PREFIX}{uuid.uuid4().hex[:8]}"
                if previous_connection_name and previous_connection_name == target_name
                else target_name
            )
            success, detail = _wifi_connect(
                ssid,
                password,
                country,
                connection_name=attempt_connection_name,
                autoconnect=True,
            )
            if not success:
                if previous_connection_name and previous_connection_name != target_name:
                    _activate_wifi_connection(previous_connection_name)
                if _is_unified_wifi_hotspot_onboarding():
                    _restore_unified_wifi_onboarding_hotspot()
                return jsonify({"error": detail}), 400

            if attempt_connection_name != target_name:
                _delete_wifi_connection(target_name)
                rename_result = _run_wifi_command([
                    "sudo",
                    "nmcli",
                    "connection",
                    "modify",
                    attempt_connection_name,
                    "connection.id",
                    target_name,
                ])
                if rename_result.returncode != 0:
                    finalize_warnings.append(
                        rename_result.stderr.strip()
                        or rename_result.stdout.strip()
                        or "Wi-Fi connected, but the saved connection name could not be updated"
                    )

        _set_wifi_country(country)
        stop_errors = _stop_wifi_onboarding_services()
        return jsonify({
            "ok": True,
            "ssid": ssid,
            "country": country,
            "onboarding_stopped": True,
            "warnings": stop_errors + finalize_warnings,
        })
    except Exception as exc:
        if _is_unified_wifi_hotspot_onboarding():
            _restore_unified_wifi_onboarding_hotspot()
        return jsonify({"error": str(exc)}), 500


@bp.route("/wakeword/model/upload", methods=["POST"])
@bp.route("/wakeword/keyword/upload", methods=["POST"])
def upload_wakeword_file():
    wakeword_file = request.files.get("keyword_file")
    if wakeword_file is None:
        return jsonify({"error": "No file uploaded"}), 400

    filename = secure_filename(wakeword_file.filename or "")
    if not filename:
        return jsonify({"error": "Invalid filename"}), 400

    suffix = Path(filename).suffix.lower()
    if suffix not in {".ppn", ".onnx"}:
        return jsonify({"error": "Only .ppn and .onnx files are supported"}), 400

    target_rel_dir = WAKEWORD_REL_ROOT
    target_dir = Path(PROJECT_ROOT) / target_rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    try:
        wakeword_file.save(target_path)
        if suffix == ".ppn":
            set_key(
                ENV_PATH,
                "WAKE_WORD_PORCUPINE_KEYWORD_PATH",
                filename,
                quote_mode='never',
            )
            set_key(ENV_PATH, "WAKE_WORD_BACKEND", "porcupine", quote_mode='never')
            backend = "porcupine"
            file_type = "keyword"
        else:
            set_key(
                ENV_PATH,
                "WAKE_WORD_OPENWAKEWORD_MODEL_PATH",
                filename,
                quote_mode='never',
            )
            set_key(ENV_PATH, "WAKE_WORD_BACKEND", "openwakeword", quote_mode='never')
            backend = "openwakeword"
            file_type = "model"
        return jsonify({
            "status": "uploaded",
            "backend": backend,
            "file_type": file_type,
            "filename": filename,
            "keyword_path": filename,
        })
    except Exception as e:
        logger.warning(f"[wakeword] Upload failed: {e}", "⚠️")
        return jsonify({"error": f"Upload failed: {e}"}), 500


@bp.route("/wakeword/keywords", methods=["GET"])
def list_wakeword_keywords():
    return jsonify({
        "keywords": _list_available_wakeword_files(".ppn"),
        "models": _list_available_wakeword_files(".onnx"),
    })


@bp.route("/camera/devices", methods=["GET"])
def list_camera_devices():
    rpi_available = _detect_rpi_camera_available()
    usb_nodes = _detect_usb_video_nodes()

    options: list[dict[str, str]] = [{"value": "none", "label": "None"}]
    if rpi_available:
        options.append({
            "value": "rpi_camera",
            "label": "Raspberry Pi Camera Module",
        })
    for node in usb_nodes:
        options.append({
            "value": f"usb_webcam:{node['index']}",
            "label": str(node["label"]),
        })

    return jsonify({
        "options": options,
        "detected": {
            "rpi_camera": rpi_available,
            "usb_webcams": usb_nodes,
        },
    })


@bp.route("/camera/preview", methods=["POST"])
def camera_preview():
    data = request.get_json(silent=True) or {}
    selection = str(data.get("selection") or "none").strip().lower()
    rotation = data.get("rotation", 0)

    camera_hardware = "none"
    camera_device_index = 0
    if selection == "rpi_camera":
        camera_hardware = "rpi_camera"
    elif selection.startswith("usb_webcam:"):
        camera_hardware = "usb_webcam"
        suffix = selection.split(":", 1)[1].strip()
        if suffix.isdigit():
            camera_device_index = int(suffix)
    elif selection == "usb_webcam":
        camera_hardware = "usb_webcam"

    result = describe_scene({
        "camera_hardware": camera_hardware,
        "camera_device_index": camera_device_index,
        "camera_rotation": rotation,
        "capture_timeout_seconds": 4,
        "usb_scan_fallback": False,
        "fresh_frame": True,
    })
    if not result.get("ok"):
        return jsonify({
            "ok": False,
            "error": result.get("error") or "Camera preview failed",
        }), 400

    image_url = str(result.get("image_url") or "")
    if not image_url.startswith("data:image/jpeg;base64,"):
        return jsonify({
            "ok": False,
            "error": "Camera preview did not return an image",
        }), 500

    return jsonify({
        "ok": True,
        "image_url": image_url,
        "bytes": len(b64decode(image_url.split(",", 1)[1])),
    })


@bp.route("/config")
def get_config():
    # Reload .env file and core config to get latest values
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH, override=True)

    # Reload core config module to pick up new .env values
    import importlib
    import sys

    if 'core.config' in sys.modules:
        importlib.reload(sys.modules['core.config'])

    # Re-import core_config to get fresh values
    from ..core_imports import core_config

    # Get basic configuration
    config_data = {k: str(getattr(core_config, k, "")) for k in CONFIG_KEYS}

    # Add voice options from the current provider
    try:
        current_provider = voice_provider_registry.get_provider()
        voices = current_provider.get_supported_voices()
    except Exception as e:
        logger.warning(f"[config] No realtime provider available: {e}")
        voices = []
    config_data["VOICE_OPTIONS"] = voices

    # Add user profile information
    try:
        from core.config import DEFAULT_USER
        from core.profile_manager import user_manager

        # Get current user from .env file (already reloaded above)
        current_user_name = (
            os.getenv("CURRENT_USER", "").strip().strip("'\"").lower()
        )  # Remove quotes, whitespace, and normalize to lowercase
        current_user = None

        # Update config_data with fresh CURRENT_USER value
        config_data["CURRENT_USER"] = current_user_name

        # If we have a current user in .env, try to load it
        if current_user_name and current_user_name.lower() != "guest":
            try:
                current_user = user_manager.identify_user(current_user_name, "high")
            except Exception as e:
                print(f"Failed to load current user {current_user_name}: {e}")

        # Only fall back to DEFAULT_USER if CURRENT_USER is completely empty (not set)
        # If CURRENT_USER is explicitly set to "guest", respect that choice
        if (
            not current_user
            and not current_user_name
            and DEFAULT_USER
            and DEFAULT_USER.lower() != "guest"
        ):
            try:
                current_user = user_manager.identify_user(DEFAULT_USER, "high")
                # Only update CURRENT_USER in .env if it was completely empty
                config_data["CURRENT_USER"] = DEFAULT_USER
            except Exception as e:
                print(f"Failed to load default user {DEFAULT_USER}: {e}")

        # Add user profile data
        if current_user:
            config_data["CURRENT_USER"] = {
                "name": current_user.name,
                "data": current_user.data,
                "memories": current_user.get_memories(10),
                "context": current_user.get_context_string(),
            }
        else:
            # If no user is loaded, preserve the CURRENT_USER value from .env
            # This could be "guest" or a user name that couldn't be loaded
            config_data["CURRENT_USER"] = (
                current_user_name if current_user_name else None
            )

        # Add available profiles with full data including preferred personas
        try:
            from core.profile_manager import UserProfile

            available_profiles = []
            for user_name in user_manager.list_all_users():
                try:
                    profile = UserProfile(user_name)
                    available_profiles.append({
                        "name": profile.name,
                        "data": profile.data,
                    })
                except Exception as e:
                    print(f"Failed to load profile {user_name}: {e}")
                    # Add basic info even if full profile can't be loaded
                    available_profiles.append({
                        "name": user_name,
                        "data": {"USER_INFO": {"preferred_persona": "default"}},
                    })
            config_data["AVAILABLE_PROFILES"] = available_profiles
        except Exception as e:
            print(f"Failed to load profile data: {e}")
            config_data["AVAILABLE_PROFILES"] = []

        # Add available personas and current persona
        try:
            from core.persona_manager import persona_manager

            config_data["AVAILABLE_PERSONAS"] = persona_manager.get_available_personas()
            config_data["CURRENT_PERSONA"] = persona_manager.current_persona
        except Exception as e:
            print(f"Failed to load personas: {e}")
            config_data["AVAILABLE_PERSONAS"] = []
            config_data["CURRENT_PERSONA"] = "default"

    except Exception as e:
        print(f"Failed to load user profile data: {e}")
        config_data["CURRENT_USER"] = None
        config_data["AVAILABLE_PROFILES"] = []
        config_data["AVAILABLE_PERSONAS"] = []

    return jsonify(config_data)


@bp.route("/profiles/current-user", methods=["PATCH"])
def update_current_user_profile():
    """Update the current user's profile settings."""
    try:
        from core.persona_manager import persona_manager
        from core.profile_manager import user_manager

        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        current_user = user_manager.get_current_user()
        if not current_user:
            return jsonify({"error": "No current user"}), 400

        # Handle persona switching
        if data.get("action") == "switch_persona":
            new_persona = data.get("preferred_persona")
            if new_persona:
                # Update user's preferred persona
                current_user.data['USER_INFO']['preferred_persona'] = new_persona
                current_user._save_profile()

                # Switch persona manager to new persona
                persona_manager.switch_persona(new_persona)

                return jsonify({
                    "success": True,
                    "message": f"Switched to {new_persona} persona",
                })

        return jsonify({"error": "Invalid action"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/config/refresh", methods=["POST"])
def refresh_config():
    """Refresh core configuration modules to pick up new settings."""
    try:
        # Reload .env file first
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=True)

        # Import and reload core modules that might have cached configuration
        import importlib
        import sys

        # Reload core modules that contain configuration
        modules_to_reload = [
            'core.config',
            'core.personality',
            'core.wakeup',
            'core.say',
            'core.audio',
        ]

        for module_name in modules_to_reload:
            if module_name in sys.modules:
                importlib.reload(sys.modules[module_name])

        # Also reload the core_imports to get fresh references
        if 'app.core_imports' in sys.modules:
            importlib.reload(sys.modules['app.core_imports'])

        return jsonify({"status": "ok", "message": "Configuration refreshed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/config/auto-refresh", methods=["POST"])
def auto_refresh_config():
    """Automatically refresh configuration when .env changes are detected."""
    try:
        # Reload .env file
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=True)

        # Reload core config module
        import importlib
        import sys

        if 'core.config' in sys.modules:
            importlib.reload(sys.modules['core.config'])

        # Get updated configuration
        from ..core_imports import core_config

        # Return updated config data
        config_data = {k: str(getattr(core_config, k, "")) for k in CONFIG_KEYS}

        # Add user profile information
        try:
            from core.config import DEFAULT_USER
            from core.profile_manager import user_manager

            # Get current user from .env file (fresh read)
            current_user_name = os.getenv("CURRENT_USER", "").strip().strip("'\"")
            current_user = None

            # Update config_data with fresh CURRENT_USER value
            config_data["CURRENT_USER"] = current_user_name

            # If we have a current user in .env, try to load it
            if current_user_name and current_user_name.lower() != "guest":
                try:
                    current_user = user_manager.identify_user(current_user_name, "high")
                except Exception as e:
                    print(f"Failed to load current user {current_user_name}: {e}")

            # Only fall back to DEFAULT_USER if CURRENT_USER is completely empty
            if (
                not current_user
                and not current_user_name
                and DEFAULT_USER
                and DEFAULT_USER.lower() != "guest"
            ):
                try:
                    current_user = user_manager.identify_user(DEFAULT_USER, "high")
                    config_data["CURRENT_USER"] = DEFAULT_USER
                except Exception as e:
                    print(f"Failed to load default user {DEFAULT_USER}: {e}")

            # Add user profile data
            if current_user:
                config_data["CURRENT_USER"] = {
                    "name": current_user.name,
                    "data": current_user.data,
                    "memories": current_user.get_memories(10),
                    "context": current_user.get_context_string(),
                }
            else:
                config_data["CURRENT_USER"] = (
                    current_user_name if current_user_name else None
                )

            # Add available profiles with full data including preferred personas
            try:
                from core.profile_manager import UserProfile

                available_profiles = []
                for user_name in user_manager.list_all_users():
                    try:
                        profile = UserProfile(user_name)
                        available_profiles.append({
                            "name": profile.name,
                            "data": profile.data,
                        })
                    except Exception as e:
                        print(f"Failed to load profile {user_name}: {e}")
                        # Add basic info even if full profile can't be loaded
                        available_profiles.append({
                            "name": user_name,
                            "data": {"USER_INFO": {"preferred_persona": "default"}},
                        })
                config_data["AVAILABLE_PROFILES"] = available_profiles
            except Exception as e:
                print(f"Failed to load profile data: {e}")
                config_data["AVAILABLE_PROFILES"] = []

            # Add available personas
            try:
                from core.persona_manager import persona_manager

                config_data["AVAILABLE_PERSONAS"] = (
                    persona_manager.get_available_personas()
                )
            except Exception as e:
                print(f"Failed to load personas: {e}")
                config_data["AVAILABLE_PERSONAS"] = []

        except Exception as e:
            print(f"Failed to load user profile data: {e}")
            config_data["CURRENT_USER"] = None
            config_data["AVAILABLE_PROFILES"] = []
            config_data["AVAILABLE_PERSONAS"] = []

        return jsonify({
            "status": "ok",
            "message": "Configuration auto-refreshed",
            "config": config_data,
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@bp.route("/news/sources", methods=["GET"])
def get_news_sources():
    try:
        return jsonify({"sources": load_news_sources()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/news/sources", methods=["POST"])
def add_news_source():
    data = request.get_json(silent=True) or {}
    try:
        normalized = _normalize_source_payload(data)
        sources = load_news_sources()
        normalized["id"] = str(data.get("id") or f"src-{uuid.uuid4().hex[:10]}")
        if any(source.get("id") == normalized["id"] for source in sources):
            return jsonify({"error": "Source id already exists"}), 409
        sources.append(normalized)
        saved = save_news_sources(sources)
        return jsonify({"status": "ok", "sources": saved})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/news/sources/<source_id>", methods=["PATCH"])
def update_news_source(source_id: str):
    data = request.get_json(silent=True) or {}
    try:
        sources = load_news_sources()
        source = next((src for src in sources if src.get("id") == source_id), None)
        if not source:
            return jsonify({"error": "Source not found"}), 404

        merged = dict(source)
        merged.update({
            "name": data.get("name", source.get("name")),
            "url": data.get("url", source.get("url")),
            "topics": data.get("topics", source.get("topics", [])),
        })
        normalized = _normalize_source_payload(merged)
        normalized["id"] = source_id

        updated_sources = [
            normalized if src.get("id") == source_id else src for src in sources
        ]
        saved = save_news_sources(updated_sources)
        return jsonify({"status": "ok", "sources": saved})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/news/sources/<source_id>", methods=["DELETE"])
def delete_news_source(source_id: str):
    try:
        sources = load_news_sources()
        filtered = [source for source in sources if source.get("id") != source_id]
        if len(filtered) == len(sources):
            return jsonify({"error": "Source not found"}), 404
        saved = save_news_sources(filtered)
        return jsonify({"status": "ok", "sources": saved})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/get-env')
def get_env():
    try:
        with open('.env') as f:
            return f.read(), 200
    except Exception as e:
        return str(e), 500


@bp.route('/save-env', methods=['POST'])
def save_env():
    content = request.json.get('content', '')
    try:
        with open('.env', 'w') as f:
            f.write(content)
        return jsonify({"status": "ok", "message": ".env saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/factory-reset", methods=["POST"])
def factory_reset():
    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return jsonify({"status": "error", "error": "Confirmation required"}), 400
    options = data.get("options") or {}
    env_enabled = options.get("env", True)
    profiles_enabled = options.get("profiles", True)
    personas_enabled = options.get("personas", True)
    logs_enabled = options.get("logs", True)
    wifi_enabled = options.get("wifi", True)
    git_enabled = options.get("git", True)
    reboot_enabled = options.get("reboot", True)

    env_results = (
        _remove_env_and_versions() if env_enabled else {"removed": {}, "errors": []}
    )
    profile_results = (
        _remove_custom_profiles() if profiles_enabled else {"removed": [], "errors": []}
    )
    persona_results = (
        _remove_custom_personas() if personas_enabled else {"removed": [], "errors": []}
    )
    log_results = _clear_service_logs() if logs_enabled else {"errors": []}
    git_results = (
        _reset_git_worktree() if git_enabled else {"errors": [], "details": {}}
    )
    wifi_results = (
        _remove_active_wifi_connections()
        if wifi_enabled
        else {"removed": [], "errors": []}
    )

    errors = (
        env_results.get("errors", [])
        + profile_results.get("errors", [])
        + persona_results.get("errors", [])
        + log_results.get("errors", [])
        + wifi_results.get("errors", [])
        + git_results.get("errors", [])
    )

    response = {
        "status": "ok" if not errors else "partial",
        "removed": {
            "env": env_results.get("removed", {}).get("env", False),
            "versions": env_results.get("removed", {}).get("versions", False),
            "profiles": profile_results.get("removed", []),
            "personas": persona_results.get("removed", []),
        },
        "logs_cleared": logs_enabled and not log_results.get("errors"),
        "wifi_removed": wifi_results.get("removed", []),
        "git_reset": git_enabled and not git_results.get("errors"),
        "git_details": git_results.get("details", {}),
        "requested": {
            "env": env_enabled,
            "profiles": profiles_enabled,
            "personas": personas_enabled,
            "logs": logs_enabled,
            "wifi": wifi_enabled,
            "git": git_enabled,
            "reboot": reboot_enabled,
        },
        "errors": errors,
    }
    logger.info(f"[factory_reset] Completed with status: {response['status']}")
    if reboot_enabled and response["status"] == "ok":
        try:
            threading.Thread(
                target=lambda: (
                    time.sleep(2),
                    subprocess.Popen(["sudo", "reboot"]),
                )
            ).start()
            response["rebooting"] = True
        except Exception as exc:
            response["rebooting"] = False
            response["errors"].append(f"Failed to reboot: {exc}")
    else:
        response["rebooting"] = False
        response["restarting_services"] = response["status"] == "ok"
    return jsonify(response)


@bp.route("/hostname", methods=["GET", "POST"])
def hostname():
    if request.method == "GET":
        return jsonify({"hostname": os.uname().nodename})
    if request.method == "POST":
        data = request.get_json()
        new_hostname = data.get("hostname", "").strip()
        if not new_hostname:
            return jsonify({"error": "Invalid hostname"}), 400
        try:
            subprocess.check_call(["sudo", "hostnamectl", "set-hostname", new_hostname])
            subprocess.run(["sudo", "systemctl", "restart", "avahi-daemon"])
            return jsonify({"status": "ok", "hostname": new_hostname})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Unsupported method"}), 405
