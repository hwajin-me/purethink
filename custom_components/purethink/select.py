import logging
from homeassistant.components import mqtt
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, FAN_SPEEDS, PRESSURE_MODES
from .protocol import generate_command
from . import mqtt_client 

_LOGGER = logging.getLogger(__name__)

# custom_components/purethink/select.py

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    command_topic = entry_data["command_topic"]
    device_info = entry_data["device"]
    config = config_entry.data
    
    selects = [
        BaseSelect(config, command_topic, device_info, "pressure_mode", PRESSURE_MODES, "Pressure Mode", config_entry.entry_id),
        DeviceModeSelect(config_entry, command_topic, device_info),
        FanModeSelect(config_entry, command_topic, device_info),
    ]
    async_add_entities(selects)


class BaseSelect(SelectEntity):
    
    def __init__(self, config, command_topic, device_info, entity_type, options, label_suffix, entry_id, default_index=0):
        self._config = config
        self._command_topic = command_topic
        self._device_info = device_info
        self._entity_type = entity_type
        self._entry_id = entry_id
        self._default_index = default_index
        
        self._attr_unique_id = f"{config['device_id']}_{entity_type}"
        self._attr_name = f"{config['friendly_name']} {label_suffix}"
        self._attr_options = options
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry_id}",
                self._handle_update
            )
        )

    def _handle_update(self):
        state = self.hass.data[DOMAIN][self._entry_id]["state"]
        self._attr_current_option = self._attr_options[state.get(self._entity_type, self._default_index)]
        self._attr_available = True
        self.schedule_update_ha_state()

    async def async_select_option(self, option: str):
        try:
            payload = generate_command(
                self._config['device_id'],
                self.hass,
                **{self._entity_type: option}
            )
            mqtt_client.publish(self._command_topic, payload, qos=1)
            _LOGGER.debug(f"[{self.__class__.__name__}] Command sent ▶ {payload}")
        except Exception as e:
            _LOGGER.error(f"[{self.__class__.__name__}] 명령 전송 실패: {e}", exc_info=True)


class DeviceModeSelect(SelectEntity):
    
    MODES = {"Sleep 1": 1, "Sleep 2": 2, "Sleep 3": 3}
    
    def __init__(self, entry, command_topic, device_info):
        self._entry = entry
        self._command_topic = command_topic
        self._device_info = device_info
        config = entry.data
        self._attr_unique_id = f"{config['device_id']}_device_mode"
        self._attr_name = f"{config['friendly_name']} Device Mode"
        self._attr_options = ["Manual", "Auto", "Sleep 1", "Sleep 2", "Sleep 3"]
        self._attr_current_option = "Manual"
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry.entry_id}",
                self._handle_update
            )
        )

    def _handle_update(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]
        ai_mode = state.get("ai_mode", 0)
        sleep_mode = state.get("sleep_mode", 0)

        self._attr_current_option = (
            "Auto" if ai_mode == 1 else
            f"Sleep {sleep_mode}" if sleep_mode in self.MODES.values() else
            "Manual"
        )
        self._attr_available = True
        self.schedule_update_ha_state()

    async def async_select_option(self, option: str):
        try:
            entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
            state = entry_data.get("state", {})

            if state.get("power") == 1 and self._attr_current_option == "Manual" and option != "Manual":
                entry_data["last_fan_speed"] = state.get("fan_speed", 4)
                _LOGGER.debug(f"[DeviceModeSelect] Fan Speed 저장: {entry_data['last_fan_speed']}")

            if option == "Manual":
                last_fan_speed = entry_data.get("last_fan_speed", 4)
                _LOGGER.debug(f"[DeviceModeSelect] Manual 모드 복귀 - 저장된 Fan Speed: {last_fan_speed}")

                mqtt_client.publish(
                    self._command_topic,
                    generate_command(
                        self._entry.data["device_id"],
                        self.hass,
                        device_mode="Manual",
                        fan_speed=last_fan_speed
                    ),
                    qos=1
                )

            payload = generate_command(
                self._entry.data["device_id"],
                self.hass,
                mode=option
            )
            mqtt_client.publish (self._command_topic, payload, qos=1)
            _LOGGER.debug(f"[DeviceModeSelect] Command sent ▶ {payload}")

        except Exception as e:
            _LOGGER.error(f"[DeviceModeSelect] 명령 전송 실패: {e}", exc_info=True)


class FanModeSelect(SelectEntity):
    
    FAN_MODES = {
        (0, 0): "환기 꺼짐",
        (0, 1): "배기 전용",
        (1, 0): "흡기 전용",
        (1, 1): "흡/배기 순환"
    }

    def __init__(self, entry, command_topic, device_info):
        self._entry = entry
        self._command_topic = command_topic
        self._device_info = device_info
        config = entry.data
        self._attr_unique_id = f"{config['device_id']}_fan_mode"
        self._attr_name = f"{config['friendly_name']} Fan Mode"
        self._attr_options = list(self.FAN_MODES.values())
        self._attr_current_option = "Fan In-Off Fan Out-Off"
        self._attr_available = False

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_state_update_{self._entry.entry_id}",
                self._handle_update
            )
        )

    def _handle_update(self):
        state = self.hass.data[DOMAIN][self._entry.entry_id]["state"]
        fan_in = state.get("fan_in", 0)
        fan_out = state.get("fan_out", 0)
        self._attr_current_option = self.FAN_MODES.get((fan_in, fan_out), "Fan In-On Fan Out-On")
        self._attr_available = True
        self.schedule_update_ha_state()

    async def async_select_option(self, option: str):
        try:
            payload = generate_command(
                self._entry.data["device_id"],
                self.hass,
                fan_mode=option
            )
            mqtt_client.publish (self._command_topic, payload, qos=1)
            _LOGGER.debug(f"[FanModeSelect] Command sent ▶ {payload}")
        except Exception as e:
            _LOGGER.error(f"[FanModeSelect] 명령 전송 실패: {e}", exc_info=True)
