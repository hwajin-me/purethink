# custom_components/purethink/__init__.py
"""메인 통합 모듈"""
import logging
import json
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import mqtt
from homeassistant.helpers.dispatcher import async_dispatcher_send
from .const import DOMAIN
from .protocol import parse_status_packet, generate_command

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """컴포넌트 초기화"""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config Entry 설정 (핵심 로직)"""
    _LOGGER.debug(f"Initializing entry: {entry.data}")
    config = entry.data
    device_id = config["device_id"]
    
    # MQTT 토픽 설정
    base_topic = f"/things/{device_id}"
    status_topic = f"{base_topic}/shadow"
    command_topic = f"{base_topic}/shadow"

    # 상태 저장소 초기화
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "state": {},
        "status_topic": status_topic,
        "command_topic": command_topic
    }
    hass.data[DOMAIN][device_id] = {
        "state": {},
        "status_topic": status_topic,
        "command_topic": command_topic
    }

    # MQTT 메시지 핸들러
    async def message_received(msg):
        try:
            payload = msg.payload
            if isinstance(payload, bytes):
                payload_hex = payload.hex()
            elif isinstance(payload, str):
                try:  # JSON 파싱 시도
                    payload = json.loads(payload)
                    payload_hex = bytes.fromhex(payload.get("contents", "")).hex()
                except (json.JSONDecodeError, ValueError):
                    payload_hex = payload.lower()
            
            if payload_hex.startswith('a8a81721'):
                parsed = parse_status_packet(payload_hex)
                full_state = {
                    **parsed,
                    "prefilter_hours": parsed["prefilter"]["hours"],
                    "prefilter_reset": parsed["prefilter"]["reset_flag"],
                    "hepafilter_hours": parsed["hepafilter"]["hours"],
                    "hepafilter_reset": parsed["hepafilter"]["reset_flag"]
                    
                }
                hass.data[DOMAIN][entry.entry_id]["state"] = full_state
                async_dispatcher_send(hass, f"{DOMAIN}_state_update_{entry.entry_id}")

        except Exception as e:
            _LOGGER.error(f"메시지 처리 실패: {str(e)}", exc_info=True)

    # MQTT 구독 & 플랫폼 로드
    await mqtt.async_subscribe(hass, status_topic, message_received)
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch", "select"])
    await async_register_services(hass)
    return True

async def async_register_services(hass):
    """Home Assistant에 서비스 등록"""
    
    async def handle_reset_filter(call):
        """필터 리셋 명령어를 전송하는 서비스 핸들러"""
        filter_type = call.data.get("filter_type")  # "prefilter" 또는 "hepafilter"

        if filter_type not in ["prefilter", "hepafilter"]:
            _LOGGER.error("Invalid filter reset request. filter_type missing or incorrect.")
            return
        
        try:
            entry_data = None
            for entry_id, data in hass.data.get(DOMAIN, {}).items():
                if "command_topic" in data:  # `command_topic`이 있는 첫 번째 기기 찾기
                    entry_data = data
                    break

            if not entry_data:
                _LOGGER.error("[Service] No valid device found in hass.data")
                return
            
            device_id = entry_id
            command_topic = entry_data.get("command_topic")
            
            payload = generate_command(
                device_id,
                hass,
                filter_reset=filter_type
            )
            
            # ✅ `generate_command()`가 실패했을 경우 MQTT 전송하지 않음
            if not payload:
                _LOGGER.error("[Service] Failed to generate filter reset command.")
                return
                
            await mqtt.async_publish(
                hass,
                command_topic,
                payload,
                qos=1
            )
            _LOGGER.info(f"[Service] Filter reset command sent for {device_id}: {payload}")
        except Exception as e:
            _LOGGER.error(f"[Service] 명령 전송 실패: {e}", exc_info=True)
        
    hass.services.async_register(DOMAIN, "reset_filter", handle_reset_filter)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """언로드 처리"""
    await hass.config_entries.async_unload_platforms(entry, ["sensor", "switch", "select"])
    return True