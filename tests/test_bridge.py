"""Exercise real TLS/MQTT sockets plus lifecycle, relay and config failures."""
import asyncio
from contextlib import suppress
import json
import ssl
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import paho.mqtt.client as mqtt
import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.purethink.bridge import EmbeddedBridge, BridgePlugin, _BRIDGES, _tls_context
from test_runtime import ha, runtime
from test_mqtt_live import until


def entry(port=0, cloud=False, device='DIV01-BRIDGE'):
    return ConfigEntry(version=1, minor_version=1, domain='purethink', title='Bridge',
        data={'friendly_name':'Bridge', 'base_id':'bridge', 'device_id':device,
              'mqtt_mode':'bridge','bridge_port':port,'bridge_cloud':cloud},
        source='user', options={}, unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[])


async def connect_device(bridge, device=None):
    client = mqtt.Client(client_id=device or bridge.device_id)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    messages, subscribed = [], []
    loop = asyncio.get_running_loop()
    client.on_connect = lambda c,u,f,rc: c.subscribe(bridge.prefix + '/#', qos=1)
    client.on_subscribe = lambda c,u,mid,qos: loop.call_soon_threadsafe(subscribed.append, qos)
    client.on_message = lambda c,u,m: loop.call_soon_threadsafe(messages.append, (m.topic, m.payload))
    client.connect_async('127.0.0.1', bridge.server.sockets[0].getsockname()[1], 60)
    client.loop_start()
    await until(lambda: subscribed)
    return client, messages


async def stop_client(client):
    await asyncio.to_thread(client.disconnect)
    await asyncio.to_thread(client.loop_stop)


async def test_embedded_tls_device_to_ha_and_commands(ha):
    e = entry()
    await ha.config_entries.async_add(e)
    assert e.state == ConfigEntryState.LOADED
    bridge = ha.data['purethink'][e.entry_id]['bridge']
    device, messages = await connect_device(bridge)
    try:
        assert ha.states.get('sensor.bridge_device_id').state == e.data['device_id']
        device.publish(bridge.topic, json.dumps({'type':'MCU','contents':'A8A81721B100C0'+'00'*16}), qos=1)
        await until(lambda: ha.states.get('fan.bridge').state == 'on')
        await ha.services.async_call('fan','set_percentage',{'entity_id':'fan.bridge','percentage':40},blocking=True)
        await until(lambda: any(json.loads(payload).get('type') == 'CMD' for _,payload in messages))
        command = next(json.loads(payload) for _,payload in messages if json.loads(payload).get('type') == 'CMD')
        assert bytes.fromhex(command['contents'])[4] == 0xA1
        assert ha.states.get('fan.bridge').attributes['percentage'] == 60
        port = bridge.server.sockets[0].getsockname()[1]
        assert await ha.config_entries.async_unload(e.entry_id)
        assert bridge.closed and bridge.token not in _BRIDGES
        # Unload releases the listener so reconfiguration can bind it immediately.
        server = await asyncio.start_server(lambda r,w:w.close(), '0.0.0.0', port)
        server.close()
        await server.wait_closed()
    finally:
        await stop_client(device)


