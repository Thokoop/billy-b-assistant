"""Catalog scans log one batch, without hiding invalid persona files."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def personas(isolated, monkeypatch, tmp_path):
    import billy_runtime.paths as paths

    monkeypatch.setattr(paths, 'INSTALL_ROOT', tmp_path)
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.providers.realtime',
        SimpleNamespace(voice_provider_registry=Mock()),
    )
    monkeypatch.setitem(
        sys.modules,
        'isolated_core.persona',
        SimpleNamespace(migrate_traits=lambda values: values),
    )
    module = isolated.load('persona_manager')
    manager = module.PersonaManager()
    (tmp_path / 'persona.ini').write_text('[META]\ndescription=Default\n')
    (manager.personas_dir / 'one.ini').write_text('[META]\ndescription=One\n')
    folder = manager.personas_dir / 'two'
    folder.mkdir()
    (folder / 'persona.ini').write_text('[META]\ndescription=Two\n')
    return SimpleNamespace(manager=manager, logger=isolated.logger)


def test_catalog_loads_both_layouts_in_one_log_and_does_not_repeat_cached_batch(
    personas,
):
    p = personas
    assert [item['name'] for item in p.manager.get_available_personas()] == [
        'default',
        'one',
        'two',
    ]
    p.logger.info.assert_called_once_with('Loaded 2 personas: one, two', '🎭')
    p.logger.info.reset_mock()
    p.manager.get_available_personas()
    p.logger.info.assert_not_called()


def test_failed_persona_is_reported_separately_and_not_counted_as_loaded(personas):
    p = personas
    (p.manager.personas_dir / 'broken.ini').write_text('not an INI file')
    assert [item['name'] for item in p.manager.get_available_personas()] == [
        'default',
        'one',
        'two',
    ]
    p.logger.error.assert_called_once()
    assert 'broken' in p.logger.error.call_args.args[0]
    p.logger.info.assert_called_once_with('Loaded 2 personas: one, two', '🎭')


def test_active_persona_switch_and_direct_load_remain_visible_but_cache_is_verbose(
    personas,
):
    p = personas
    p.manager.get_available_personas()
    p.logger.reset_mock()
    assert p.manager.switch_persona('one')
    p.logger.info.assert_called_once_with('Switched to persona: one', '🎭')
    p.logger.verbose.assert_called_once_with('Cleared cache for persona: one', '🎭')
    assert p.manager.load_persona('one')['meta']['description'] == 'One'
    p.logger.info.assert_any_call('Loaded persona: one', '🎭')
    p.logger.info.reset_mock()
    p.manager.get_available_personas()
    p.logger.info.assert_not_called()
