"""Opt-in tests with an isolated, authenticated Mosquitto broker on loopback."""
import asyncio
import json
import os
import socket
from pathlib import Path
import subprocess
from types import MappingProxyType

import paho.mqtt.client as mqtt
import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from test_runtime import ha

pytestmark = pytest.mark.skipif(os.environ.get('PURETHINK_LIVE_MQTT') != '1',
                                reason='Set PURETHINK_LIVE_MQTT=1 to run Docker Mosquitto tests')


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT).strip()


@pytest.fixture
async def broker(tmp_path):
    config = tmp_path / 'mosquitto.conf'
    config.write_text('listener 1883\nallow_anonymous false\npassword_file /tmp/passwords\npersistence false\n')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        selected_port = reservation.getsockname()[1]
    container = await asyncio.to_thread(docker, 'run', '-d', '-p', f'127.0.0.1:{selected_port}:1883',
        '-v', f'{config}:/mosquitto/config/mosquitto.conf:ro', 'eclipse-mosquitto:2',
        'sh', '-c', 'if [ ! -f /tmp/passwords ]; then mosquitto_passwd -b -c /tmp/passwords test-user test-password; chown mosquitto:mosquitto /tmp/passwords; chmod 600 /tmp/passwords; fi; exec mosquitto -c /mosquitto/config/mosquitto.conf')
    try:
        port = int((await asyncio.to_thread(docker, 'port', container, '1883/tcp')).rsplit(':', 1)[1])
        yield container, port
    except Exception:
        print(await asyncio.to_thread(docker, "logs", container))
        raise
    finally:
        await asyncio.to_thread(docker, 'rm', '-f', container)


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_real_mqtt_retained_commands_reconnect_and_unload(ha, broker, caplog):
    container, port = broker
    emulator = mqtt.Client()
    emulator.username_pw_set('test-user', 'test-password')
    messages = []
    loop = asyncio.get_running_loop()
    emulator.on_message = lambda c, u, m: loop.call_soon_threadsafe(messages.append, (m.topic, json.loads(m.payload)))
    emulator.on_connect = lambda c, u, f, rc: c.subscribe('/things/+/shadow', qos=1)
    emulator.connect_async('127.0.0.1', port, 60)
    emulator.loop_start()
    entries = []
    try:
        await until(emulator.is_connected)
        async def publish(device, contents):
            info = emulator.publish(f'/things/{device}/shadow', json.dumps({'type': 'MCU', 'contents': contents}), qos=1, retain=True)
            await asyncio.to_thread(info.wait_for_publish, 5)
        # Retained messages arrive before platform setup and must populate all entities.
        for i in range(2):
            device = f'DIV01-BEE00{i}'
            await publish(device, 'A8A81721' + ('B100C0' if i == 0 else 'D100C0') + '00' * 16)
            e = ConfigEntry(version=1, minor_version=1, domain='purethink', title=f'Live {i}',
                data={'friendly_name': f'Live {i}', 'base_id': f'live_{i}', 'device_id': device,
                      'mqtt_mode': 'local', 'mqtt_host': '127.0.0.1', 'mqtt_port': port,
                      'mqtt_username': 'test-user', 'mqtt_password': 'test-password'},
                source='user', options={}, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[])
            await ha.config_entries.async_add(e)
            entries.append(e)
        await until(lambda: all(ha.states.get(f'fan.live_{i}') and
                     ha.states.get(f'fan.live_{i}').attributes.get('percentage') == speed
                     for i, speed in [(0, 60), (1, 100)]))
        await ha.async_block_till_done()
        for e in entries:
            assert e.state == ConfigEntryState.LOADED
            for item in er.async_entries_for_config_entry(er.async_get(ha), e.entry_id):
                assert ha.states.get(item.entity_id).state not in ('unknown', 'unavailable'), item.entity_id
        state_before = dict(ha.data['purethink'][entries[1].entry_id]['state'])
        await ha.services.async_call('fan', 'set_percentage', {'entity_id': 'fan.live_1', 'percentage': 40}, blocking=True)
        await until(lambda: any(m[1]['type'] == 'CMD' for m in messages))
        commands = [(topic, msg) for topic, msg in messages if msg['type'] == 'CMD']
        assert all(topic == '/things/DIV01-BEE001/shadow' for topic, msg in commands)
        assert bytes.fromhex(commands[-1][1]['contents'])[4] == 0xA1
        # Broker echoes must not overwrite the device's actual telemetry.
        assert ha.data['purethink'][entries[1].entry_id]['state'] == state_before
        clients = [ha.data['purethink'][e.entry_id]['mqtt'] for e in entries]
        subscribed = set()
        for index, client in enumerate([*clients, emulator]):
            client.on_subscribe = lambda c, u, mid, qos, i=index: loop.call_soon_threadsafe(subscribed.add, i)
        await asyncio.to_thread(docker, 'restart', container)
        try:
            await until(lambda: len(subscribed) == 3)
        except TimeoutError:
            print(await asyncio.to_thread(docker, 'logs', container))
            print('ports', port, await asyncio.to_thread(docker, 'port', container, '1883/tcp'))
            raise
        await publish('DIV01-BEE000', 'A8A81722C100C0' + '00' * 16)
        await until(lambda: ha.states.get('fan.live_0').attributes.get('percentage') == 80)
        assert await ha.config_entries.async_unload(entries[0].entry_id)
        assert clients[0]._thread is None
        assert clients[1].is_connected()
        await ha.services.async_call('purethink', 'reset_filter', {'filter_type': 'prefilter'}, blocking=True)
        await until(lambda: any(msg['type'] == 'CMD' and bytes.fromhex(msg['contents'])[14:16] == bytes([135,208]) for _,msg in messages))
        assert not [r for r in caplog.records if r.levelno >= 40]
    finally:
        for e in entries:
            if e.state == ConfigEntryState.LOADED:
                await ha.config_entries.async_unload(e.entry_id)
        await asyncio.to_thread(emulator.disconnect)
        await asyncio.to_thread(emulator.loop_stop)


