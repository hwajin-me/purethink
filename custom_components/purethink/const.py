DOMAIN = "purethink"

# 프로토콜 상수
CMD_HEADER = bytes.fromhex("A8 A8")
CHECKSUM_BASE = 0x393

# 옵션 리스트
PRESSURE_MODES = ["정압", "양압", "음압"]
FAN_SPEEDS = ["Off", "1", "2", "3", "4", "5"]
SLEEP_MODES = ["Off", "1", "2", "3"]

# 로깅 포맷
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
