import asyncio
import logging
import time

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import mqtt_client
from .const import DOMAIN, PRESSURE_MODES
from .protocol import generate_command

_LOGGER = logging.getLogger(__name__)


# custom_components/purethink/select.py

async def async_setup_entry(hass, config_entry, async_add_entities):
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    command_topic = entry_data["command_topic"]
    device_info = entry_data["device"]
    config = config_entry.data

    selects = [
        BaseSelect(config, command_topic, device_info, "pressure_mode", PRESSURE_MODES, "Pressure Mode",
                   config_entry.entry_id),
        FanModeSelect(config_entry, command_topic, device_info),
    ]
    async_add_entities(selects)


class BaseSelect(SelectEntity):

    def __init__(self, config, command_topic, device_info, entity_type, options, label_suffix, entry_id,
                 default_index=0):
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
        _LOGGER.debug(f"[{self.name}] async_added_to_hass called")
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


class FanModeSelect(SelectEntity):
    FAN_MODES = {
        (0, 0): "환기 꺼짐",
        (0, 1): "배기",
        (1, 0): "흡기",
        (1, 1): "흡/배기"
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
        self._attr_sync = True

    @property
    def device_info(self):
        return self._device_info

    async def async_added_to_hass(self):
        _LOGGER.debug(f"[{self.name}] async_added_to_hass called")
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

        _LOGGER.debug(
            f"[FanModeSelect] State updated: fan_in: {fan_in}, fan_out: {fan_out}, fan_speed: {state.get('fan_speed', 0)}")

        self._attr_current_option = self.FAN_MODES.get((fan_in, fan_out), "Fan In-On Fan Out-On")
        self._attr_available = True
        self.schedule_update_ha_state()

        # 만약에 팬 속도가 0 인데 state 결과가 환기 꺼짐이 아니라면 끔으로 변경
        # 처리 하기 전에 1초 대기
        time.sleep(1)
        self._adjust_fan_mode(fan_in, fan_out, state)

    def _adjust_fan_mode(self, fan_in, fan_out, state):
        if (fan_in != 0 or fan_out != 0) and state.get("fan_speed", 0) == 0:
            _LOGGER.debug(f"[FanModeSelect] 팬 속도 0 감지, 환기 꺼짐으로 변경 {self._entry.data['device_id']}")
            payload = generate_command(self._entry.data["device_id"],
                                       self.hass,
                                       fan_in=0,
                                       fan_out=0,
                                       fan_mode="환기 꺼짐",
                                       mode="Manual")
            mqtt_client.publish(self._command_topic, payload, qos=1)

        elif state.get("fan_speed", 0) > 0 and (fan_in == 0 or fan_out == 0):
            _LOGGER.debug(f"[FanModeSelect] 팬 속도 > 0 감지, 흡/배기로 변경 {self._entry.data['device_id']}")
            payload = generate_command(self._entry.data["device_id"],
                                       self.hass,
                                       fan_in=1,
                                       fan_out=1,
                                       fan_mode="흡/배기",
                                       mode="Manual")
            mqtt_client.publish(self._command_topic, payload, qos=1)

    async def async_select_option(self, option: str):
        try:
            payload = generate_command(
                self._entry.data["device_id"],
                self.hass,
                fan_mode=option
            )
            mqtt_client.publish(self._command_topic, payload, qos=1)
            _LOGGER.debug(f"[FanModeSelect] Command sent ▶ {payload}")
        except Exception as e:
            _LOGGER.error(f"[FanModeSelect] 명령 전송 실패: {e}", exc_info=True)