async def test_reconfigure_bad_password_then_recover(ha, broker):
    from test_reconfigure import start, snapshot
    _, port = broker
    probe = mqtt.Client()
    probe.username_pw_set('test-user', 'test-password')
    probe.connect_async('127.0.0.1', port)
    probe.loop_start()
    try:
        await until(probe.is_connected)
        result = await ha.config_entries.flow.async_init('purethink', context={'source': 'user'},
            data={'friendly_name':'Reconfig live', 'device_id':'DIV01-RECONF', 'mqtt_mode':'local'})
        await ha.config_entries.flow.async_configure(result['flow_id'], {
            'mqtt_host':'127.0.0.1', 'mqtt_port':port,
            'mqtt_username':'test-user', 'mqtt_password':'test-password'})
        await ha.async_block_till_done()
        entry = ha.config_entries.async_entries('purethink')[0]
        before = snapshot(ha, entry)
        old = ha.data['purethink'][entry.entry_id]['mqtt']
        result = await start(ha, entry)
        result = await ha.config_entries.flow.async_configure(result['flow_id'], {})
        await ha.config_entries.flow.async_configure(result['flow_id'], {'mqtt_password':'wrong-password'})
        await ha.async_block_till_done()
        # A TCP connection alone must not be reported as a working MQTT setup.
        assert entry.state == ConfigEntryState.SETUP_RETRY
        assert old._thread is None
        assert entry.entry_id not in ha.data['purethink']
        result = await start(ha, entry)
        assert result['step_id'] == 'reconfigure'
        result = await ha.config_entries.flow.async_configure(result['flow_id'], {})
        result = await ha.config_entries.flow.async_configure(result['flow_id'], {'mqtt_password':'test-password'})
        assert result['reason'] == 'reconfigure_successful'
        await ha.async_block_till_done()
        assert entry.state == ConfigEntryState.LOADED
        assert snapshot(ha, entry) == before
        current = ha.data['purethink'][entry.entry_id]['mqtt']
        assert current.is_connected()
        assert current is not old
    finally:
        await asyncio.to_thread(probe.disconnect)
        await asyncio.to_thread(probe.loop_stop)


async def test_initial_offline_broker_recovers_without_user_reload(ha, broker, monkeypatch):
    """Exercise HA's actual scheduled retry, not a manual async_setup/reconfigure."""
    import homeassistant.config_entries as config_entries
    container,port=broker
    await asyncio.to_thread(docker,'stop',container)
    # Speed up HA's backoff only; the MQTT connection/CONNACK timeout is real.
    schedule=config_entries.async_call_later
    monkeypatch.setattr(config_entries,'async_call_later',lambda hass,delay,action:
        schedule(hass,min(delay,0.1),action))
    await ha.async_start()
    e=ConfigEntry(version=1,minor_version=1,domain='purethink',title='Retry live',
        data={'friendly_name':'Retry live','base_id':'retry_live','device_id':'DIV01-RETRY',
              'mqtt_mode':'local','mqtt_host':'127.0.0.1','mqtt_port':port,
              'mqtt_username':'test-user','mqtt_password':'test-password'},
        source='user',options={},unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=[])
    await ha.config_entries.async_add(e)
    assert e.state==ConfigEntryState.SETUP_RETRY
    # A second failed setup proves retry continues after the first failure.
    await until(lambda: e._tries>=2,timeout=20)
    await asyncio.to_thread(docker,'start',container)
    await until(lambda:e.state==ConfigEntryState.LOADED,timeout=25)
    client=ha.data['purethink'][e.entry_id]['mqtt']
    assert client.is_connected()
    message=json.dumps({'type':'MCU','contents':'A8A81721B100C0'+'00'*16})
    client.publish('/things/DIV01-RETRY/shadow',message,qos=1)
    await until(lambda:ha.states.get('fan.retry_live').state=='on')
    assert ha.states.get('fan.retry_live').attributes['percentage']==60


async def test_exited_network_worker_is_recreated_by_recovery(ha, broker):
    _,port=broker
    e=ConfigEntry(version=1,minor_version=1,domain='purethink',title='Worker',
        data={'friendly_name':'Worker','base_id':'worker','device_id':'DIV01-WORKER',
              'mqtt_mode':'local','mqtt_host':'127.0.0.1','mqtt_port':port,
              'mqtt_username':'test-user','mqtt_password':'test-password'},
        source='user',options={},unique_id=None,discovery_keys=MappingProxyType({}),subentries_data=[])
    await ha.config_entries.async_add(e)
    data=ha.data['purethink'][e.entry_id]
    client=data['mqtt']
    await asyncio.to_thread(client.loop_stop)
    assert client._thread is None and client.is_connected()
    recovery=data['recovery']
    for _ in range(2):
        recovery.cancel_timer()
        recovery._check(None)
    await until(lambda: ha.data['purethink'].get(e.entry_id,{}).get('mqtt') not in (None,client)
                and e.state==ConfigEntryState.LOADED)
    client=ha.data['purethink'][e.entry_id]['mqtt']
    assert client._thread is not None and client._thread.is_alive()
    client.publish('/things/DIV01-WORKER/shadow',json.dumps({
        'type':'MCU','contents':'A8A81721B100C0'+'00'*16}),qos=1)
    await until(lambda:ha.states.get('fan.worker').state=='on')
    assert e.state==ConfigEntryState.LOADED
