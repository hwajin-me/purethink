import logging
import random
import json
import binascii
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def parse_status_packet(payload: str) -> dict:
    """상태 패킷 파싱 (전체 필드 구현)"""
    _LOGGER.debug(f"Parsing raw packet: {payload[:24]}...")
    try:
        return {
            # 5번째 바이트 (0x21) - 전원, 팬 속도, AI 모드, 수면 모드, 입력 감지
            'power': _parse_bits(payload[8:10], 0, 1),
            'fan_speed': _parse_bits(payload[8:10], 1, 3),
            'ai_mode': _parse_bits(payload[8:10], 4, 1),
            'sleep_mode': _parse_bits(payload[8:10], 5, 2),
            'input_occurred': _parse_bits(payload[8:10], 7, 1),

            # 6번째 바이트 (0x22) - 악취(odor), 압력 모드, WiFi 상태
            'odor': _parse_bits(payload[10:12], 0, 2),
            'pressure_mode': _parse_bits(payload[10:12], 2, 2),
            'wifi': _parse_bits(payload[10:12], 5, 3),

            # 7번째 바이트 (0x23) - 팬 흡기, 팬 배기, 예약된 비트
            'fan_in': _parse_bits(payload[12:14], 0, 1),
            'fan_out': _parse_bits(payload[12:14], 1, 1),
            'reserved_bits': _parse_bits(payload[12:14], 2, 6),

            # 8번째 바이트 (0x24) - 알람 상태
            'fan1_alarm': _parse_bits(payload[14:16], 0, 1),
            'fan2_alarm': _parse_bits(payload[14:16], 1, 1),
            'dust_sensor_alarm': _parse_bits(payload[14:16], 2, 1),
            'co2_sensor_alarm': _parse_bits(payload[14:16], 3, 1),
            'filter_alarm': _parse_bits(payload[14:16], 4, 1),
            'heat_exchanger_alarm': _parse_bits(payload[14:16], 5, 1),
            
            # 9-14바이트: 측정값
            'co2': _parse_bits(payload[16:28], 1, 13),
            'pm1': _parse_bits(payload[16:28], 14, 10),
            'pm25': _parse_bits(payload[16:28], 24, 10),
            'pm10': _parse_bits(payload[16:28], 34, 10),
            
            # 15-18바이트: 필터
            'prefilter': _parse_filter(payload[28:36], 2, 14),
            'hepafilter': _parse_filter(payload[28:36], 18, 14)
        }
    except Exception as e:
        _LOGGER.error(f"Packet parsing failed: {str(e)}", exc_info=True)
        raise

def _parse_bits(hex_str: str, start_bit: int, length: int) -> int:
    """비트 단위 파싱"""
    try:
        full_bits = bin(int(hex_str, 16))[2:].zfill(len(hex_str)*4)
        return int(full_bits[start_bit:start_bit+length], 2)
    except ValueError as e:
        _LOGGER.error(f"Bit parsing error: {hex_str} [{start_bit}:{length}]")
        raise

def _parse_filter(hex_str: str, start_bit: int, length: int) -> dict:
    return {
        'reset_flag': bool(_parse_bits(hex_str, start_bit-2, 1)),
        'hours': _parse_bits(hex_str, start_bit, length)
    }

