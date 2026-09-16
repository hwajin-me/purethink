"""Offline regression tests using real Home Assistant entities and mocked MQTT."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.purethink import (
    _get_mqtt_config, _create_tls_context, _on_message,
    async_setup_entry, async_unload_entry,
)
from custom_components.purethink.const import DOMAIN, FAN_SPEEDS
from custom_components.purethink.fan import PurethinkFan
from custom_components.purethink.select import BaseSelect, FanModeSelect
from custom_components.purethink.switch import PowerSwitch
from custom_components.purethink.protocol import generate_command
from custom_components.purethink.config_flow import PurethinkConfigFlow


def entry(number, **config):
    return SimpleNamespace(entry_id=f"entry{number}", data={
        "device_id": f"DIV01-00000{number}", "friendly_name": f"Fan {number}",
        "base_id": f"fan_{number}", **config,
    })


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(), async_unload_platforms=AsyncMock(return_value=True))
    clients = []
    def factory():
        client = MagicMock()
        client.loop_start.side_effect = lambda: client.on_connect(
            client, client.user_data_set.call_args.args[0], {}, 0)
        clients.append(client)
        return client
    monkeypatch.setattr("custom_components.purethink.mqtt.Client", factory)
    entries = [entry(1), entry(2, mqtt_mode="local", mqtt_host="192.0.2.1",
                               mqtt_port=1884, mqtt_username="user", mqtt_password="secret")]
    for e in entries:
        assert await async_setup_entry(hass, e)
    yield hass, entries, clients
    for e in entries:
        if e.entry_id in hass.data[DOMAIN]:
            await async_unload_entry(hass, e)


def command_bytes(client):
    return bytes.fromhex(json.loads(client.publish.call_args.args[1])["contents"])


async def test_connections_and_unload(setup):
    hass, entries, clients = setup
    clients[0].connect_async.assert_called_once_with("dapt.iptime.org", 8885, 60)
    clients[0].tls_set_context.assert_called_once()
    clients[1].connect_async.assert_called_once_with("192.0.2.1", 1884, 60)
    clients[1].tls_set_context.assert_not_called()
    clients[1].username_pw_set.assert_called_once_with("user", "secret")
    assert "fan" in hass.config_entries.async_forward_entry_setups.call_args.args[1]
    hass.config_entries.async_unload_platforms.return_value = False
    assert not await async_unload_entry(hass, entries[0])
    clients[0].disconnect.assert_not_called()
    hass.config_entries.async_unload_platforms.return_value = True
    assert await async_unload_entry(hass, entries[0])
    clients[0].disconnect.assert_called_once()
    clients[1].disconnect.assert_not_called()
    assert hass.services.has_service(DOMAIN, "reset_filter")
    await async_unload_entry(hass, entries[1])
    assert not hass.services.has_service(DOMAIN, "reset_filter")


@pytest.mark.parametrize("header", ["A8A81721", "A8A81722"])
async def test_status_and_command_echo(setup, header):
    hass, entries, clients = setup
    user = clients[0].user_data_set.call_args.args[0]
    contents = header + "B120C0" + "00" * 16
    def receive(kind):
        _on_message(clients[0], user, SimpleNamespace(payload=json.dumps(
            {"type": kind, "contents": contents}).encode()))
    receive("CMD")
    await asyncio.sleep(0)
    assert hass.data[DOMAIN][entries[0].entry_id]["state"] == {}
    receive("MCU")
    await asyncio.sleep(0)
    assert hass.data[DOMAIN][entries[0].entry_id]["state"]["fan_speed"] == 3
    assert hass.data[DOMAIN][entries[1].entry_id]["state"] == {}
    await async_unload_entry(hass, entries[0])
    receive("MCU")
    await asyncio.sleep(0)
    assert entries[0].entry_id not in hass.data[DOMAIN]


async def test_filter_target_and_state_isolation(setup):
    hass, entries, clients = setup
    hass.data[DOMAIN][entries[0].entry_id]["state"] = {"power": 1, "fan_speed": 1}
    hass.data[DOMAIN][entries[1].entry_id]["state"] = {
        "power": 1, "fan_speed": 5, "ai_mode": 1, "pressure_mode": 2, "fan_in": 1, "fan_out": 1}
    await hass.services.async_call(DOMAIN, "reset_filter", {
        "filter_type": "hepafilter", "device_id": entries[1].data["device_id"]}, blocking=True)
    clients[0].publish.assert_not_called()
    packet = command_bytes(clients[1])
    assert packet[4:7] == bytes([0xD9, 0x20, 0xC0])
    assert packet[16:18] == bytes([143, 160])
    assert len(packet) == 23
    with pytest.raises(ValueError, match="Multiple devices"):
        await hass.services.async_call(DOMAIN, "reset_filter", {"filter_type": "prefilter"}, blocking=True)
    with pytest.raises(ValueError, match="Unknown device_id"):
        await hass.services.async_call(DOMAIN, "reset_filter", {
            "filter_type": "prefilter", "device_id": "missing"}, blocking=True)
    await async_unload_entry(hass, entries[0])
    await hass.services.async_call(DOMAIN, "reset_filter", {"filter_type": "prefilter"}, blocking=True)
    assert command_bytes(clients[1])[14:16] == bytes([135, 208])


@pytest.mark.parametrize("percentage,speed", [(0, 0), (20, 1), (40, 2), (60, 3), (80, 4), (100, 5)])
async def test_existing_fan_speed_logic(setup, percentage, speed):
    hass, entries, clients = setup
    e = entries[1]; data = hass.data[DOMAIN][e.entry_id]
    data["state"] = {"power": 1, "fan_speed": 4, "ai_mode": 1, "pressure_mode": 2}
    fan = PurethinkFan(e, data, data["device"], data["command_topic"])
    fan.hass = hass
    await fan.async_set_percentage(percentage)
    packet = command_bytes(clients[1])
    assert packet[4] == 0x81 | (speed << 4)
    assert packet[5] == 0x20
    assert packet[6] == (0xC0 if speed else 0)
    clients[0].publish.assert_not_called()
    assert FAN_SPEEDS == ["Off", "Min", "Low", "Medium", "High", "Max"]


@pytest.mark.parametrize("mode,bits", [("Manual", 0), ("Auto", 8), ("Sleep 1", 2), ("Sleep 2", 4), ("Sleep 3", 6)])
async def test_existing_presets(setup, mode, bits):
    hass, entries, clients = setup
    e = entries[0]; data = hass.data[DOMAIN][e.entry_id]
    data["state"] = {"power": 1, "fan_speed": 3, "pressure_mode": 1, "fan_in": 1, "fan_out": 1}
    fan = PurethinkFan(e, data, data["device"], data["command_topic"]); fan.hass = hass
    await fan.async_set_preset_mode(mode)
    packet = command_bytes(clients[0])
    assert packet[4:7] == bytes([0xB1 | bits, 0x10, 0xC0])
    clients[1].publish.assert_not_called()


async def test_select_and_power_routing(setup):
    hass, entries, clients = setup
    e = entries[1]; data = hass.data[DOMAIN][e.entry_id]
    data["state"] = {"power": 1, "fan_speed": 3, "fan_in": 1, "fan_out": 1}
    pressure = BaseSelect(e.data, data["command_topic"], data["device"], "pressure_mode", ["정압", "양압", "음압"], "Pressure Mode", e.entry_id)
    pressure.hass = hass
    await pressure.async_select_option("음압")
    assert command_bytes(clients[1])[5] == 0x20
    mode = FanModeSelect(e, data["command_topic"], data["device"]); mode.hass = hass
    mode._adjust_fan_mode(1, 1, {"fan_speed": 0})
    assert command_bytes(clients[1])[6] == 0
    mode._adjust_fan_mode(0, 0, {"fan_speed": 3})
    assert command_bytes(clients[1])[6] == 0xC0
    power = PowerSwitch(hass, e, data["command_topic"], data["device"])
    await power.async_turn_off()
    assert data["last_fan_speed"] == 3
    data["state"]["power"] = 0
    await power.async_turn_on()
    assert command_bytes(clients[1])[4] == 0xB1
    clients[0].publish.assert_not_called()


async def test_config_flow():
    flow = PurethinkConfigFlow()
    flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda *args: []))
    flow.async_create_entry = MagicMock(side_effect=lambda **kw: kw)
    result = await flow.async_step_user({"friendly_name": " Room ", "device_id": "DIV01-000001", "mqtt_mode": "manufacturer"})
    assert result["data"]["friendly_name"] == "Room"
    flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
    result = await flow.async_step_user({"friendly_name": "Room", "device_id": "DIV01-000001", "mqtt_mode": "local"})
    assert result["step_id"] == "local_mqtt"
    result = await flow.async_step_local_mqtt({"mqtt_host": " ", "mqtt_port": 1883})
    assert result["errors"]["base"] == "host_required"
    result = await flow.async_step_local_mqtt({"mqtt_host": " broker ", "mqtt_port": 1884})
    assert result["data"]["mqtt_host"] == "broker"
    assert _get_mqtt_config(entry(1))["tls"] is True
    assert _create_tls_context().check_hostname is False


async def test_failed_connect_cleanup(setup):
    hass, entries, clients = setup
    clients[0].connect_async.side_effect = OSError("offline")
    await async_unload_entry(hass, entries[0])
    from unittest.mock import patch
    with patch("custom_components.purethink.mqtt.Client", return_value=clients[0]):
        from homeassistant.exceptions import ConfigEntryNotReady
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entries[0])
    assert entries[0].entry_id not in hass.data[DOMAIN]
    assert entries[0].data["device_id"] not in hass.data[DOMAIN]["_devices"]
    clients[1].disconnect.assert_not_called()


async def test_fan_turn_on_off_and_status(setup):
    hass, entries, clients = setup
    e = entries[1]; data = hass.data[DOMAIN][e.entry_id]
    data["state"] = {"power": 1, "fan_speed": 3, "pressure_mode": 2, "fan_in": 1, "fan_out": 1}
    fan = PurethinkFan(e, data, data["device"], data["command_topic"])
    fan.hass = hass
    fan.schedule_update_ha_state = MagicMock()
    fan._handle_update()
    assert fan.percentage == 60
    assert fan.preset_mode == "Manual"
    assert fan.is_on
    await fan.async_turn_off()
    assert command_bytes(clients[1])[4:7] == bytes([0x81, 0x20, 0])
    await fan.async_turn_on(percentage=100)
    assert command_bytes(clients[1])[4:7] == bytes([0xD1, 0x20, 0xC0])
    await fan.async_turn_on(preset_mode="Auto")
    assert command_bytes(clients[1])[4:7] == bytes([0xB9, 0x20, 0xC0])
    clients[0].publish.assert_not_called()


async def test_platform_setup_failure_cleanup(setup):
    hass, entries, clients = setup
    await async_unload_entry(hass, entries[0])
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("platform failure")
    with pytest.raises(RuntimeError, match="platform failure"):
        await async_setup_entry(hass, entries[0])
    assert entries[0].entry_id not in hass.data[DOMAIN]
    clients[-1].disconnect.assert_called_once()
    clients[1].disconnect.assert_not_called()
