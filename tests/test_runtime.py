"""Load all platforms through Home Assistant's real config-entry manager."""
import asyncio
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntries, ConfigEntryState
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.setup import async_setup_component


@pytest.fixture
async def ha(tmp_path):
    (tmp_path / 'custom_components').symlink_to(Path(__file__).resolve().parents[1] / 'custom_components', target_is_directory=True)
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    await er.async_load(hass)
    await dr.async_load(hass)
    assert await async_setup_component(hass, 'homeassistant', {})
    yield hass
    for e in hass.config_entries.async_entries('purethink'):
        if e.state == ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(e.entry_id)
    await hass.async_stop(force=True)


@pytest.fixture
async def runtime(ha, monkeypatch):
    hass = ha
    clients = []
    def factory():
        c = MagicMock()
        c.loop_start.side_effect = lambda: c.on_connect(c, c.user_data_set.call_args.args[0], {}, 0)
        clients.append(c)
        return c
    monkeypatch.setattr('custom_components.purethink.mqtt.Client', factory)
    entries = []
    for i in range(2):
        e = ConfigEntry(version=1, minor_version=1, domain='purethink',
            title=f'Runtime {i}', data={'friendly_name': f'Runtime {i}', 'base_id': f'runtime_{i}',
            'device_id': f'DIV01-ABC00{i}'}, source='user', options={}, unique_id=None,
            discovery_keys=MappingProxyType({}), subentries_data=[])
        await hass.config_entries.async_add(e)
        entries.append(e)
    await hass.async_block_till_done()
    yield hass, entries, clients


async def test_real_platform_loading(runtime):
    hass, entries, clients = runtime
    assert all(e.state == ConfigEntryState.LOADED for e in entries)
    registry = er.async_get(hass)
    for e in entries:
        entities = er.async_entries_for_config_entry(registry, e.entry_id)
        assert len(entities) == 18
        assert len({entity.device_id for entity in entities}) == 1
        assert entities[0].device_id is not None
    assert len(clients) == 2


def receive(client, contents):
    client.on_message(client, client.user_data_set.call_args.args[0],
                      SimpleNamespace(payload=json.dumps({'type': 'MCU', 'contents': contents}).encode()))


@pytest.mark.parametrize('initial_on', [False, True])
async def test_toggle_via_homeassistant_service(runtime, initial_on):
    hass, entries, clients = runtime
    # Stable combinations: no automatic fan correction is needed.
    receive(clients[0], 'A8A81721' + ('B100C0' if initial_on else '810000') + '00' * 16)
    await hass.async_block_till_done()
    clients[0].publish.reset_mock()
    await hass.services.async_call('fan', 'toggle', {'entity_id': 'fan.runtime_0'}, blocking=True)
    assert clients[0].publish.called
    assert not clients[1].publish.called


async def test_state_update_and_registry_reload(runtime):
    hass, entries, clients = runtime
    receive(clients[0], 'A8A81721B100C0' + '00' * 16)
    await hass.async_block_till_done()
    assert hass.states.get('fan.runtime_0').attributes['percentage'] == 60
    assert hass.states.get('fan.runtime_0').state == 'on'
    registry = er.async_get(hass)
    before = {e.unique_id: (e.entity_id, e.device_id) for e in er.async_entries_for_config_entry(registry, entries[0].entry_id)}
    assert await hass.config_entries.async_reload(entries[0].entry_id)
    await hass.async_block_till_done()
    after = {e.unique_id: (e.entity_id, e.device_id) for e in er.async_entries_for_config_entry(registry, entries[0].entry_id)}
    assert before == after


async def test_duplicate_device_is_rejected(runtime):
    hass, entries, clients = runtime
    result = await hass.config_entries.flow.async_init('purethink', context={'source': 'user'},
        data={**entries[0].data, 'mqtt_mode': 'manufacturer'})
    assert result['type'] == 'abort'
    assert result['reason'] == 'already_configured'


