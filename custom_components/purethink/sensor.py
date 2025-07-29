import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    device_info = entry_data["device"]
    sensors = [
        AirQualitySensor(config_entry, device_info, "co2", "ppm", "mdi:molecule-co2"),
        AirQualitySensor(config_entry, device_info, "pm1", "µg/m³", "mdi:weather-dust"),
        AirQualitySensor(config_entry, device_info, "pm25", "µg/m³", "mdi:weather-dust"),
        AirQualitySensor(config_entry, device_info, "pm10", "µg/m³", "mdi:weather-dust"),
        AirQualitySensor(config_entry, device_info, "odor", "level", "mdi:scent"),
        WifiSensor(config_entry, device_info, "wifi", "%", "mdi:wifi"),
        FilterSensor(config_entry, device_info, "prefilter", "시간", "mdi:air-filter"),
        FilterSensor(config_entry, device_info, "hepafilter", "시간", "mdi:air-filter"),
        AlarmSensor(config_entry, device_info, "filter", None, "mdi:alert-circle-outline"),
        AlarmSensor(config_entry, device_info, "fan", None, "mdi:fan-alert")
    ]
    async_add_entities(sensors)

class BaseSensor(SensorEntity):

    def __init__(self, entry, device_info, sensor_type, unit=None, icon=None):
        self._entry = entry
        self._device_info = device_info
        self._sensor_type = sensor_type
        config = entry.data
        self._attr_unique_id = f"{config['device_id']}_{sensor_type}"
        self._attr_name = f"{config['friendly_name']} {sensor_type.replace('_', ' ').title()}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry.entry_id}",
                self._update_state
            )
        )

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id].get("state", {})
        self._attr_native_value = state.get(self._sensor_type, None)
        self._attr_available = self._attr_native_value is not None
        self.schedule_update_ha_state()

class AirQualitySensor(BaseSensor):

    def __init__(self, entry, sensor_type, unit, icon):
        super().__init__(entry, sensor_type, unit, icon)

class WifiSensor(BaseSensor):
    def __init__(self, entry, sensor_type, unit, icon):
        super().__init__(entry, sensor_type, unit, icon)

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id].get("state", {})
        raw_value = state.get(self._sensor_type, 0)
        self._attr_native_value = int((raw_value / 7) * 100) if raw_value else 0
        self._attr_available = True
        self.schedule_update_ha_state()
        
class FilterSensor(BaseSensor):

    def __init__(self, entry, filter_type, unit, icon):
        super().__init__(entry, f"{filter_type}", unit, icon)
        self.filter_type = filter_type
        
    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id].get("state", {})
        self._attr_native_value = state.get(f"{self.filter_type}_hours", None)
        self._attr_available = self._attr_native_value is not None
        self.schedule_update_ha_state()

    @property
    def extra_state_attributes(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id].get("state", {})
        return {
            "reset_needed": state.get(f"{self.filter_type}_reset", False)
        }

class AlarmSensor(BaseSensor):

    def __init__(self, entry, alarm_type, unit=None, icon=None):
        super().__init__(entry, f"{alarm_type}_alarm", unit, icon)
        self._alarm_type = alarm_type

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id].get("state", {})
        self._attr_native_value = "on" if state.get(f"{self._alarm_type}_alarm") else "off"
        self._attr_available = True
        self.schedule_update_ha_state()
        