async def test_plugin_acl_and_relay(ha):
    bridge = EmbeddedBridge(ha, entry())
    await bridge.start()
    try:
        plugin = BridgePlugin(SimpleNamespace(config=BridgePlugin.Config(token=bridge.token)))
        physical = SimpleNamespace(client_id=bridge.device_id,username=None,password=None)
        local = SimpleNamespace(client_id='ha',username='homeassistant',password=bridge.token)
        stranger = SimpleNamespace(client_id='intruder',username='homeassistant',password='wrong')
        for session in (physical, local):
            assert await plugin.authenticate(session=session)
            assert await plugin.topic_filtering(session=session,topic=bridge.topic)
            assert await plugin.topic_filtering(session=session,topic=bridge.prefix+'/#')
            assert not await plugin.topic_filtering(session=session,topic='/things/#')
            assert not await plugin.topic_filtering(session=session,topic='/things/other/shadow')
        assert not await plugin.authenticate(session=stranger)
        assert not await plugin.topic_filtering(session=stranger,topic=bridge.topic)
        cloud = MagicMock()
        bridge.cloud = cloud
        bridge.to_cloud(bridge.topic, b'offline')
        cloud.is_connected.return_value = False
        bridge.to_cloud(bridge.topic, b'dropped')
        cloud.is_connected.return_value = True
        cloud.publish.reset_mock()
        bridge.broker.internal_message_broadcast = AsyncMock()
        bridge._cloud_connected(cloud, None, {}, 1)
        cloud.subscribe.assert_not_called()
        bridge._cloud_connected(cloud, None, {}, 0)
        cloud.subscribe.assert_called_once_with(bridge.prefix+'/#')
        await plugin.on_broker_message_broadcast(client_id='ha', message=SimpleNamespace(topic=bridge.topic,data=b'local'))
        cloud.publish.assert_not_called()
        await plugin.on_broker_message_broadcast(client_id=bridge.device_id, message=SimpleNamespace(topic=bridge.topic,data=b'device'))
        cloud.publish.assert_called_once_with(bridge.topic,b'device',qos=0,retain=False)
        bridge._receive_cloud(bridge.topic, b'device')
        bridge.broker.internal_message_broadcast.assert_not_called()
        bridge._receive_cloud('/things/other/shadow', b'foreign')
        bridge.echoes.clear()
        bridge.echoes.append((b'expired',0))
        bridge.echoes.append((b'unmatched',float('inf')))
        bridge._cloud_message(cloud,None,SimpleNamespace(topic=bridge.topic,payload=b'command'))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        bridge.broker.internal_message_broadcast.assert_awaited_once_with(bridge.topic,b'command',qos=0)
        # In-flight broadcasts drain before broker shutdown; cleanup failures are isolated.
        bridge._receive_cloud(bridge.topic, b'last')
        cloud.disconnect.side_effect=OSError('gone')
    finally:
        await bridge.stop()
    bridge._receive_cloud(bridge.topic,b'late')
    bridge.to_cloud(bridge.topic,b'late')


async def test_cloud_setup_nonblocking_and_certificate_persistence(ha, monkeypatch):
    cloud = MagicMock()
    monkeypatch.setattr('custom_components.purethink.bridge.mqtt.Client', lambda **kwargs:cloud)
    bridge = EmbeddedBridge(ha,entry(cloud=True))
    await bridge.start()
    try:
        cloud.connect_async.assert_called_once_with('dapt.iptime.org',8885,60)
        cloud.loop_start.assert_called_once()
        directory = ha.config.path('.storage','purethink_bridge',bridge.entry.entry_id)
        from pathlib import Path
        before = (Path(directory)/'cert.pem').read_bytes()
        await ha.async_add_executor_job(_tls_context,directory)
        assert (Path(directory)/'cert.pem').read_bytes() == before
        assert (Path(directory)/'key.pem').stat().st_mode & 0o777 == 0o600
    finally:
        await bridge.stop()


async def test_occupied_listener_cleans_up(ha):
    occupied = await asyncio.start_server(lambda r,w:w.close(),'0.0.0.0',0)
    port = occupied.sockets[0].getsockname()[1]
    e = entry(port=port)
    try:
        await ha.config_entries.async_add(e)
        assert e.state == ConfigEntryState.SETUP_RETRY
        assert e.entry_id not in ha.data['purethink']
        assert not _BRIDGES
    finally:
        occupied.close()
        await occupied.wait_closed()


