
import logging
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util.percentage import ordered_list_item_to_percentage, percentage_to_ordered_list_item
from .const import DOMAIN, FAN_SPEEDS
from .protocol import generate_command
from . import mqtt_client

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    device_info = entry_data["device"]
    command_topic = entry_data["command_topic"]
    async_add_entities([PurethinkFan(config_entry, entry_data, device_info, command_topic)])

class PurethinkFan(FanEntity):
    _attr_preset_modes = ["Manual", "Auto", "Sleep 1", "Sleep 2", "Sleep 3"]
    _attr_supported_features = FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    _attr_speed_count = len(FAN_SPEEDS) - 1
    
    def __init__(self, config_entry, entry_data, device_info, command_topic):
        self._config_entry = config_entry
        self._config = config_entry.data
        self._device_info = device_info
        self._command_topic = command_topic
        self._attr_unique_id = f"{self._config['device_id']}_fan"
        self._attr_name = self._config['friendly_name']
        self._attr_is_on = entry_data["state"].get("power", 0) == 1 and (int(entry_data["state"].get("fan_in", 0)) == 1 or int(entry_data["state"].get("fan_out", 0)) == 1)
        self._attr_percentage = 0

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._config_entry.entry_id}",
                self._handle_update
            )
        )

    def _handle_update(self):
        state = self.hass.data[DOMAIN][self._config_entry.entry_id]["state"]
        self._attr_is_on = state.get("power", 0) == 1 and (int(state.get("fan_in", 0)) == 1 or int(state.get("fan_out", 0)) == 1)

        # 장치로부터 받은 숫자 속도(0-5)를 백분율로 변환
        fan_speed_index = state.get("fan_speed", 0)
        self._attr_percentage = ordered_list_item_to_percentage(FAN_SPEEDS[1:], FAN_SPEEDS[fan_speed_index]) if fan_speed_index != 0 else 0

        ai_mode = state.get("ai_mode", 0)
        sleep_mode = state.get("sleep_mode", 0)

        if ai_mode == 1:
            self._attr_preset_mode = "Auto"
        elif sleep_mode in [1, 2, 3]:
            self._attr_preset_mode = f"Sleep {sleep_mode}"
        else:
            self._attr_preset_mode = "Manual"

        self.schedule_update_ha_state()
        
    async def async_toggle(self, **kwargs) -> None:
        if self._attr_is_on is True:
            await self.async_turn_off(kwargs)
        else:
            await self.async_turn_on(kwargs)

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs):
        payload = generate_command(self._config['device_id'], self.hass, power=1, fan_mode="흡/배기")
        mqtt_client.publish(self._command_topic, payload, qos=1)
        
        if percentage is not None:
            await self.async_set_percentage(percentage)
        elif preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)

    async def async_turn_off(self, **kwargs):
        payload = generate_command(self._config['device_id'], self.hass, fan_mode="환기 꺼짐")
        mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_set_percentage(self, percentage: int):
        speed = FAN_SPEEDS.index(percentage_to_ordered_list_item(FAN_SPEEDS[1:], percentage)) if percentage != 0 else 0
        payload = generate_command(self._config['device_id'], self.hass, fan_speed=speed)
        mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_set_preset_mode(self, preset_mode: str):
        payload = generate_command(self._config['device_id'], self.hass, mode=preset_mode)
        mqtt_client.publish(self._command_topic, payload, qos=1)

    @property
    def device_info(self):
        return self._device_info