def generate_command(device_id: str, hass, **kwargs) -> str:
    try:
        state = {}
        for entry_id, data in hass.data.get(DOMAIN, {}).items():
            if data.get("state") and data.get("command_topic"):
                if device_id in entry_id or device_id in data.get("command_topic", ""):
                    state = data.get("state", {})
                    break
                    
        if "mode" in kwargs and "power" not in kwargs:
            kwargs["power"] = 1 if kwargs["mode"] == "on" else 0

        if "device_mode" in kwargs and kwargs["device_mode"] == "Normal":
            kwargs["ai_mode"] = 0
            kwargs["sleep_mode"] = 0
            _LOGGER.debug(f"[generate_command] Normal 모드 복귀 - AI Mode: 0, Sleep Mode: 0")


        if "pressure_mode" in kwargs:
            kwargs["pressure_mode"] = {
                "정압": 0,
                "양압": 1,
                "음압": 2
            }.get(kwargs["pressure_mode"], 0)

        if "fan_mode" in kwargs:
            kwargs["fan_in"], kwargs["fan_out"] = {
                "흡기Off-배기Off": (0, 0),
                "흡기Off-배기On":  (0, 1),
                "흡기On-배기Off":  (1, 0),
                "흡기On-배기On":   (1, 1)
            }.get(kwargs["fan_mode"], (0, 0))
            
        combined = {**state, **kwargs}
        device_mode = combined.get("device_mode") or combined.get("mode")
        topic_id = str(random.randint(100000, 200000))
        
        if device_mode in ["Sleep 1", "Sleep 2", "Sleep 3","AI Mode"]:
            payloads = {
                "Sleep 1": "A8A817228300C0000000000000000000000000000002CC",
                "Sleep 2": "A8A817228500C0000000000000000000000000000002CE",
                "Sleep 3": "A8A817228700C0000000000000000000000000000002D0",
                "AI Mode": "A8A817228900C0000000000000000000000000000002D2"
            }
            contents = payloads[device_mode]
            _LOGGER.debug(f"[generate_command] 고정된 Device Mode({device_mode}) CMD: {contents}")
            return json.dumps({
                "topic_id": topic_id,
                "type": "CMD",
                "contents": contents
            })

        # B5 (전원, 팬속도, AI 모드 구성)
        bin_power = int(combined.get("power", 0)) << 7
        bin_fan_speed = int(combined.get("fan_speed", 0)) << 4
        bin_ai_mode = int(combined.get("ai_mode", 0)) << 3
        bin_sleep_mode = int(combined.get("sleep_mode", 0)) << 1
        b5 = bin_power | bin_fan_speed | bin_ai_mode | bin_sleep_mode | 1

        # B6 (압력 모드)
        b6 = int(combined.get("pressure_mode", 0)) << 4

        # B7 (팬 모드)
        bin_fan_in = int(combined.get("fan_in", 0)) << 7
        bin_fan_out = int(combined.get("fan_out", 0)) << 6
        b7 = bin_fan_in | bin_fan_out
        
        # B15, B16(프리필터 : 2000시간 - 135 208, 3000시간 - 139 184, 40000시간 - 143 160)
        b15 = 0
        b16 = 0
        
        # B17, B18(헤파필터 : 2000시간 - 135 208, 3000시간 - 139 184, 40000시간 - 143 160)
        b17 = 0
        b18 = 0
        
        if "filter_reset" in kwargs:
            reset_type = kwargs["filter_reset"]
            
            if reset_type == "prefilter":
                b15 = 135
                b16 = 208
            elif reset_type == "hepafilter":
                b17 = 143
                b18 = 160
            else:
                _LOGGER.error(f"Invalid filter reset type: {reset_type}")
                return None
        
        # Checksum 계산
        checksum = 393 + b5 + b6 + b7 + b15 + b16 + b17 + b18

        # 전체 명령어 구성
        payload = (
            f"{b5:02X}{b6:02X}{b7:02X}"
            f"{'00' * 7}"
            f"{b15:02X}{b16:02X}{b17:02X}{b18:02X}"
            f"{'00' * 3}"
            f"{checksum:04X}"
        )
        contents = f"A8A81722{payload}"
            
        if len(contents) != 46:
            _LOGGER.warning(f"[generate_command] CMD 길이 불일치: {len(contents)}자 (예상: 46자)")

        command = {
            "topic_id": topic_id,
            "type": "CMD",
            "contents": contents
        }

        _LOGGER.debug(f"[generate_command] 최종 CMD ▶ {contents}")
        return json.dumps(command)

    except Exception as e:
        _LOGGER.error(f"[generate_command] 생성 실패: {e}", exc_info=True)
        raise
