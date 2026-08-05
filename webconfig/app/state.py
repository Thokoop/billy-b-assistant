import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from packaging.version import InvalidVersion
from packaging.version import parse as parse_version


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from core.logger import logger


WEBCONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = Path(os.path.abspath(os.path.join(WEBCONFIG_DIR, "..")))
PERSONA_PATH = PROJECT_ROOT / "persona.ini"
VERSIONS_PATH = PROJECT_ROOT / "versions.ini"
WAKE_UP_DIR = PROJECT_ROOT / "sounds" / "wake-up" / "custom"
WAKE_UP_DIR_DEFAULT = PROJECT_ROOT / "sounds" / "wake-up" / "default"

RELEASE_NOTE = {"tag": None, "body": "", "url": "", "fetched_at": 0}
VERSION_STATE_LOCK = threading.RLock()
GITHUB_CONNECT_TIMEOUT_SECONDS = 2
GITHUB_REQUEST_TIMEOUT_SECONDS = 4


def _fetch_github_json(url: str):
    """Fetch GitHub metadata without ever holding startup indefinitely."""
    output = subprocess.check_output(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            str(GITHUB_CONNECT_TIMEOUT_SECONDS),
            "--max-time",
            str(GITHUB_REQUEST_TIMEOUT_SECONDS),
            url,
        ],
        text=True,
        timeout=GITHUB_REQUEST_TIMEOUT_SECONDS + 1,
    )
    return json.loads(output)


def _show_rc_versions_enabled() -> bool:
    """Return whether prerelease/RC tags should be considered."""
    return os.getenv("SHOW_RC_VERSIONS", "false").strip().lower() == "true"


def load_versions():
    with VERSION_STATE_LOCK:
        config = configparser.ConfigParser()
        if not os.path.exists(VERSIONS_PATH):
            example_path = os.path.join(PROJECT_ROOT, "versions.ini.example")
            if os.path.exists(example_path):
                shutil.copy(example_path, VERSIONS_PATH)
            else:
                config["version"] = {"current": "unknown", "latest": "unknown"}
                with open(VERSIONS_PATH, "w") as f:
                    config.write(f)
        config.read(VERSIONS_PATH)
        return config


def save_versions(current: str, latest: str):
    if not current or not latest:
        logger.warning("[save_versions] Refusing to save empty version")
        return
    try:
        parsed_current = parse_version(current.lstrip("v"))
        parsed_latest = parse_version(latest.lstrip("v"))
    except InvalidVersion as e:
        logger.warning(f"[save_versions] Invalid version: {e}")
        return
    if parsed_latest < parsed_current:
        logger.warning(
            f"[save_versions] Skipping downgrade from {parsed_current} to {parsed_latest}"
        )
        latest = current
    with VERSION_STATE_LOCK:
        config = configparser.ConfigParser()
        config["version"] = {"current": current, "latest": latest}
        with open(VERSIONS_PATH, "w") as f:
            config.write(f)


