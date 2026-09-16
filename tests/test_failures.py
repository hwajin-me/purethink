"""Failure paths that used to be hidden by happy-path mocks."""
from unittest.mock import MagicMock
import pytest
from test_port import setup
from custom_components.purethink import async_unload_entry


async def test_stop_loop_even_if_disconnect_raises(setup):
    hass, entries, clients = setup
    clients[0].disconnect.side_effect = OSError('already disconnected')
    assert await async_unload_entry(hass, entries[0])
    clients[0].loop_stop.assert_called_once()


async def test_invalid_fan_speed_does_not_replace_good_state(setup):
    import asyncio, json
    from types import SimpleNamespace
    from custom_components.purethink import _on_message
    hass, entries, clients = setup
    good = {'power': 1, 'fan_speed': 3}
    hass.data['purethink'][entries[0].entry_id]['state'] = good
    _on_message(clients[0], clients[0].user_data_set.call_args.args[0], SimpleNamespace(
        payload=json.dumps({'type': 'MCU', 'contents': 'A8A81721F100C0' + '00' * 16}).encode()))
    await asyncio.sleep(0)
    assert hass.data['purethink'][entries[0].entry_id]['state'] == good


@pytest.mark.parametrize('payload', [b'not-json', b'[]', b'null', b'{}',
    b'{"contents": null}', b'{"contents":"A8A81721"}',
    b'{"contents":"A8A81721B100C0000000000000000000000000000000000Z"}',
    b'{"contents":"A8A81721B130C00000000000000000000000000000000000"}'])
async def test_bad_packets_preserve_previous_state(setup, payload):
    import asyncio
    from types import SimpleNamespace
    from custom_components.purethink import _on_message
    hass, entries, clients = setup
    good = {'power': 1, 'fan_speed': 3}
    hass.data['purethink'][entries[0].entry_id]['state'] = good
    _on_message(clients[0], clients[0].user_data_set.call_args.args[0], SimpleNamespace(payload=payload))
    await asyncio.sleep(0)
    assert hass.data['purethink'][entries[0].entry_id]['state'] == good


@pytest.mark.parametrize('ai,sleep,bits', [(1,0,8), (0,1,2), (0,2,4), (0,3,6)])
async def test_power_cycle_restores_automatic_modes(setup, ai, sleep, bits):
    from custom_components.purethink.switch import PowerSwitch
    from test_port import command_bytes
    hass, entries, clients = setup
    e = entries[1]; data = hass.data['purethink'][e.entry_id]
    data['state'] = {'power': 1, 'fan_speed': 3, 'ai_mode': ai, 'sleep_mode': sleep,
                     'pressure_mode': 2, 'fan_in': 1, 'fan_out': 1}
    power = PowerSwitch(hass, e, data['command_topic'], data['device'])
    await power.async_turn_on()  # Already on: do not restore old settings.
    clients[1].publish.assert_not_called()
    await power.async_turn_off()
    data['state']['power'] = 0
    data['state']['ai_mode'] = data['state']['sleep_mode'] = 0
    await power.async_turn_on()
    packet = command_bytes(clients[1])
    assert packet[4:7] == bytes([0xB1 | bits, 0x20, 0xC0])
    clients[0].publish.assert_not_called()


async def test_setup_cancellation_while_waiting_for_broker_cleans_client(setup, monkeypatch):
    import asyncio
    from custom_components.purethink import async_setup_entry
    from test_port import entry
    hass, entries, clients = setup
    client = MagicMock()
    # Socket connected, but no CONNACK has arrived.
    monkeypatch.setattr('custom_components.purethink.mqtt.Client', lambda: client)
    third = entry(3)
    task = asyncio.create_task(async_setup_entry(hass, third))
    while not client.loop_start.called:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()
    assert third.entry_id not in hass.data['purethink']
    assert third.data['device_id'] not in hass.data['purethink']['_devices']


async def test_cancel_during_socket_connect_waits_before_cleanup(setup, monkeypatch):
    import asyncio
    import threading
    from custom_components.purethink import async_setup_entry
    from test_port import entry
    hass, entries, clients = setup
    client = MagicMock()
    started = threading.Event()
    release = threading.Event()
    def blocked_connect(*args):
        started.set()
        release.wait(2)
    client.connect_async.side_effect = blocked_connect
    monkeypatch.setattr('custom_components.purethink.mqtt.Client', lambda: client)
    third = entry(3)
    task = asyncio.create_task(async_setup_entry(hass, third))
    try:
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        client.disconnect.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    client.loop_start.assert_called_once()
    client.loop_stop.assert_called_once()
    assert third.entry_id not in hass.data['purethink']
