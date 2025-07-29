import logging
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    """Binary Sensor 플랫폼 설정"""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    device_info = entry_data["device"]
    sensors = [
        PureThinkModeSensor(config_entry, device_info, "Auto", "auto"),
        PureThinkModeSensor(config_entry, device_info, "Manual", "manual"),
        PureThinkModeSensor(config_entry, device_info, "Sleep", "sleep"),
    ]
    async_add_entities(sensors)

class PureThinkModeSensor(BinarySensorEntity):
    """Device Mode에 따라 활성화되는 센서"""

    def __init__(self, entry, device_info, mode_name, entity_id_suffix):
        """초기화"""
        self._entry = entry
        self._device_info = device_info
        self._mode_name = mode_name
        config = entry.data
        self._attr_unique_id = f"{config['device_id']}_{entity_id_suffix}"
        self._attr_name = f"{config['friendly_name']} {mode_name} Mode"
        self._attr_is_on = False
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    @property
    def icon(self):
        if self._mode_name == "Auto":
            return "mdi:robot"
        elif self._mode_name == "Sleep":
            return "mdi:power-sleep"
        else:  # Manual 모드
            return "mdi:account"

    async def async_added_to_hass(self):
        """상태 업데이트 신호 구독"""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry.entry_id}",
                self._handle_update
            )
        )

    def _handle_update(self):
        """현재 모드 확인 후 센서 상태 업데이트"""
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]

        if state.get("power", 1) == 0:
            self._attr_is_on = False
            _LOGGER.debug(f"[BinarySensor] Power가 꺼져 있음 - {self._mode_name} 센서 Off")
        else:
            device_mode = (
                "Auto" if state.get("ai_mode") == 1 else
                f"Sleep {state.get('sleep_mode')}" if state.get("sleep_mode") in [1, 2, 3] else
                "Manual"
            )

            self._attr_is_on = device_mode == self._mode_name or (self._mode_name == "Sleep" and "Sleep" in device_mode)

        self._attr_available = True
        self.schedule_update_ha_state()
