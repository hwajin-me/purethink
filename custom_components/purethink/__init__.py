import logging
import json
import ssl
import paho.mqtt.client as mqtt
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from .const import DOMAIN
from .protocol import parse_status_packet, generate_command

_LOGGER = logging.getLogger(__name__)

# MQTT Broker 정보
MQTT_BROKER = "dapt.iptime.org"
MQTT_PORT = 8885

# MQTT 클라이언트 초기화
mqtt_client = mqtt.Client()
mqtt_client.enable_logger(_LOGGER)

def on_connect(client, userdata, flags, rc):
    """MQTT 연결 시 실행"""
    if rc == 0:
        _LOGGER.info("[MQTT] 연결 성공")
        client.subscribe(userdata["status_topic"])
    else:
        _LOGGER.error(f"[MQTT] 연결 실패 (코드 {rc})")

def on_message(client, userdata, msg):
    """MQTT 메시지 수신 핸들러"""

    try:
        payload = msg.payload.decode("utf-8")
        payload_json = json.loads(payload)

        if "contents" not in payload_json:
            _LOGGER.warning(f"[MQTT] 잘못된 메시지 형식 (contents 없음): {payload_json}")
            return

        payload_hex = payload_json["contents"]

        if not payload_hex.startswith("A8A81721"):
            return

        parsed = parse_status_packet(payload_hex)

        if not parsed:
            _LOGGER.error(f"[MQTT] 상태 패킷 파싱 실패: {payload_hex}")
            return

        full_state = {
            **parsed,
            "prefilter_hours": parsed.get("prefilter", {}).get("hours", 0),
            "prefilter_reset": parsed.get("prefilter", {}).get("reset_flag", 0),
            "hepafilter_hours": parsed.get("hepafilter", {}).get("hours", 0),
            "hepafilter_reset": parsed.get("hepafilter", {}).get("reset_flag", 0)
        }

        userdata["hass"].data[DOMAIN][userdata["entry_id"]]["state"] = full_state

        # ✅ 메인 이벤트 루프에서 실행되도록 수정
        hass = userdata["hass"]
        hass.loop.call_soon_threadsafe(
            async_dispatcher_send,
            hass,
            f"{DOMAIN}_state_update_{userdata['entry_id']}"
        )

    except Exception as e:
        _LOGGER.error(f"[MQTT] 메시지 처리 실패: {e}", exc_info=True)




async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config Entry 설정"""
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
    
    # MQTT 클라이언트 설정
    mqtt_client.user_data_set({
        "hass": hass,
        "entry_id": entry.entry_id,
        "status_topic": status_topic
    })
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    # TLS 설정 (인증서 검증 비활성화)
    try:
        context = ssl.create_default_context()
        context.check_hostname = False  # ✅ 호스트 이름 검증 비활성화
        context.verify_mode = ssl.CERT_NONE  # ✅ 인증서 검증 비활성화
        mqtt_client.tls_set_context(context)
    except Exception as e:
        _LOGGER.error(f"[MQTT] TLS 설정 오류: {e}", exc_info=True)
        return False

    # MQTT 연결
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)  # ✅ 동기 연결
        mqtt_client.loop_start()
    except Exception as e:
        _LOGGER.error(f"[MQTT] 연결 실패: {e}", exc_info=True)
        return False

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch", "select", "binary_sensor"])

    #필터 리셋
    async def handle_reset_filter(call):
        try:
            filter_type = call.data.get("filter_type", "").strip().lower()
            _LOGGER.debug(f"[Service] 필터 리셋 요청: filter_type={filter_type}")

            entry_id, entry_data = next(
                (k, v) for k, v in hass.data[DOMAIN].items()
                if isinstance(v, dict) and "state" in v and "command_topic" in v
            )

            device_id = entry_data["command_topic"].split("/")[2]
            command_topic = entry_data["command_topic"]

            payload = generate_command(device_id, hass, filter_reset=filter_type)
            if payload:
                mqtt_client.publish(command_topic, payload, qos=1)
                _LOGGER.debug(f"[Service] 필터 리셋 명령 전송 ▶ {payload}")
            else:
                _LOGGER.error(f"[Service] 필터 리셋 명령 생성 실패")

        except Exception as e:
            _LOGGER.error(f"[Service] 필터 리셋 처리 중 오류 발생: {e}", exc_info=True)
            raise

    hass.services.async_register(DOMAIN, "reset_filter", handle_reset_filter)
    
    return True
