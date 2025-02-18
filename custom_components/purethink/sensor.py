import logging
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, ENTITY_ICONS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    sensors = [
        AirQualitySensor(config_entry, "co2", "ppm"),
        AirQualitySensor(config_entry, "pm1", "µg/m³"),
        AirQualitySensor(config_entry, "pm25", "µg/m³"),
        AirQualitySensor(config_entry, "pm10", "µg/m³"),
        AirQualitySensor(config_entry, "tvoc", "ppb"),
        WifiSensor(config_entry),
        FilterSensor(config_entry, "prefilter"),
        FilterSensor(config_entry, "hepafilter"),
        AlarmSensor(config_entry, "filter"),
        AlarmSensor(config_entry, "fan")
    ]
    async_add_entities(sensors)

class BaseSensor(SensorEntity):

    def __init__(self, entry, sensor_type, unit=None, icon=None):
        self._entry = entry
        self._sensor_type = sensor_type
        config = entry.data
        self._attr_unique_id = f"{config['device_id']}_{sensor_type}"
        self._attr_name = f"{config['friendly_name']} {sensor_type.title()}"
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_available = False

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry.entry_id}",
                self._update_state
            )
        )

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]
        self._attr_native_value = state.get(self._sensor_type)
        self._attr_available = True
        self.schedule_update_ha_state()

class AirQualitySensor(BaseSensor):

    def __init__(self, entry, sensor_type, unit):
        icon = "mdi:air-filter" if "pm" in sensor_type else "mdi:scent" if "tvoc" in sensor_type else "mdi:molecule-co2"
        super().__init__(entry, sensor_type, unit, icon)

class WifiSensor(BaseSensor):

    def __init__(self, entry):
        super().__init__(entry, "wifi", None, "mdi:wifi")
        
class FilterSensor(BaseSensor):

    def __init__(self, entry, filter_type):
        super().__init__(entry, f"{filter_type}", "시간", ENTITY_ICONS["filter"])
        self.filter_type = filter_type

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]
        self._attr_native_value = state.get(f"{self.filter_type}_hours")
        self._attr_available = True
        self.schedule_update_ha_state()

    @property
    def extra_state_attributes(self):
        return {
            "reset_needed": self.hass.data[DOMAIN][self._entry.entry_id]["state"].get(f"{self.filter_type}_reset")
        }

class AlarmSensor(BaseSensor):

    def __init__(self, entry, alarm_type):
        super().__init__(entry, f"{alarm_type}_alarm", None, ENTITY_ICONS["alarm"])
        self._alarm_type = alarm_type

    def _update_state(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]
        self._attr_native_value = "on" if state.get(f"{self._alarm_type}_alarm") else "off"
        self._attr_available = True
        self.schedule_update_ha_state()