async def test_bridge_config_and_reconfigure(runtime):
    hass, entries, clients = runtime
    flow = await hass.config_entries.flow.async_init('purethink', context={'source':'user'})
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'],
        {'friendly_name':'New bridge','device_id':'DIV01-NEW','mqtt_mode':'bridge'})
    assert flow['step_id'] == 'bridge'
    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(flow['flow_id'],{'bridge_port':0,'bridge_cloud':False})
    # Prevent actual setup here (socket behavior is exercised in separate tests).
    hass.config_entries.async_setup = AsyncMock(return_value=True)
    result = await hass.config_entries.flow.async_configure(flow['flow_id'],{'bridge_port':8885,'bridge_cloud':False})
    assert result['data']['bridge_cloud'] is False
    duplicate = await hass.config_entries.flow.async_init('purethink',context={'source':'user'})
    duplicate = await hass.config_entries.flow.async_configure(duplicate['flow_id'],
        {'friendly_name':'Second','device_id':'DIV01-SECOND','mqtt_mode':'bridge'})
    duplicate = await hass.config_entries.flow.async_configure(duplicate['flow_id'],{'bridge_port':8885,'bridge_cloud':False})
    assert duplicate['errors']['base'] == 'bridge_port_in_use'
    new_entry=result['result']
    flow=await hass.config_entries.flow.async_init('purethink', context={'source':'reconfigure','entry_id':new_entry.entry_id})
    flow=await hass.config_entries.flow.async_configure(flow['flow_id'], {'mqtt_mode':'manufacturer'})
    assert flow['type']=='abort' and flow['reason']=='reconfigure_successful'
    assert 'bridge_port' not in new_entry.data and 'bridge_cloud' not in new_entry.data


async def test_device_id_sensor_follows_reconfigure(runtime):
    hass, entries, _ = runtime
    sensor='sensor.runtime_0_device_id'
    registry=er.async_get(hass)
    assert hass.states.get(sensor).state == 'DIV01-ABC000'
    assert registry.async_get(sensor).entity_category.value == 'diagnostic'
    flow=await hass.config_entries.flow.async_init('purethink',context={'source':'reconfigure','entry_id':entries[0].entry_id})
    result=await hass.config_entries.flow.async_configure(flow['flow_id'],{'device_id':'DIV01-CHANGED'})
    assert result['reason']=='reconfigure_successful'
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == 'DIV01-CHANGED'
    assert registry.async_get(sensor).unique_id == 'DIV01-CHANGED_device_id'


async def test_bridge_step_without_pending(ha):
    from custom_components.purethink.config_flow import PurethinkConfigFlow
    flow=PurethinkConfigFlow()
    flow.hass=ha
    assert (await flow.async_step_bridge())['step_id']=='user'


async def test_shutdown_drains_inflight_messages_and_is_idempotent(ha):
    bridge=EmbeddedBridge(ha,entry())
    await bridge.start()
    release=asyncio.Event()
    async def pending(*args, **kwargs):
        await release.wait()
    bridge.broker.internal_message_broadcast=pending
    bridge._receive_cloud(bridge.topic,b'pending')
    stopping=asyncio.create_task(bridge.stop())
    await asyncio.sleep(0)
    assert not stopping.done()
    release.set()
    await stopping
    await bridge.stop()
    assert bridge.token not in _BRIDGES


async def test_stop_event_closes_idle_tls_connection(ha):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP
    e=entry()
    await ha.config_entries.async_add(e)
    bridge=ha.data['purethink'][e.entry_id]['bridge']
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname=False
    context.verify_mode=ssl.CERT_NONE
    reader,writer=await asyncio.open_connection('127.0.0.1',bridge.server.sockets[0].getsockname()[1],ssl=context)
    try:
        ha.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await until(lambda: bridge.closed and bridge.token not in _BRIDGES)
        with suppress(ConnectionResetError):
            assert await asyncio.wait_for(reader.read(),2)==b''
    finally:
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()


