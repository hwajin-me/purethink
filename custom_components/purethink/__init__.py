import asyncio
import logging
import json
import ssl
import paho.mqtt.client as mqtt

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_MQTT_HOST,
    CONF_MQTT_MODE,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    DOMAIN,
    MQTT_MODE_BRIDGE,
    MQTT_LOCAL_DEFAULT_PORT,
    MQTT_MANUFACTURER_BROKER,
    MQTT_MANUFACTURER_PORT,
    MQTT_MODE_LOCAL,
    MQTT_MODE_MANUFACTURER,
)
from .protocol import parse_status_packet, generate_command
from .recovery import ConnectionRecovery

_LOGGER = logging.getLogger(__name__)

# MQTT Broker 정보
def _create_tls_context() -> ssl.SSLContext:
    """TLS 설정 (인증서 검증 비활성화)"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _get_mqtt_config(entry: ConfigEntry) -> dict:
    """Return MQTT connection settings for this config entry."""
    mqtt_mode = entry.data.get(CONF_MQTT_MODE, MQTT_MODE_MANUFACTURER)

    if mqtt_mode == MQTT_MODE_LOCAL:
        return {
            "mode": MQTT_MODE_LOCAL,
            "host": entry.data.get(CONF_MQTT_HOST),
            "port": int(entry.data.get(CONF_MQTT_PORT, MQTT_LOCAL_DEFAULT_PORT)),
            "username": entry.data.get(CONF_MQTT_USERNAME) or None,
            "password": entry.data.get(CONF_MQTT_PASSWORD) or None,
            "tls": False,
        }

    return {
        "mode": MQTT_MODE_MANUFACTURER,
        "host": MQTT_MANUFACTURER_BROKER,
        "port": MQTT_MANUFACTURER_PORT,
        "username": None,
        "password": None,
        "tls": True,
    }


def _on_connect(client, userdata, flags, rc):
    """MQTT 연결 시 실행"""
    connected = userdata.get("connected")
    if connected is not None:
        def complete_connection():
            if not connected.done():
                connected.set_result(rc)
        userdata["hass"].loop.call_soon_threadsafe(complete_connection)
    if rc == 0:
        _LOGGER.info("[MQTT] 연결 성공 (%s)", userdata.get("entry_id"))
        status_topic = userdata.get("status_topic")
        if status_topic:
            client.subscribe(status_topic)
            _LOGGER.debug("[MQTT] subscribed: %s", status_topic)
    else:
        _LOGGER.error("[MQTT] 연결 실패 (코드 %s)", rc)


def _on_connect_fail(client, userdata):
    _LOGGER.warning("[MQTT] 연결 실패 (%s); 5~60초 간격으로 자동 재시도", userdata.get("entry_id"))


def _on_disconnect(client, userdata, rc):
    if rc != 0:
        _LOGGER.warning("[MQTT] 연결 끊김 (%s, 코드 %s); 자동 재접속", userdata.get("entry_id"), rc)


def _on_message(client, userdata, msg):
    """MQTT 메시지 수신 핸들러"""
    hass: HomeAssistant = userdata.get("hass")
    entry_id: str = userdata.get("entry_id")

    if hass is None or entry_id is None:
        _LOGGER.error("[MQTT] userdata 누락: hass=%s entry_id=%s", hass, entry_id)
        return

    try:
        payload = msg.payload.decode("utf-8")
        payload_json = json.loads(payload)

        if "contents" not in payload_json:
            _LOGGER.warning("[MQTT] 잘못된 메시지 형식 (contents 없음): %s", payload_json)
            return

        payload_hex = payload_json["contents"]

        if payload_json.get("type") == "CMD":
            return

        if not isinstance(payload_hex, str) or not payload_hex.startswith(("A8A81721", "A8A81722")):
            return

        # Reject incomplete/non-hex frames before they can replace valid state.
        if len(payload_hex) != 46 or len(bytes.fromhex(payload_hex)) != 23:
            return
        parsed = parse_status_packet(payload_hex)
        if parsed.get("fan_speed", 0) > 5 or parsed.get("pressure_mode", 0) > 2:
            _LOGGER.warning("[MQTT] 범위를 벗어난 상태 패킷 무시")
            return

        if not parsed:
            _LOGGER.error("[MQTT] 상태 패킷 파싱 실패: %s", payload_hex)
            return

        full_state = {
            **parsed,
            "fan_alarm": bool(parsed.get("fan1_alarm") or parsed.get("fan2_alarm")),
            "prefilter_hours": parsed.get("prefilter", {}).get("hours", 0),
            "prefilter_reset": parsed.get("prefilter", {}).get("reset_flag", 0),
            "hepafilter_hours": parsed.get("hepafilter", {}).get("hours", 0),
            "hepafilter_reset": parsed.get("hepafilter", {}).get("reset_flag", 0),
        }

        def update_state():
            entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
            if entry_data is None or entry_data.get("mqtt") is not client:
                return
            entry_data["state"] = full_state
            async_dispatcher_send(hass, f"{DOMAIN}_state_update_{entry_id}")

        hass.loop.call_soon_threadsafe(update_state)

    except Exception as e:
        _LOGGER.error("[MQTT] 메시지 처리 실패: %s", e, exc_info=True)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config Entry 설정"""
    _LOGGER.debug("Initializing entry: %s", entry.entry_id)
    config = entry.data
    device_id = config["device_id"]

    base_topic = f"/things/{device_id}"
    status_topic = f"{base_topic}/shadow"
    command_topic = f"{base_topic}/shadow"

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("_devices", {})  # device_id -> entry_id

    if device_id in hass.data[DOMAIN]["_devices"]:
        _LOGGER.error("Already loaded device_id: %s", device_id)
        return False

    hass.data[DOMAIN][entry.entry_id] = {
        "state": {},
        "status_topic": status_topic,
        "command_topic": command_topic,
        "device_id": device_id,
        "mqtt": None,
        "device": {
            "identifiers": {(DOMAIN, device_id)},
            "name": config["friendly_name"],
            "manufacturer": "Purethink",
            "model": "Air Ventilator",
        },
    }
    hass.data[DOMAIN]["_devices"][device_id] = entry.entry_id

    client = mqtt.Client()
    client.enable_logger(_LOGGER)
    connected = hass.loop.create_future()
    client.user_data_set({
        "hass": hass, "entry_id": entry.entry_id,
        "status_topic": status_topic, "connected": connected,
    })
    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_connect_fail = _on_connect_fail
    client.on_disconnect = _on_disconnect

    hass.data[DOMAIN][entry.entry_id]["mqtt"] = client

    def connect():
        if mqtt_config["username"] or mqtt_config["password"]:
            client.username_pw_set(mqtt_config["username"], mqtt_config["password"])
        if mqtt_config["tls"]:
            client.tls_set_context(_create_tls_context())
        client.reconnect_delay_set(min_delay=5, max_delay=60)
        client.connect_async(mqtt_config["host"], mqtt_config["port"], 60)
        client.loop_start()

    connect_job = None
    try:
        if config.get(CONF_MQTT_MODE) == MQTT_MODE_BRIDGE:
            from .bridge import EmbeddedBridge
            bridge = EmbeddedBridge(hass, entry)
            hass.data[DOMAIN][entry.entry_id]['bridge'] = bridge
            mqtt_config = await bridge.start()
            entry.async_on_unload(hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, bridge.stop))
        else:
            mqtt_config = _get_mqtt_config(entry)
        connect_job = hass.async_add_executor_job(connect)
        await asyncio.shield(connect_job)
        rc = await asyncio.wait_for(connected, timeout=10)
        if rc != 0:
            raise OSError(f"MQTT broker rejected connection (code {rc})")
    except asyncio.CancelledError:
        # An executor thread cannot be cancelled. Let it finish before stopping
        # the client, otherwise loop_start could run after our cleanup.
        if connect_job is not None:
            try:
                await connect_job
            except Exception:
                pass
        await _async_cleanup_entry(hass, entry)
        raise
    except Exception as err:
        _LOGGER.exception("[MQTT] 연결 설정 실패: %s", entry.entry_id)
        await _async_cleanup_entry(hass, entry)
        raise ConfigEntryNotReady(
            f"MQTT connection failed ({type(err).__name__}: {err}); automatic retry pending"
        ) from err
    finally:
        if not connected.done():
            connected.cancel()

    try:
        await hass.config_entries.async_forward_entry_setups(entry, ["sensor", "switch", "select", "binary_sensor", "fan"])
    except Exception:
        await _async_cleanup_entry(hass, entry)
        raise

    hass.data[DOMAIN][entry.entry_id]["recovery"] = ConnectionRecovery(hass, entry, client)

    async def handle_reset_filter(call):
        try:
            filter_type = (call.data.get("filter_type") or "").strip().lower()
            if filter_type not in ("prefilter", "hepafilter"):
                raise ValueError(f"Invalid filter_type: {filter_type}")
            target_device_id = (call.data.get("device_id") or "").strip()

            _LOGGER.debug("[Service] 필터 리셋 요청: filter_type=%s device_id=%s", filter_type, target_device_id)

            if target_device_id:
                target_entry_id = hass.data[DOMAIN]["_devices"].get(target_device_id)
                if not target_entry_id:
                    raise ValueError(f"Unknown device_id: {target_device_id}")
            else:
                entry_ids = [
                    k for k, v in hass.data[DOMAIN].items()
                    if isinstance(v, dict) and "command_topic" in v
                ]
                if len(entry_ids) != 1:
                    raise ValueError("Multiple devices are configured. Provide device_id in service call.")
                target_entry_id = entry_ids[0]

            entry_data = hass.data[DOMAIN][target_entry_id]
            mqtt_client = entry_data["mqtt"]
            command_topic = entry_data["command_topic"]
            device_id_local = entry_data["device_id"]

            payload = generate_command(device_id_local, hass, filter_reset=filter_type)
            if payload:
                mqtt_client.publish(command_topic, payload, qos=1)
                _LOGGER.debug("[Service] 필터 리셋 명령 전송 ▶ %s", payload)
            else:
                _LOGGER.error("[Service] 필터 리셋 명령 생성 실패")

        except Exception as e:
            _LOGGER.error("[Service] 필터 리셋 처리 중 오류 발생: %s", e, exc_info=True)
            raise

    if not hass.services.has_service(DOMAIN, "reset_filter"):
        hass.services.async_register(DOMAIN, "reset_filter", handle_reset_filter)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config Entry 제거 시 정리"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, ["sensor", "switch", "select", "binary_sensor", "fan"])

    if not unload_ok:
        return False
    await _async_cleanup_entry(hass, entry)
    if not hass.data[DOMAIN].get("_devices"):
        hass.services.async_remove(DOMAIN, "reset_filter")
    return True


async def _async_cleanup_entry(hass, entry):
    """Release only this entry's connection and state."""
    entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if entry_data:
        recovery = entry_data.get("recovery")
        if recovery is not None:
            recovery.stop()
        client = entry_data.get("mqtt")
        if client:
            for cleanup in (client.disconnect, client.loop_stop):
                try:
                    await hass.async_add_executor_job(cleanup)
                except Exception:
                    _LOGGER.debug("[MQTT] disconnect/stop 중 예외 (무시)", exc_info=True)

        bridge = entry_data.get('bridge')
        if bridge is not None:
            await bridge.stop()
        dev_map = hass.data.get(DOMAIN, {}).get("_devices", {})
        device_id = entry_data.get("device_id")
        if device_id and dev_map.get(device_id) == entry.entry_id:
            dev_map.pop(device_id, None)