async def test_initial_retained_state_reaches_entities(runtime):
    hass, entries, clients = runtime
    from custom_components.purethink.fan import PurethinkFan
    data = hass.data['purethink'][entries[0].entry_id]
    data['state'] = {'power': 1, 'fan_speed': 4, 'fan_in': 1, 'fan_out': 1, 'ai_mode': 1}
    entity = PurethinkFan(entries[0], data, data['device'], data['command_topic'])
    entity.hass = hass
    entity.schedule_update_ha_state = MagicMock()
    await entity.async_added_to_hass()
    assert entity.percentage == 80
    assert entity.preset_mode == 'Auto'
    await entity.async_will_remove_from_hass()


async def test_sensor_packet_end_to_end(runtime):
    hass, entries, clients = runtime
    air = ((987 << 34) | (12 << 24) | (34 << 14) | (56 << 4)).to_bytes(6, 'big')
    filters = (0x8000 | 1234).to_bytes(2, 'big') + (4321).to_bytes(2, 'big')
    frame = bytes.fromhex('A8A81721B127C090') + air + filters + bytes(5)
    receive(clients[0], frame.hex().upper())
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    def state(suffix):
        eid = registry.async_get_entity_id('sensor', 'purethink', 'DIV01-ABC000_' + suffix)
        return hass.states.get(eid)
    assert state('co2').state == '987'
    assert state('pm1').state == '12'
    assert state('pm25').state == '34'
    assert state('pm10').state == '56'
    assert state('wifi').state == '100'
    assert state('prefilter').state == '1234'
    assert state('prefilter').attributes['reset_needed'] is True
    assert state('hepafilter').state == '4321'
    assert state('fan_alarm').state == 'on'


async def test_new_state_cancels_stale_fan_adjustment(runtime):
    hass, entries, clients = runtime
    # First packet needs correction. A newer consistent state supersedes it.
    receive(clients[0], 'A8A817218100C0' + '00' * 16)
    await hass.async_block_till_done()
    receive(clients[0], 'A8A81721810000' + '00' * 16)
    await hass.async_block_till_done()
    await asyncio.sleep(1.1)
    clients[0].publish.assert_not_called()
    # Pending corrections must also be cancelled when the entry is unloaded.
    receive(clients[0], 'A8A817218100C0' + '00' * 16)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entries[0].entry_id)
    await asyncio.sleep(1.1)
    clients[0].publish.assert_not_called()


@pytest.mark.parametrize('port', [0, 65536, -1, 'oops', None])
async def test_config_flow_rejects_invalid_port(runtime, port):
    hass, entries, clients = runtime
    result = await hass.config_entries.flow.async_init('purethink', context={'source': 'user'},
        data={'friendly_name': 'New', 'device_id': 'DIV01-NEW001', 'mqtt_mode': 'local'})
    assert result['step_id'] == 'local_mqtt'
    from homeassistant.data_entry_flow import InvalidData
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result['flow_id'],
            {'mqtt_host': 'localhost', 'mqtt_port': port})


@pytest.mark.parametrize('device', ['', '  ', 'a/b', '+', '#'])
async def test_config_flow_rejects_invalid_device(runtime, device):
    hass, entries, clients = runtime
    result = await hass.config_entries.flow.async_init('purethink', context={'source': 'user'},
        data={'friendly_name': 'New', 'device_id': device, 'mqtt_mode': 'local'})
    assert result['type'] == 'form'
    assert result['errors']['base'] == 'invalid_device_id'


async def test_status_before_platform_setup_initializes_every_entity(runtime, monkeypatch):
    hass, entries, clients = runtime
    original = hass.config_entries.async_forward_entry_setups

    async def status_before_setup(entry, platforms):
        receive(clients[-1], 'A8A81721B100C0' + '00' * 16)
        await asyncio.sleep(0)
        await original(entry, platforms)

    monkeypatch.setattr(hass.config_entries, 'async_forward_entry_setups', status_before_setup)
    assert await hass.config_entries.async_reload(entries[0].entry_id)
    await hass.async_block_till_done()
    for item in er.async_entries_for_config_entry(er.async_get(hass), entries[0].entry_id):
        assert hass.states.get(item.entity_id).state not in ('unknown', 'unavailable'), item.entity_id
    assert hass.states.get('fan.runtime_0').attributes['percentage'] == 60