async def test_real_manufacturer_relay_and_echo_suppression(ha, monkeypatch):
    from functools import partial
    from amqtt.broker import Broker
    # Isolated TLS MQTT server replaces the manufacturer; never contacts production.
    cloud_broker=Broker({'listeners':{'default':{'type':'external'}},
        'plugins':{'amqtt.plugins.authentication.AnonymousAuthPlugin':{'allow_anonymous':True}}})
    await cloud_broker.start()
    context=await ha.async_add_executor_job(_tls_context,ha.config.path('test_cloud_certs'))
    server=await asyncio.start_server(partial(cloud_broker.stream_connected,listener_name='default'),
        '127.0.0.1',0,ssl=context)
    monkeypatch.setattr('custom_components.purethink.bridge.MQTT_MANUFACTURER_BROKER','127.0.0.1')
    monkeypatch.setattr('custom_components.purethink.bridge.MQTT_MANUFACTURER_PORT',server.sockets[0].getsockname()[1])
    e=entry(cloud=True)
    device=app=None
    try:
        await ha.config_entries.async_add(e)
        assert e.state==ConfigEntryState.LOADED
        bridge=ha.data['purethink'][e.entry_id]['bridge']
        await until(lambda: bridge.prefix+'/#' in cloud_broker._subscriptions)
        device,messages=await connect_device(bridge)
        cloud_view=SimpleNamespace(device_id='cloud-app',prefix=bridge.prefix,server=server)
        app,cloud_messages=await connect_device(cloud_view)
        payload=json.dumps({'type':'MCU','contents':'A8A81721B100C0'+'00'*16}).encode()
        device.publish(bridge.topic,payload,qos=1)
        await until(lambda: (bridge.topic,payload) in cloud_messages)
        await until(lambda: ha.states.get('fan.bridge').state=='on')
        # The upstream broker echoes the forwarded packet back to the bridge.
        await until(lambda: not bridge.echoes)
        assert messages.count((bridge.topic,payload))==1
        command=b'{"type":"CMD","contents":"cloud-command"}'
        app.publish(bridge.topic,command,qos=1)
        await until(lambda: (bridge.topic,command) in messages)
        assert messages.count((bridge.topic,command))==1
        assert ha.states.get('fan.bridge').state=='on'
        extra=bridge.prefix+'/firmware/status'
        device.publish(extra,b'update-status')
        await until(lambda: (extra,b'update-status') in cloud_messages)
        await until(lambda: not bridge.echoes)
        # Manufacturer loss must not prevent subsequent local telemetry or HA commands.
        await stop_client(app)
        app=None
        server.close()
        server.close_clients()
        await cloud_broker.shutdown()
        await server.wait_closed()
        await until(lambda: not bridge.cloud.is_connected())
        device.publish(bridge.topic,json.dumps({'type':'MCU','contents':'A8A81721D100C0'+'00'*16}))
        await until(lambda: ha.states.get('fan.bridge').attributes['percentage']==100)
        await ha.services.async_call('fan','set_percentage',{'entity_id':'fan.bridge','percentage':20},blocking=True)
        await until(lambda: any(b'A8A81722' in p for _,p in messages))
    finally:
        for client in (device,app):
            if client:
                await stop_client(client)
        if e.state==ConfigEntryState.LOADED:
            await ha.config_entries.async_unload(e.entry_id)
        server.close()
        server.close_clients()
        if cloud_broker.transitions.state=='started':
            await cloud_broker.shutdown()
        await server.wait_closed()


async def test_real_bridge_reconfiguration_preserves_entities_and_changes_port(ha):
    import socket
    e=entry()
    await ha.config_entries.async_add(e)
    old_bridge=ha.data['purethink'][e.entry_id]['bridge']
    old_port=old_bridge.server.sockets[0].getsockname()[1]
    original={item.entity_id:item.unique_id for item in er.async_entries_for_config_entry(er.async_get(ha),e.entry_id)}
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        new_port=reservation.getsockname()[1]
    flow=await ha.config_entries.flow.async_init('purethink',context={'source':'reconfigure','entry_id':e.entry_id})
    flow=await ha.config_entries.flow.async_configure(flow['flow_id'],{'mqtt_mode':'bridge'})
    assert flow['step_id']=='bridge' and not old_bridge.closed
    result=await ha.config_entries.flow.async_configure(flow['flow_id'],{'bridge_port':new_port,'bridge_cloud':False})
    assert result['reason']=='reconfigure_successful'
    await ha.async_block_till_done()
    assert e.state==ConfigEntryState.LOADED
    new_bridge=ha.data['purethink'][e.entry_id]['bridge']
    assert old_bridge.closed and new_bridge.token!=old_bridge.token
    assert new_bridge.server.sockets[0].getsockname()[1]==new_port
    assert {item.entity_id:item.unique_id for item in er.async_entries_for_config_entry(er.async_get(ha),e.entry_id)}==original
    freed=await asyncio.start_server(lambda r,w:w.close(),'0.0.0.0',old_port)
    freed.close()
    await freed.wait_closed()
    device,messages=await connect_device(new_bridge)
    try:
        device.publish(new_bridge.topic,json.dumps({'type':'MCU','contents':'A8A81721B100C0'+'00'*16}))
        await until(lambda: ha.states.get('fan.bridge').state=='on')
    finally:
        await stop_client(device)


