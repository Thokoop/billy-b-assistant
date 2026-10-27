import configparser
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolate_profile_lock(monkeypatch, tmp_path):
    import billy_runtime.profiles as locks

    # The default argument was bound at import time; patching ROOT alone
    # still opens a lock in the real project instead of this test directory.
    monkeypatch.setattr(locks.profile_lock, '__defaults__', (tmp_path,))


def test_stale_profile_objects_merge_updates_and_keep_fresh_mood(
    isolated, monkeypatch, tmp_path
):
    import billy_runtime.profiles as locks

    monkeypatch.setattr(locks, 'ROOT', tmp_path)
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(persona_manager=SimpleNamespace(current_persona='default')),
    )
    module = isolated.load('profile_manager')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    first = module.UserProfile('Thom')
    second = module.UserProfile('Thom')
    first.add_memory('first memory')
    second.set_display_name('Tom')
    first.increment_interaction_count()
    second.increment_interaction_count()
    result = module.UserProfile('Thom')
    assert result.data['USER_INFO']['display_name'] == 'Tom'
    assert result.data['USER_INFO']['interaction_count'] == '2'
    assert len(result.get_memories()) == 1


def test_failed_profile_save_does_not_publish_memory_change(
    isolated, monkeypatch, tmp_path
):
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona_manager',
        SimpleNamespace(persona_manager=SimpleNamespace(current_persona='default')),
    )
    module = isolated.load('profile_manager')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    profile = module.UserProfile('Thom')
    original = profile.profile_path.read_bytes()

    def fail(*args):
        raise OSError('disk full')

    monkeypatch.setattr(module, 'write_ini', fail)
    with pytest.raises(OSError):
        profile.set_display_name('wrong')
    assert profile.data['USER_INFO']['display_name'] == 'Thom'
    assert profile.profile_path.read_bytes() == original


def test_corrupt_ini_is_not_overwritten_during_load(isolated, monkeypatch, tmp_path):
    module = isolated.load('profile_manager')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    path = tmp_path / 'profiles' / 'thom.ini'
    path.parent.mkdir(exist_ok=True)
    path.write_text('broken file')
    with pytest.raises(configparser.Error):
        module.UserProfile('Thom')
    assert path.read_text() == 'broken file'
