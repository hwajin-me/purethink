"""Exercise reconfiguration through Home Assistant's real flow manager."""
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import entity_registry as er, device_registry as dr
from test_runtime import ha, runtime


async def start(hass, entry):
    return await hass.config_entries.flow.async_init('purethink', context={
        'source': 'reconfigure', 'entry_id': entry.entry_id,
    })


def snapshot(hass, entry):
    return {entity.entity_id: (entity.unique_id, entity.device_id, entity.name)
            for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_existing_manufacturer_to_local(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    before = snapshot(hass, entry)
    original = dict(entry.data)
    result = await start(hass, entry)
    assert result['step_id'] == 'reconfigure'
    assert result['data_schema']({}) == {
        'friendly_name': 'Runtime 0', 'device_id': 'DIV01-ABC000', 'mqtt_mode': 'manufacturer'}
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {
        'friendly_name': 'Renamed', 'device_id': 'DIV01-ABC000', 'mqtt_mode': 'local'})
    assert result['step_id'] == 'local_mqtt'
    assert result['data_schema']({})['mqtt_port'] == 1883
    assert dict(entry.data) == original
    clients[0].disconnect.assert_not_called()
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {
        'mqtt_host': '192.0.2.3', 'mqtt_port': 2883, 'mqtt_username': 'user', 'mqtt_password': 'secret'})
    assert result['type'] == 'abort'
    assert result['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done()
    assert entry.data['mqtt_mode'] == 'local'
    assert entry.title == entry.data['friendly_name'] == 'Renamed'
    assert entry.data['base_id'] == original['base_id']
    assert len(hass.config_entries.async_entries('purethink')) == 2
    assert snapshot(hass, entry) == before
    clients[0].disconnect.assert_called_once()
    clients[-1].connect_async.assert_called_once_with('192.0.2.3', 2883, 60)
    clients[-1].username_pw_set.assert_called_once_with('user', 'secret')
    clients[-1].tls_set_context.assert_not_called()
    clients[1].disconnect.assert_not_called()


async def test_local_defaults_and_switch_back(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    data = {**entry.data, 'mqtt_mode': 'local', 'mqtt_host': 'broker', 'mqtt_port': 1884,
            'mqtt_username': 'user', 'mqtt_password': 'secret'}
    hass.config_entries.async_update_entry(entry, data=data)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {})
    assert result['step_id'] == 'local_mqtt'
    assert result['data_schema']({}) == {'mqtt_host': 'broker', 'mqtt_port': 1884,
                                        'mqtt_username': 'user', 'mqtt_password': 'secret'}
    # Invalid host preserves submitted fields and does not commit pending edits.
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {
        'mqtt_host': ' ', 'mqtt_port': 1885, 'mqtt_username': '', 'mqtt_password': ''})
    assert result['errors'] == {'base': 'host_required'}
    assert result['data_schema']({})['mqtt_port'] == 1885
    assert dict(entry.data) == data
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_host': 'new-broker'})
    await hass.async_block_till_done()
    assert entry.data['mqtt_port'] == 1885
    assert entry.data['mqtt_username'] == entry.data['mqtt_password'] == ''
    clients[-1].username_pw_set.assert_not_called()
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_mode': 'manufacturer'})
    assert result['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done()
    assert not {'mqtt_host','mqtt_port','mqtt_username','mqtt_password'} & entry.data.keys()
    clients[-1].connect_async.assert_called_once_with('dapt.iptime.org', 8885, 60)
    clients[-1].tls_set_context.assert_called_once()


async def test_cancel_preserves_original_config(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    original = dict(entry.data)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_mode': 'local'})
    hass.config_entries.flow.async_abort(result['flow_id'])
    await hass.async_block_till_done()
    assert dict(entry.data) == original
    clients[0].disconnect.assert_not_called()
    assert len(clients) == 2


async def test_change_device_id_preserves_registry_and_routes_new_topic(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    entities = er.async_get(hass)
    entities.async_update_entity('fan.runtime_0', new_entity_id='fan.my_ventilator', name='My fan')
    await hass.async_block_till_done()
    before = snapshot(hass, entry)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'THESOOP-123456'})
    assert result['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done()
    after = snapshot(hass, entry)
    assert after == {eid: (uid.replace('DIV01-ABC000_', 'THESOOP-123456_'), did, name)
                     for eid, (uid, did, name) in before.items()}
    assert len(after) == 18
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={('purethink','DIV01-ABC000')}) is None
    assert devices.async_get_device(identifiers={('purethink','THESOOP-123456')}).id == next(iter(before.values()))[1]
    assert 'DIV01-ABC000' not in hass.data['purethink']['_devices']
    assert hass.data['purethink']['_devices']['THESOOP-123456'] == entry.entry_id
    await hass.services.async_call('fan','turn_on', {'entity_id': 'fan.my_ventilator'}, blocking=True)
    assert clients[-1].publish.call_args.args[0] == '/things/THESOOP-123456/shadow'


async def test_other_device_rejected_without_changes(runtime):
    hass, entries, clients = runtime
    original = dict(entries[0].data)
    result = await start(hass, entries[0])
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': entries[1].data['device_id']})
    assert result['step_id'] == 'reconfigure'
    assert result['errors'] == {'base': 'already_configured'}
    assert dict(entries[0].data) == original
    assert len(clients) == 2


async def test_failed_unload_does_not_migrate_config_or_registry(runtime, monkeypatch):
    hass, entries, clients = runtime
    entry = entries[0]
    original = dict(entry.data)
    before = snapshot(hass, entry)
    monkeypatch.setattr(hass.config_entries, 'async_unload', AsyncMock(return_value=False))
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-NEW001'})
    assert result['reason'] == 'reconfigure_failed'
    assert dict(entry.data) == original
    assert snapshot(hass, entry) == before
    monkeypatch.undo()


async def test_registry_collision_aborts_without_unloading(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    before = snapshot(hass, entry)
    dr.async_get(hass).async_get_or_create(config_entry_id=entries[1].entry_id,
        identifiers={('purethink', 'DIV01-ORPHAN')})
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-ORPHAN'})
    assert result['reason'] == 'already_configured'
    assert snapshot(hass, entry) == before
    clients[0].disconnect.assert_not_called()


async def test_same_id_unload_failure_keeps_mqtt_settings(runtime, monkeypatch):
    hass, entries, clients = runtime
    entry = entries[0]
    original = dict(entry.data)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_mode': 'local'})
    monkeypatch.setattr(hass.config_entries, 'async_unload', AsyncMock(return_value=False))
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_host': 'other-broker'})
    assert result['reason'] == 'reconfigure_failed'
    assert dict(entry.data) == original
    monkeypatch.undo()


async def test_partial_registry_migration_is_rolled_back(runtime, monkeypatch):
    hass, entries, clients = runtime
    entry = entries[0]
    original = dict(entry.data)
    before = snapshot(hass, entry)
    registry = er.async_get(hass)
    update = registry.async_update_entity
    count = 0
    def fail_second(*args, **kwargs):
        nonlocal count
        if 'new_unique_id' in kwargs:
            count += 1
            if count == 2:
                raise ValueError('injected registry failure')
        return update(*args, **kwargs)
    monkeypatch.setattr(registry, 'async_update_entity', fail_second)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-NEW002'})
    assert result['reason'] == 'reconfigure_failed'
    await hass.async_block_till_done()
    assert dict(entry.data) == original
    assert snapshot(hass, entry) == before
    assert entry.entry_id in hass.data['purethink']


async def test_stale_flow_cannot_overwrite_new_settings(runtime):
    hass, entries, clients = runtime
    entry = entries[0]
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_mode': 'local'})
    updated = {**entry.data, 'friendly_name': 'Changed elsewhere'}
    hass.config_entries.async_update_entry(entry, data=updated)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'mqtt_host': 'broker'})
    assert result['reason'] == 'config_changed'
    assert dict(entry.data) == updated
    clients[0].disconnect.assert_not_called()


