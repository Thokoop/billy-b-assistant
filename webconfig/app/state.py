import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from packaging.version import InvalidVersion
from packaging.version import parse as parse_version


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from billy_runtime.paths import CODE_ROOT, INSTALL_ROOT
from billy_runtime.release_policy import newest_release, release_version, update_channel
from core.logger import logger


WEBCONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PROJECT_ROOT = INSTALL_ROOT
PERSONA_PATH = PROJECT_ROOT / "persona.ini"
VERSIONS_PATH = PROJECT_ROOT / "versions.ini"
WAKE_UP_DIR = PROJECT_ROOT / "sounds" / "wake-up" / "custom"
WAKE_UP_DIR_DEFAULT = CODE_ROOT / "sounds" / "wake-up" / "default"

RELEASE_NOTE = {"tag": None, "body": "", "url": "", "fetched_at": 0}
VERSION_STATE_LOCK = threading.RLock()
KNOWN_RELEASE_TAGS = ()
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


def available_version(current, stored):
    """Apply today's preference even when versions.ini was saved on another channel."""
    with VERSION_STATE_LOCK:
        tags = (*KNOWN_RELEASE_TAGS, stored)
    latest = newest_release(tags)
    installed = release_version(current)
    if not latest or installed is None or release_version(latest) <= installed:
        return current
    return latest


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
    selected = os.environ.get("BILLY_RELEASE_VERSION")
    if selected:
        return selected
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
    global KNOWN_RELEASE_TAGS
    try:
        tags = []
        # A busy beta channel must not hide stable releases beyond page one.
        # Keep the background lookup bounded; no network work on /version.
        for page in range(1, 11):
            data = _fetch_github_json(
                f"https://api.github.com/repos/Thokoop/Billy-B-assistant/tags?per_page=100&page={page}"
            )
            if not isinstance(data, list):
                logger.warning("[fetch_latest_tag] Invalid GitHub tag response")
                return None
            tags.extend(item.get('name') for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
        with VERSION_STATE_LOCK:
            KNOWN_RELEASE_TAGS = tuple(tag for tag in tags if release_version(tag))
        return newest_release(tags)
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
    channel = update_channel()
    latest = fetch_latest_tag()
    current = get_current_version()
    if not latest:
        try:
            latest = load_versions()["version"].get("latest")
        except Exception:
            latest = None
    latest = available_version(current, latest)
    save_versions(current, latest)
    try:
        versions_cfg = load_versions()
        tag_for_notes = versions_cfg["version"].get("latest") or versions_cfg[
            "version"
        ].get("current")
        if tag_for_notes:
            note = fetch_release_note_for_tag(tag_for_notes)
            if note and channel == update_channel():
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