async def test_cancelled_cloud_start_waits_for_thread_before_cleanup(ha, monkeypatch):
    import threading
    entered=threading.Event()
    finish=threading.Event()
    bridge=EmbeddedBridge(ha,entry(cloud=True))
    def slow_start():
        entered.set()
        finish.wait(5)
        bridge.cloud=MagicMock()
    monkeypatch.setattr(bridge,'_start_cloud',slow_start)
    task=asyncio.create_task(bridge.start())
    try:
        await until(entered.is_set)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        finish.set()
        await bridge.stop()
    bridge.cloud.loop_stop.assert_called_once()
    assert bridge.token not in _BRIDGES


async def test_failed_certificate_and_broker_start_cleanup(ha, monkeypatch):
    e=entry()
    monkeypatch.setattr('custom_components.purethink.bridge._tls_context',MagicMock(side_effect=OSError('cert denied')))
    await ha.config_entries.async_add(e)
    assert e.state==ConfigEntryState.SETUP_RETRY
    assert not _BRIDGES and e.entry_id not in ha.data['purethink']


async def test_real_listener_rejects_unknown_client_and_broad_subscription(ha):
    bridge=EmbeddedBridge(ha,entry())
    await bridge.start()
    device=None
    try:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname=False
        context.verify_mode=ssl.CERT_NONE
        reader,writer=await asyncio.open_connection('127.0.0.1',bridge.server.sockets[0].getsockname()[1],ssl=context)
        try:
            client_id=b'unknown-client'
            body=b'\x00\x04MQTT\x04\x02\x00\x3c'+len(client_id).to_bytes(2,'big')+client_id
            writer.write(b'\x10'+bytes([len(body)])+body)
            await writer.drain()
            with suppress(ConnectionResetError):
                response=await asyncio.wait_for(reader.read(4),2)
                assert response in (b'',b'\x20\x02\x00\x05')
        finally:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
        device,_=await connect_device(bridge)
        results=[]
        loop=asyncio.get_running_loop()
        device.on_subscribe=lambda c,u,mid,qos:loop.call_soon_threadsafe(results.append,qos)
        device.subscribe('/things/#',qos=1)
        await until(lambda:results)
        assert tuple(results[0])==(0x80,)
        device.publish('/things/other/shadow',b'not-allowed',qos=1,retain=True)
        await asyncio.to_thread(device.publish(bridge.topic,b'allowed',qos=1).wait_for_publish,2)
        assert '/things/other/shadow' not in bridge.broker._retained_messages
    finally:
        if device:
            await stop_client(device)
        await bridge.stop()


async def test_cancelled_qos_waiter_does_not_interrupt_broker_shutdown(ha, monkeypatch):
    from amqtt.broker import Broker
    bridge=EmbeddedBridge(ha,entry())
    await bridge.start()
    original=Broker._shutdown_broadcast_loop
    async def child_cancelled(broker):
        await original(broker)
        raise asyncio.CancelledError
    monkeypatch.setattr(Broker,'_shutdown_broadcast_loop',child_cancelled)
    await bridge.stop()
    assert bridge.broker.transitions.state=='stopped'
    assert bridge.broker._session_monitor_task.cancelling()
    assert bridge.token not in _BRIDGES


async def test_broker_shutdown_preserves_caller_cancellation(monkeypatch):
    from amqtt.broker import Broker
    from custom_components.purethink.bridge import _BridgeBroker
    entered=asyncio.Event()
    async def blocked(broker):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(Broker,'_shutdown_broadcast_loop',blocked)
    broker=object.__new__(_BridgeBroker)
    task=asyncio.create_task(broker._shutdown_broadcast_loop())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