def get_current_version():
    tag_list: list[str] = []
    try:
        # First, check if HEAD points to a tag directly (most reliable for detached HEAD)
        tags = subprocess.check_output(
            ["git", "tag", "--points-at", "HEAD"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if tags:
            # If multiple tags point to HEAD, return the one with highest version
            tag_list = [t.strip() for t in tags.split('\n') if t.strip()]
        if tag_list:
            # Sort by version and return the highest
            try:
                tag_list.sort(key=lambda v: parse_version(v.lstrip("v")), reverse=True)
                result = tag_list[0]
                logger.verbose(
                    f"[get_current_version] Found tag via --points-at: {result}"
                )
                return result
            except Exception:
                # If version parsing fails, just return the first one
                result = tag_list[0]
                logger.verbose(
                    f"[get_current_version] Found tag via --points-at (unparsed): {result}"
                )
                return result
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        logger.debug(f"[get_current_version] Error checking --points-at: {e}")

    try:
        # Try to get exact tag match
        result = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if result:
            logger.verbose(
                f"[get_current_version] Found tag via --exact-match: {result}"
            )
            return result
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        logger.debug(f"[get_current_version] Error checking --exact-match: {e}")

    try:
        # Try to get the nearest tag (with distance if not exact)
        result = subprocess.check_output(
            ["git", "describe", "--tags"],
            cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if result:
            # If exact match: "v2.0.1"
            # If ahead of a tag: "v2.0.1-5-gabc123"
            m = re.match(r"^(?P<tag>.+)-\d+-g[0-9a-fA-F]+$", result)
            if m:
                tag_only = m.group("tag")
                logger.verbose(
                    f"[get_current_version] Found via --tags (normalized to nearest tag): {tag_only}"
                )
                return tag_only
            logger.verbose(f"[get_current_version] Found via --tags: {result}")
            return result
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        logger.debug(f"[get_current_version] Error checking --tags: {e}")

    # Last resort: return commit hash
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
        result = f"(commit {commit})"
        logger.verbose(f"[get_current_version] Using commit hash: {result}")
        return result
    except Exception as e:
        logger.warning(f"[get_current_version] Failed: {e}")
        return "unknown"


def fetch_latest_tag():
    try:
        data = _fetch_github_json(
            "https://api.github.com/repos/Thokoop/Billy-B-assistant/tags"
        )
        if isinstance(data, dict) and data.get("message"):
            logger.warning(f"[fetch_latest_tag] GitHub error: {data['message']}")
            return None
        if not isinstance(data, list):
            logger.warning("[fetch_latest_tag] Unexpected response format")
            return None
        filtered = [tag["name"] for tag in data if "name" in tag]
        include_prerelease = _show_rc_versions_enabled()
        valid_tags: list[str] = []
        for tag_name in filtered:
            try:
                parsed = parse_version(tag_name.lstrip("v"))
                if parsed.is_prerelease and not include_prerelease:
                    logger.verbose(
                        f"[fetch_latest_tag] Skipping prerelease tag (SHOW_RC_VERSIONS=false): {tag_name}"
                    )
                    continue
                valid_tags.append(tag_name)
            except InvalidVersion:
                logger.verbose(
                    f"[fetch_latest_tag] Skipping non-semver tag: {tag_name}"
                )

        if valid_tags:
            return max(valid_tags, key=lambda v: parse_version(v.lstrip("v")))
        logger.warning("[fetch_latest_tag] No tags found")
        return None
    except Exception as e:
        logger.warning(f"[fetch_latest_tag] Exception: {e}")
        return None


def fetch_release_note_for_tag(tag: str):
    try:
        data = _fetch_github_json(
            f"https://api.github.com/repos/Thokoop/billy-b-assistant/releases/tags/{tag}"
        )
        if isinstance(data, dict) and data.get("body"):
            return {
                "tag": data.get("tag_name") or tag,
                "body": data.get("body") or "",
                "url": data.get("html_url") or "",
            }
        return None
    except Exception as e:
        logger.warning(f"[fetch_release_note_for_tag] Exception: {e}")
        return None


def bootstrap_versions_and_release_note():
    latest = fetch_latest_tag()
    current = get_current_version()
    if not latest:
        try:
            latest = load_versions()["version"].get("latest")
        except Exception:
            latest = None
    if not latest:
        latest = current
    save_versions(current, latest)
    try:
        versions_cfg = load_versions()
        tag_for_notes = versions_cfg["version"].get("latest") or versions_cfg[
            "version"
        ].get("current")
        if tag_for_notes:
            note = fetch_release_note_for_tag(tag_for_notes)
            if note:
                RELEASE_NOTE.update(note)
                RELEASE_NOTE["fetched_at"] = int(time.time())
                logger.info(f"[release-note] Cached notes for {RELEASE_NOTE['tag']}")
            else:
                logger.verbose(
                    f"[release-note] No notes found for tag: {tag_for_notes}"
                )
        else:
            logger.verbose("[release-note] No tag available to fetch notes.")
    except Exception as e:
        logger.warning(f"[release-note] Bootstrap failed: {e}")
