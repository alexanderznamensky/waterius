DOMAIN = "waterius"

CONF_TOKEN = "token"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_NAME = "name"
# Unified Waterius synchronization interval.
CONF_SYNC_INTERVAL = "sync_interval"

# Legacy interval settings kept only for config-entry migration.
CONF_SCAN_INTERVAL = "scan_interval"
CONF_UC_SEND_INTERVAL = "uc_send_interval"

# Legacy single-sensor setting kept for backward compatibility.
CONF_UC_SOURCE_ENTITY = "uc_source_entity"

# New multi-channel mapping: a list of channel dictionaries.
CONF_METER_MAPPINGS = "meter_mappings"
CONF_METER_TYPES = "meter_types"
CONF_RECONFIGURE_MAPPINGS = "reconfigure_mappings"

DEFAULT_NAME = "Waterius"
DEFAULT_SYNC_INTERVAL = 20

# Legacy defaults used to recognize old untouched configuration during migration.
DEFAULT_SCAN_INTERVAL = 15
DEFAULT_SEND_INTERVAL = 30

BASE_URL = "https://account.waterius.ru"
TOKEN_URL = BASE_URL + "/api/user/token/"
CHANNELS_URL = BASE_URL + "/api/channel/"
SOURCES_URL = BASE_URL + "/api/source/"
HOME_ASSISTANT_ADD_URL = BASE_URL + "/devices/add/home-assistant"
EXPORTS_URL = BASE_URL + "/api/export/"
EXPORT_DETAIL_URL_TEMPLATE = BASE_URL + "/api/export/{export_id}/"
CHANNEL_REPORTS_URL_TEMPLATE = BASE_URL + "/api/channel/{channel_id}/reports/"
CHANNEL_SEND_URL_TEMPLATE = BASE_URL + "/api/channel/{channel_id}/reports/"

# Universal Cloud endpoint used only to bootstrap channels that do not exist yet.
UC_SEND_URL = "https://uc.waterius.ru"
# Legacy endpoint removed: current Waterius universal API is uc.waterius.ru only.

HA_DEVICE_MANUFACTURER = "Waterius"
HA_DEVICE_MODEL = "account.waterius.ru"

# Universal Cloud data_type values.
DATA_TYPE_NAMES = {
    0: "ХВС",
    1: "ГВС",
    2: "Электроэнергия",
    3: "Газ",
    4: "Отопление",
    5: "Электроэнергия (день)",
    6: "Электроэнергия T2 (ночь)",
    7: "Электроэнергия T1 (пик)",
    8: "Электроэнергия T3 (полупик)",
    9: "Питьевая вода",
    10: "Другой",
}

DATA_TYPE_DEVICE_CLASS = {
    0: "water",
    1: "water",
    2: "energy",
    3: "gas",
    5: "energy",
    6: "energy",
    7: "energy",
    8: "energy",
    9: "water",
}

DATA_TYPE_UNIT = {
    0: "m³",
    1: "m³",
    2: "kWh",
    3: "m³",
    5: "kWh",
    6: "kWh",
    7: "kWh",
    8: "kWh",
    9: "m³",
}

DATA_TYPE_STATE_CLASS = {key: "total_increasing" for key in DATA_TYPE_NAMES}

DEVICE_CLASS_TITLES = {
    "water": "Счетчики воды",
    "energy": "Счетчик электроэнергии",
    "gas": "Счетчик газа",
}

METER_TYPE_LABELS = {
    "cold_water": "Холодная вода",
    "hot_water": "Горячая вода",
    "electricity": "Электроэнергия",
    "gas": "Газ",
    "heat": "Отопление / тепло",
    "drinking_water": "Питьевая вода",
    "other": "Другой",
}

ELECTRICITY_TARIFF_LABELS = {
    "single": "Однотарифный",
    "dual": "Двухтарифный (день / ночь)",
    "triple": "Трёхтарифный (пик / ночь / полупик)",
}

ELECTRICITY_TARIFF_CHANNELS = {
    "single": [("total", 2, "Общее")],
    "dual": [("day", 5, "День"), ("night", 6, "Ночь")],
    "triple": [("peak", 7, "Пик / T1"), ("night", 6, "Ночь / T2"), ("half_peak", 8, "Полупик / T3")],
}

SERVICE_SEND_READING = "send_reading"
SERVICE_SEND_ALL = "send_all"
SERVICE_SEND_CONFIGURED_READING = "send_configured_reading"
SERVICE_SEND_ALL_TO_WATERIUS = "send_all_to_waterius"
