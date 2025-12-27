DOMAIN = "waterius"

CONF_TOKEN = "token"
CONF_NAME = "name"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_NAME = "Waterius"

BASE_URL = "https://account.waterius.ru"
CHANNELS_URL = BASE_URL + "/api/channel/"
SOURCES_URL = BASE_URL + "/api/source/"
EXPORTS_URL = BASE_URL + "/api/export/"
EXPORT_DETAIL_URL_TEMPLATE = BASE_URL + "/api/export/{export_id}/"
CHANNEL_REPORTS_URL_TEMPLATE = BASE_URL + "/api/channel/{channel_id}/reports/"

HA_DEVICE_MANUFACTURER = "Waterius"
HA_DEVICE_MODEL = "account.waterius.ru"

DATA_TYPE_NAMES = {
    0: "ХВС",
    1: "ГВС",
    6: "Электроэнергия T2 (ночь)",
    7: "Электроэнергия T1 (пик)",
    8: "Электроэнергия T3 (полупик)",
}

DATA_TYPE_DEVICE_CLASS = {
    0: "water",
    1: "water",
    6: "energy",
    7: "energy",
    8: "energy",
}

DATA_TYPE_UNIT = {
    0: "m³",
    1: "m³",
    6: "kWh",
    7: "kWh",
    8: "kWh",
}

DATA_TYPE_STATE_CLASS = {
    0: "total_increasing",
    1: "total_increasing",
    6: "total_increasing",
    7: "total_increasing",
    8: "total_increasing",
}

DEVICE_CLASS_TITLES = {
    "water": "Счетчики воды",
    "energy": "Счетчик электроэнергии",
}


SERVICE_SEND_READING = "send_reading"
SERVICE_SEND_ALL = "send_all"
CHANNEL_SEND_URL_TEMPLATE = BASE_URL + "/api/channel/{channel_id}/reports/"
