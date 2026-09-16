"""Boundary inputs and injected dependency failures with observable outcomes."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components import purethink
from custom_components.purethink import protocol
from custom_components.purethink.config_flow import PurethinkConfigFlow
from custom_components.purethink.select import BaseSelect, FanModeSelect
from custom_components.purethink.switch import PowerSwitch
from test_port import setup, entry
from test_runtime import ha, runtime, receive


@pytest.mark.parametrize('payload', ['', 'A8A81721ZZ' + '00' * 18])
def test_parser_rejects_invalid_bits(payload, caplog):
    with pytest.raises(ValueError):
        protocol.parse_status_packet(payload)
    assert 'Packet parsing failed' in caplog.text


def test_command_fallbacks_and_failures(caplog):
    hass = SimpleNamespace(data={})
    packet = json.loads(protocol.generate_command('test', hass, device_mode='Sleep'))['contents']
    assert int(packet[8:10], 16) & 6 == 2  # Legacy malformed Sleep label falls back to Sleep 1.
    assert protocol.generate_command('test', hass, filter_reset='invalid') is None
    with pytest.raises(ValueError):
        protocol.generate_command('test', hass, fan_speed='invalid')
    assert '생성 실패' in caplog.text
    protocol.generate_command('test', hass, fan_speed=1 << 40)
    assert 'CMD 길이 불일치' in caplog.text


async def test_mqtt_missing_context_and_empty_parser_result(setup, monkeypatch, caplog):
    hass, entries, clients = setup
    msg = SimpleNamespace(payload=json.dumps({'contents':'A8A81721B100C0'+'00'*16}).encode())
    purethink._on_message(clients[0], {}, msg)
    assert 'userdata 누락' in caplog.text
    monkeypatch.setattr(purethink, 'parse_status_packet', lambda _: {})
    purethink._on_message(clients[0], clients[0].user_data_set.call_args.args[0], msg)
    await asyncio.sleep(0)
    assert not hass.data['purethink'][entries[0].entry_id]['state']
    assert '상태 패킷 파싱 실패' in caplog.text


async def test_duplicate_runtime_setup_cannot_replace_client(setup):
    hass, entries, clients = setup
    old = hass.data['purethink'][entries[0].entry_id]
    assert not await purethink.async_setup_entry(hass, entries[0])
    assert hass.data['purethink'][entries[0].entry_id] is old
    assert len(clients) == 2


async def test_filter_service_rejects_invalid_and_failed_command(setup, monkeypatch, caplog):
    hass, entries, clients = setup
    with pytest.raises(ValueError, match='Invalid filter_type'):
        await hass.services.async_call('purethink','reset_filter', {'filter_type':'wrong'}, blocking=True)
    monkeypatch.setattr(purethink, 'generate_command', lambda *a, **kw: None)
    await hass.services.async_call('purethink','reset_filter', {
        'filter_type':'prefilter','device_id':entries[0].data['device_id']}, blocking=True)
    assert '명령 생성 실패' in caplog.text
    clients[0].publish.assert_not_called()


@pytest.mark.parametrize('rc', [4, 5])
async def test_broker_refusal_is_retryable_and_cleans_up(setup, monkeypatch, rc):
    hass, entries, clients = setup
    client = MagicMock()
    client.loop_start.side_effect = lambda: client.on_connect(client, client.user_data_set.call_args.args[0], {}, rc)
    monkeypatch.setattr(purethink.mqtt, 'Client', lambda: client)
    with pytest.raises(ConfigEntryNotReady):
        await purethink.async_setup_entry(hass, entry(3))
    client.subscribe.assert_not_called()
    client.loop_stop.assert_called_once()
    assert 'entry3' not in hass.data['purethink']


async def test_cancelled_socket_failure_still_cleans_up(setup, monkeypatch):
    hass, entries, clients = setup
    client = MagicMock()
    started = threading.Event()
    release = threading.Event()
    def connect(*args):
        started.set()
        release.wait(2)
        raise OSError('connection closed while cancelling')
    client.connect_async.side_effect = connect
    monkeypatch.setattr(purethink.mqtt, 'Client', lambda: client)
    task = asyncio.create_task(purethink.async_setup_entry(hass, entry(3)))
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    client.loop_stop.assert_called_once()
    assert 'entry3' not in hass.data['purethink']


async def test_entity_publish_failures_do_not_change_telemetry(setup, caplog):
    hass, entries, clients = setup
    e = entries[0]
    data = hass.data['purethink'][e.entry_id]
    data['state'] = {'power':0, 'fan_speed':0}
    power = PowerSwitch(hass, e, data['command_topic'], data['device'])
    await power.async_turn_off()
    clients[0].publish.assert_not_called()
    clients[0].publish.side_effect = OSError('offline')
    pressure = BaseSelect(e.data, data['command_topic'], data['device'], 'pressure_mode',
                          ['정압','양압','음압'], 'Pressure', e.entry_id)
    pressure.hass = hass
    mode = FanModeSelect(e, data['command_topic'], data['device'])
    mode.hass = hass
    await power.async_turn_on()
    await pressure.async_select_option('양압')
    await mode.async_select_option('흡기')
    assert data['state'] == {'power':0,'fan_speed':0}
    assert sum('명령 전송 실패' in r.message for r in caplog.records) == 3
    clients[0].publish.side_effect = None
    await mode.async_select_option('배기')
    assert bytes.fromhex(json.loads(clients[0].publish.call_args.args[1])['contents'])[6] == 0x40


async def test_sleep_telemetry_then_power_off(runtime):
    hass, entries, clients = runtime
    receive(clients[0], 'A8A81721B500C0' + '00'*16)
    await hass.async_block_till_done()
    assert hass.states.get('fan.runtime_0').attributes['preset_mode'] == 'Sleep 2'
    assert hass.states.get('binary_sensor.runtime_0_sleep_mode').state == 'on'
    receive(clients[0], 'A8A81721010000' + '00'*16)
    await hass.async_block_till_done()
    for mode in ['auto', 'manual', 'sleep']:
        assert hass.states.get(f'binary_sensor.runtime_0_{mode}_mode').state == 'off'


def flow():
    f = PurethinkConfigFlow()
    f.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda *args: []))
    return f


@pytest.mark.parametrize('overrides,error', [({'friendly_name':''},'name_required'),
    ({'friendly_name':'x'*31},'name_too_long'), ({'mqtt_mode':'unsupported'},'invalid_mqtt_mode')])
async def test_setup_input_errors(overrides, error):
    result = await flow().async_step_user({'friendly_name':'Test','device_id':'DIV01-TEST01', **overrides})
    assert result['errors'] == {'base':error}


@pytest.mark.parametrize('port', [0,65536,'bad',None])
async def test_direct_local_step_port_validation(port):
    f = flow()
    await f.async_step_user({'friendly_name':'Test','device_id':'DIV01-TEST01','mqtt_mode':'local'})
    result = await f.async_step_local_mqtt({'mqtt_host':'broker','mqtt_port':port})
    assert result['errors'] == {'base':'invalid_port'}


async def test_local_step_without_pending_config_restarts_setup():
    assert (await flow().async_step_local_mqtt())['step_id'] == 'user'


def reconfigure_flow(hass, entry):
    f = PurethinkConfigFlow()
    f.hass = hass
    f.handler = 'purethink'
    f.context = {'source':'reconfigure','entry_id':entry.entry_id}
    f._reconfigure_entry = entry
    f._original_data = dict(entry.data)
    f._original_title = entry.title
    return f


async def test_commit_rechecks_duplicate_entry(runtime):
    hass, entries, clients = runtime
    f = reconfigure_flow(hass, entries[0])
    result = await f._finish({**entries[0].data,'device_id':entries[1].data['device_id']})
    assert result['reason'] == 'already_configured'
    clients[0].disconnect.assert_not_called()


async def test_entity_unique_id_collision_prevents_migration(runtime):
    from homeassistant.helpers import entity_registry as er
    hass, entries, clients = runtime
    registry = er.async_get(hass)
    registry.async_get_or_create('fan','purethink','DIV01-TARGET_fan',config_entry=entries[1])
    f = reconfigure_flow(hass, entries[0])
    result = await f._finish({**entries[0].data,'device_id':'DIV01-TARGET'})
    assert result['reason'] == 'already_configured'
    clients[0].disconnect.assert_not_called()


@pytest.mark.parametrize('failure',[OSError('unload failed'),asyncio.CancelledError()])
async def test_unload_exception_requests_old_configuration_reload(runtime, monkeypatch, failure):
    from unittest.mock import AsyncMock
    hass, entries, clients = runtime
    f = reconfigure_flow(hass, entries[0])
    original = dict(entries[0].data)
    with monkeypatch.context() as m:
        reload = MagicMock()
        m.setattr(hass.config_entries, 'async_schedule_reload', reload)
        m.setattr(hass.config_entries, 'async_unload', AsyncMock(side_effect=failure))
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await f._finish({**original, 'friendly_name':'New name'})
        else:
            assert (await f._finish({**original, 'friendly_name':'New name'}))['reason'] == 'reconfigure_failed'
        reload.assert_called_once_with(entries[0].entry_id)
    assert dict(entries[0].data) == original


async def test_settings_changed_during_unload_are_not_overwritten(runtime, monkeypatch):
    hass, entries, clients = runtime
    e = entries[0]
    f = reconfigure_flow(hass, e)
    updated = {**e.data, 'friendly_name':'Saved by another flow'}
    unload = hass.config_entries.async_unload
    async def concurrent_update(*args, **kwargs):
        result = await unload(*args, **kwargs)
        hass.config_entries.async_update_entry(e, data=updated)
        return result
    with monkeypatch.context() as m:
        m.setattr(hass.config_entries,'async_unload',concurrent_update)
        result = await f._finish({**e.data,'friendly_name':'Stale input'})
    assert result['reason'] == 'config_changed'
    await hass.async_block_till_done()
    assert dict(e.data) == updated
    assert e.entry_id in hass.data['purethink']


async def test_local_step_duplicate_appearing_between_steps(runtime):
    hass, entries, clients = runtime
    f = reconfigure_flow(hass, entries[0])
    await f.async_step_user({**entries[0].data,'device_id':'DIV01-TARGET','mqtt_mode':'local'})
    hass.config_entries.async_update_entry(entries[1], data={**entries[1].data,'device_id':'DIV01-TARGET'})
    result = await f.async_step_local_mqtt({'mqtt_host':'broker','mqtt_port':1883})
    assert result['errors'] == {'base':'already_configured'}
    clients[0].disconnect.assert_not_called()
