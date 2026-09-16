DOMAIN = "purethink"

# 프로토콜 상수
CMD_HEADER = bytes.fromhex("A8 A8")
CHECKSUM_BASE = 0x393

# 옵션 리스트
PRESSURE_MODES = ["정압", "양압", "음압"]
FAN_SPEEDS = ["Off", "Min", "Low", "Medium", "High", "Max"]
SLEEP_MODES = ["Off", "1", "2", "3"]

# 로깅 포맷
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

CONF_MQTT_MODE = "mqtt_mode"
CONF_MQTT_HOST = "mqtt_host"
CONF_MQTT_PORT = "mqtt_port"
CONF_MQTT_USERNAME = "mqtt_username"
CONF_MQTT_PASSWORD = "mqtt_password"

MQTT_MODE_MANUFACTURER = "manufacturer"
MQTT_MODE_LOCAL = "local"
MQTT_MANUFACTURER_BROKER = "dapt.iptime.org"
MQTT_MANUFACTURER_PORT = 8885
MQTT_LOCAL_DEFAULT_PORT = 1883

MQTT_MODE_BRIDGE = "bridge"
CONF_BRIDGE_PORT = "bridge_port"
CONF_BRIDGE_CLOUD = "bridge_cloud"