async def test_device_registry_failure_restores_all_entity_ids(runtime, monkeypatch):
    hass, entries, clients = runtime
    entry = entries[0]
    before = snapshot(hass, entry)
    devices = dr.async_get(hass)
    update = devices.async_update_device
    def reject_new_id(device_id, **kwargs):
        if ('purethink', 'DIV01-NEW003') in kwargs.get('new_identifiers', set()):
            raise ValueError('injected device registry failure')
        return update(device_id, **kwargs)
    monkeypatch.setattr(devices, 'async_update_device', reject_new_id)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-NEW003'})
    assert result['reason'] == 'reconfigure_failed'
    await hass.async_block_till_done()
    assert snapshot(hass, entry) == before
    assert entry.data['device_id'] == 'DIV01-ABC000'
    assert devices.async_get_device(identifiers={('purethink','DIV01-ABC000')}) is not None


async def test_collision_created_during_unload_is_detected(runtime, monkeypatch):
    hass, entries, clients = runtime
    entry = entries[0]
    before = snapshot(hass, entry)
    unload = hass.config_entries.async_unload
    async def concurrent_collision(*args, **kwargs):
        result = await unload(*args, **kwargs)
        dr.async_get(hass).async_get_or_create(config_entry_id=entries[1].entry_id,
            identifiers={('purethink', 'DIV01-RACING')})
        return result
    monkeypatch.setattr(hass.config_entries, 'async_unload', concurrent_collision)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-RACING'})
    assert result['reason'] == 'already_configured'
    await hass.async_block_till_done()
    assert snapshot(hass, entry) == before
    assert entry.data['device_id'] == 'DIV01-ABC000'


async def test_ui_support_and_disabled_entity_identity_preserved(runtime):
    from homeassistant.helpers.entity_registry import RegistryEntryDisabler
    hass, entries, clients = runtime
    entry = entries[0]
    assert entry.supports_reconfigure is True
    registry = er.async_get(hass)
    registry.async_update_entity('fan.runtime_0', disabled_by=RegistryEntryDisabler.USER)
    before = snapshot(hass, entry)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-DISABL'})
    assert result['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done()
    entity = registry.async_get('fan.runtime_0')
    assert entity.disabled_by == RegistryEntryDisabler.USER
    assert entity.unique_id == 'DIV01-DISABL_fan'
    assert entity.device_id == before['fan.runtime_0'][1]


async def test_commit_failure_restores_device_and_entity_registry(runtime, monkeypatch):
    from custom_components.purethink.config_flow import PurethinkConfigFlow
    hass, entries, clients = runtime
    entry = entries[0]
    before = snapshot(hass, entry)
    original = dict(entry.data)
    def fail_commit(*args, **kwargs):
        raise ValueError('injected commit failure')
    monkeypatch.setattr(PurethinkConfigFlow, 'async_update_reload_and_abort', fail_commit)
    result = await start(hass, entry)
    result = await hass.config_entries.flow.async_configure(result['flow_id'], {'device_id': 'DIV01-COMMIT'})
    assert result['reason'] == 'reconfigure_failed'
    await hass.async_block_till_done()
    assert dict(entry.data) == original
    assert snapshot(hass, entry) == before
    devices = dr.async_get(hass)
    assert devices.async_get_device(identifiers={('purethink', 'DIV01-COMMIT')}) is None
    assert devices.async_get_device(identifiers={('purethink', original['device_id'])}) is not None
