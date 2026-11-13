"""A device's names and images (billy_runtime/theme.py and the /theme/ route)."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from billy_runtime import theme


ROOT = Path(__file__).resolve().parents[1]


def install(root: Path, name="demo", theme_json=None, files=()):
    """An extension device in <root>/private/<name> with an optional theme."""
    folder = root / "private" / name
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text("")
    if theme_json is not None or files:
        (folder / "theme").mkdir()
    if theme_json is not None:
        text = theme_json if isinstance(theme_json, str) else json.dumps(theme_json)
        (folder / "theme" / "theme.json").write_text(text)
    for relative, content in files:
        path = folder / "theme" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return folder


def test_the_fish_keeps_billys_names(tmp_path):
    assert theme.theme_dir(tmp_path) is None
    assert theme.load(tmp_path) == theme.DEFAULTS
    assert theme.load(tmp_path)["hotspot_ssid"] == "Billy_Bassistant"


def test_a_device_overrides_only_what_it_names(tmp_path):
    install(
        tmp_path,
        theme_json={"product_name": "Demo Product", "hotspot_ssid": "Demo_Setup"},
    )
    current = theme.load(tmp_path)
    assert current["product_name"] == "Demo Product"
    assert current["hotspot_ssid"] == "Demo_Setup"
    assert current["name"] == "Billy"  # left out: the default


def test_the_configured_device_wins_over_the_only_one(tmp_path):
    install(tmp_path, "first", theme_json={"product_name": "First"})
    install(tmp_path, "second", theme_json={"product_name": "Second"})
    assert theme.load(tmp_path)["product_name"] == "Billy Bassistant"  # two: the fish
    (tmp_path / ".env").write_text("BILLY_DEVICE=private.second.device:Device\n")
    assert theme.load(tmp_path)["product_name"] == "Second"
    (tmp_path / ".env").write_text("BILLY_DEVICE=fish\n")
    assert theme.theme_dir(tmp_path) is None


@pytest.mark.parametrize(
    "theme_json",
    [
        "{not json",
        "[1, 2]",
        {"hotspot_ssid": "x" * 33},
        {"hotspot_ssid": "evil\"; rm -rf /"},
        {"product_name": ""},
        {"name": "two\nlines"},
        {"name": 42},
        {"persona_icon": "set meal; color:red"},
    ],
)
def test_invalid_values_fall_back_to_billy(tmp_path, theme_json):
    install(tmp_path, theme_json=theme_json)
    assert theme.load(tmp_path) == theme.DEFAULTS


def test_images_come_from_the_theme_folder_only(tmp_path):
    install(tmp_path, theme_json={}, files=[("images/status-active.png", b"png")])
    (tmp_path / "private" / "secret.txt").write_text("no")
    assert theme.asset("images/status-active.png", tmp_path).read_bytes() == b"png"
    assert theme.asset("images/status-inactive.png", tmp_path) is None
    assert theme.asset("../../secret.txt", tmp_path) is None
    assert theme.asset("images/../../__init__.py", tmp_path) is None


def test_persona_presets_of_the_device(tmp_path):
    assert theme.persona_presets(tmp_path) is None
    install(
        tmp_path, theme_json={}, files=[("personas/caller/persona.ini", b"[meta]\n")]
    )
    assert theme.persona_presets(tmp_path).name == "personas"


def test_display_name_is_the_hostname(monkeypatch):
    monkeypatch.setattr(
        os, "uname", lambda: os.uname_result(("", "billyphone", "", "", ""))
    )
    assert theme.display_name() == "Billyphone"
    monkeypatch.setattr(
        os, "uname", lambda: os.uname_result(("", "raspberrypi", "", "", ""))
    )
    assert theme.display_name({"name": "Demo"}) == "Demo"


def test_command_line_prints_one_value(tmp_path):
    install(tmp_path, theme_json={"hotspot_ssid": "Demo_Setup"})
    run = subprocess.run(
        [sys.executable, "-m", "billy_runtime.theme", "hotspot_ssid"],
        cwd=ROOT,
        env={**os.environ, "BILLY_INSTALL_ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout == "Demo_Setup\n"
    assert theme.main(["unknown"]) == 2


def test_hotspot_script_asks_the_theme_for_its_name(tmp_path):
    # wifi_check.sh reads the SSID through theme_value, with a fallback.
    install(tmp_path, theme_json={"hotspot_ssid": "Demo_Setup"})
    script = (ROOT / "setup" / "wifi_check.sh").read_text()
    function = script[
        script.index("theme_value() {") : script.index("UNIFIED_HOTSPOT_SSID=")
    ]
    run = subprocess.run(
        ["bash", "-c", function + 'theme_value hotspot_ssid'],
        env={
            **os.environ,
            "PROJECT_ROOT": str(ROOT),
            "BILLY_INSTALL_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert run.stdout.strip() == "Demo_Setup"
    assert 'UNIFIED_HOTSPOT_SSID="Billy_Bassistant"' in script  # the fallback


def test_theme_route_serves_the_device_image_else_the_core_one(tmp_path, monkeypatch):
    from flask import Flask

    sys.path.insert(0, str(ROOT / "application"))
    from webconfig.app import theme as web_theme

    install(tmp_path, theme_json={}, files=[("images/status-active.png", b"device")])
    monkeypatch.setattr(theme, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(
        theme, "theme_dir", lambda root=None, _real=theme.theme_dir: _real(tmp_path)
    )
    static = tmp_path / "static" / "images"
    static.mkdir(parents=True)
    (static / "status-active.png").write_bytes(b"core")
    (static / "status-inactive.png").write_bytes(b"core-inactive")
    app = Flask("theme-test", static_folder=str(tmp_path / "static"))
    app.register_blueprint(web_theme.bp)
    client = app.test_client()
    assert client.get("/theme/images/status-active.png").data == b"device"
    assert client.get("/theme/images/status-inactive.png").data == b"core-inactive"
    assert client.get("/theme/images/missing.png").status_code == 404


ALLOWED = re.compile(
    r"default\('Billy[^']*'\)|<!--.*?-->|\{#.*?#\}|Billy Project Releases"
    r"|X-Billy-Update|//.*$|console\.\w+\(.*\)|'None \(Billy fish\)'"
    r"|\|\| 'Billy'|'Billy';$|this Billy by"
)


@pytest.mark.parametrize("pattern", ["templates/**/*.html", "static/js/*.js"])
def test_no_hard_coded_billy_in_text_the_user_reads(pattern):
    # docs/design-system.md 4.5: the name comes from the hostname (theme.py).
    web = ROOT / "application" / "webconfig"
    found = []
    for path in sorted(web.glob(pattern)):
        if path.name.endswith(".min.js") or path.name == "marked-min.js":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bBilly\b", ALLOWED.sub("", line)):
                found.append(f"{path.relative_to(web)}:{number}: {line.strip()[:80]}")
    assert not found, "\n".join(found)
