"""Ringing for incoming Bluetooth calls: the ringtone list, the ringer, and
a button press answering the call."""

import json
import sys
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def ringtones(isolated, monkeypatch, tmp_path):
    module = isolated.load("bluetooth.ringtones")
    defaults = tmp_path / "code" / "ringtones"
    defaults.mkdir(parents=True)
    for number in range(1, 6):
        with wave.open(str(defaults / f"{number}.wav"), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(b"\0\0" * 240)
    user = tmp_path / "data" / "ringtones"
    monkeypatch.setattr(module, "DEFAULT_DIR", defaults)
    monkeypatch.setattr(module, "USER_DIR", user)
    monkeypatch.setattr(module, "MANIFEST", user / "ringtones.json")
    return module


def ids(module):
    return [entry["id"] for entry in module.entries()]


def test_the_list_starts_with_the_five_built_in_tones(ringtones):
    entries = ringtones.entries()
    assert [e["id"] for e in entries] == [f"default-{n}" for n in range(1, 6)]
    assert all(e["has_audio"] for e in entries)


def test_ringtones_can_be_added_and_removed_any_number(ringtones):
    for _ in range(3):
        ringtones.add(phrase="Ring ring!")
    assert ids(ringtones)[-3:] == ["tone-1", "tone-2", "tone-3"]
    for number in range(1, 6):
        ringtones.remove(f"default-{number}")
    ringtones.remove("tone-2")
    assert ids(ringtones) == ["tone-1", "tone-3"]
    # A tone without audio yet is never picked.
    assert ringtones.pick() is None


def test_restore_brings_back_every_removed_built_in_tone(ringtones):
    ringtones.remove("default-2")
    ringtones.remove("default-4")
    ringtones.add(phrase="Mine")
    assert ringtones.restore_defaults() == 2
    assert set(ids(ringtones)) == {f"default-{n}" for n in range(1, 6)} | {"tone-1"}
    assert ringtones.restore_defaults() == 0


def test_new_audio_for_a_built_in_tone_becomes_the_users_own(ringtones):
    entry = ringtones.save_audio("default-3", b"\1\0" * 2400, name="My tone")
    assert entry["kind"] == "custom" and entry["id"].startswith("tone-")
    assert "default-3" not in ids(ringtones)
    assert ringtones.path_for(entry).parent == ringtones.USER_DIR
    with wave.open(str(ringtones.path_for(entry))) as wf:
        assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (
            24000,
            1,
            2,
        )
    # The built-in file is untouched, so restore brings the original back.
    assert ringtones.restore_defaults() == 1


def test_removing_a_users_tone_deletes_its_audio(ringtones):
    entry = ringtones.add(phrase="Hello")
    entry = ringtones.save_audio(entry["id"], b"\0\0" * 10)
    path = ringtones.path_for(entry)
    assert path.is_file()
    ringtones.remove(entry["id"])
    assert not path.exists()
    assert json.loads(ringtones.MANIFEST.read_text())["ringtones"]


@pytest.fixture
def ringer(isolated, monkeypatch):
    module = isolated.load("bluetooth.ringer")
    started = []
    monkeypatch.setattr(module, "_start", lambda: started.append(True))
    module.started = started
    return module


def test_the_ringer_follows_the_phones_call_state(ringer):
    assert not ringer.call_ringing()
    ringer.update(True)
    assert ringer.call_ringing() and ringer.started == [True]
    ringer.update(True)  # still ringing: no second ringer
    assert ringer.started == [True]
    ringer.update(False)
    assert not ringer.call_ringing()


def test_a_stale_ringing_state_does_not_count(ringer, monkeypatch):
    ringer.update(True)
    now = ringer.time.monotonic()
    monkeypatch.setattr(ringer, "time", SimpleNamespace(monotonic=lambda: now + 10))
    assert not ringer.call_ringing()


@pytest.mark.parametrize(
    "device, choice, mode, sounds",
    [
        ("fish", "billy", "billy", True),
        ("fish", "off", "off", True),  # blinks the status LED only
        ("fish", "phone", "phone", False),  # the phone's ringtone plays instead
        ("fish", "bogus", "billy", True),
        ("phone", "billy", None, False),  # other devices handle calls themselves
    ],
)
def test_the_ringtone_choice(isolated, device, choice, mode, sounds):
    module = isolated.load("bluetooth.ringer")
    isolated.config.BILLY_DEVICE = device
    isolated.config.BLUETOOTH_RINGTONE = choice
    assert module.ringtone_mode() == mode
    assert module._should_sound() is sounds


def test_a_button_press_answers_a_ringing_call(isolated, monkeypatch):
    ringer = isolated.load("bluetooth.ringer")
    controls = isolated.load("bluetooth.controls")
    # handle_button's "from . import ringer" reads the package attribute.
    monkeypatch.setattr(
        sys.modules["isolated_core.bluetooth"], "ringer", ringer, raising=False
    )
    monkeypatch.setattr(ringer, "_start", lambda: None)
    answered = Mock(return_value=True)
    monkeypatch.setattr(controls, "answer_call", answered)
    ringer.update(True)
    assert controls.handle_button(ai_active=False) is True
    answered.assert_called_once()
    assert not ringer.call_ringing()


def test_without_a_ringing_call_the_button_works_as_before(isolated, monkeypatch):
    controls = isolated.load("bluetooth.controls")
    monkeypatch.setattr(controls, "mode", isolated.load("bluetooth.mode"))
    answered = Mock()
    monkeypatch.setattr(controls, "answer_call", answered)
    assert controls.handle_button(ai_active=False) is False  # starts a conversation
    answered.assert_not_called()
