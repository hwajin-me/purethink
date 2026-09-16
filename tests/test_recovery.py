"""Recovery timers are independent per entry and cannot revive unloaded clients."""
from unittest.mock import MagicMock
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from custom_components.purethink import _on_connect_fail, _on_disconnect, async_unload_entry
from test_runtime import ha, runtime


async def test_retry_configuration_and_temporary_disconnect(runtime):
    hass,entries,clients=runtime
    client=clients[0]
    client.connect.assert_not_called()
    client.reconnect_delay_set.assert_called_once_with(min_delay=5,max_delay=60)
    recovery=hass.data['purethink'][entries[0].entry_id]['recovery']
    hass.config_entries.async_schedule_reload=MagicMock()
    recovery.cancel_timer()
    client.is_connected.return_value=False
    recovery._check(None)
    assert recovery.misses==1 and not recovery.stopped
    recovery.cancel_timer()
    client.is_connected.return_value=True
    recovery._check(None)
    assert recovery.misses==0
    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_stalled_connection_reloads_only_affected_entry(runtime):
    hass,entries,clients=runtime
    recovery=hass.data['purethink'][entries[0].entry_id]['recovery']
    hass.config_entries.async_schedule_reload=MagicMock()
    clients[0].is_connected.return_value=False
    for _ in range(2):
        recovery.cancel_timer()
        recovery._check(None)
    hass.config_entries.async_schedule_reload.assert_called_once_with(entries[0].entry_id)
    assert not recovery.stopped and recovery.misses == 0
    assert not hass.data['purethink'][entries[1].entry_id]['recovery'].stopped
    # If HA cannot unload on this attempt, the watchdog must keep trying.
    for _ in range(2):
        recovery.cancel_timer()
        recovery._check(None)
    assert hass.config_entries.async_schedule_reload.call_count == 2
    recovery.stop()
    recovery._check(None)
    assert hass.config_entries.async_schedule_reload.call_count == 2


async def test_stale_and_removed_connection_cannot_reload(runtime):
    hass,entries,clients=runtime
    recovery=hass.data['purethink'][entries[0].entry_id]['recovery']
    hass.config_entries.async_schedule_reload=MagicMock()
    recovery.cancel_timer()
    data=hass.data['purethink'][entries[0].entry_id]
    data['mqtt']=MagicMock()
    recovery._check(None)
    assert recovery.stopped
    data['mqtt']=clients[0]
    second=hass.data['purethink'][entries[1].entry_id]['recovery']
    second.cancel_timer()
    removed=hass.data['purethink'].pop(entries[1].entry_id)
    second._check(None)
    hass.data['purethink'][entries[1].entry_id]=removed
    hass.config_entries.async_schedule_reload.assert_not_called()


async def test_shutdown_and_unload_cancel_recovery(runtime):
    hass,entries,clients=runtime
    first=hass.data['purethink'][entries[0].entry_id]['recovery']
    second=hass.data['purethink'][entries[1].entry_id]['recovery']
    assert await async_unload_entry(hass,entries[0])
    assert first.stopped and first.cancel_timer is None
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert second.stopped and second.cancel_timer is None
    second.stop()


async def test_connection_failure_callbacks_report_retry(runtime,caplog):
    hass,entries,clients=runtime
    client=clients[0]
    userdata=client.user_data_set.call_args.args[0]
    assert client.on_connect_fail is _on_connect_fail
    assert client.on_disconnect is _on_disconnect
    client.on_connect_fail(client,userdata)
    client.on_disconnect(client,userdata,7)
    client.on_disconnect(client,userdata,0)
    assert '자동 재시도' in caplog.text and '자동 재접속' in caplog.text


import pytest


@pytest.mark.parametrize('worker_present',[False,True])
async def test_connected_flag_with_dead_worker_requires_recovery(runtime,worker_present):
    hass,entries,clients=runtime
    client=clients[0]
    client.is_connected.return_value=True
    client._thread=MagicMock() if worker_present else None
    if worker_present:
        client._thread.is_alive.return_value=False
    recovery=hass.data['purethink'][entries[0].entry_id]['recovery']
    hass.config_entries.async_schedule_reload=MagicMock()
    for _ in range(2):
        recovery.cancel_timer()
        recovery._check(None)
    hass.config_entries.async_schedule_reload.assert_called_once_with(entries[0].entry_id)
