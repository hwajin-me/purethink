
import logging
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, FAN_SPEEDS
from .protocol import generate_command
from . import mqtt_client

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    device_info = entry_data["device"]
    command_topic = entry_data["command_topic"]
    async_add_entities([PurethinkFan(config_entry, device_info, command_topic)])

class PurethinkFan(FanEntity):
    _attr_preset_modes = ["Manual", "Auto", "Sleep 1", "Sleep 2", "Sleep 3"]
    _attr_supported_features = FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
    _attr_percentage_step = 20

    def __init__(self, config_entry, device_info, command_topic):
        self._config_entry = config_entry
        self._config = config_entry.data
        self._device_info = device_info
        self._command_topic = command_topic
        self._attr_unique_id = f"{self._config['device_id']}_fan"
        self._attr_name = self._config['friendly_name']
        self._attr_is_on = False
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
        self._attr_is_on = state.get("power", 0) == 1
        self._attr_percentage = round(state.get("fan_speed", 0) * 20)

        ai_mode = state.get("ai_mode", 0)
        sleep_mode = state.get("sleep_mode", 0)

        if ai_mode == 1:
            self._attr_preset_mode = "Auto"
        elif sleep_mode in [1, 2, 3]:
            self._attr_preset_mode = f"Sleep {sleep_mode}"
        else:
            self._attr_preset_mode = "Manual"

        self.schedule_update_ha_state()

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs):
        if percentage is not None:
            speed = int(percentage / 100 * (len(FAN_SPEEDS) - 1))
            await self.async_set_percentage(speed)
        elif preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        else:
            payload = generate_command(self._config['device_id'], self.hass, power=1)
            mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_turn_off(self, **kwargs):
        payload = generate_command(self._config['device_id'], self.hass, power=0)
        mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_set_percentage(self, percentage: int):
        # 백분율(0-100)을 장치 속도(0-5)로 변환
        speed = round(percentage / 20)
        payload = generate_command(self._config['device_id'], self.hass, fan_speed=speed)
        mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_set_preset_mode(self, preset_mode: str):
        payload = generate_command(self._config['device_id'], self.hass, mode=preset_mode)
        mqtt_client.publish(self._command_topic, payload, qos=1)

    @property
    def device_info(self):
        return self._device_info
