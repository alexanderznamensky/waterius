from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .api import WateriusApi
from .const import (
    DOMAIN,
    CONF_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_UC_SOURCE_ENTITY,
    CONF_UC_SEND_INTERVAL,
    SERVICE_SEND_READING,
    SERVICE_SEND_ALL,
    SERVICE_SEND_CONFIGURED_READING,
    SERVICE_SEND_ALL_TO_WATERIUS,
    CHANNEL_SEND_URL_TEMPLATE,
    UC_SEND_URL,
)
from .coordinator import WateriusCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


def _entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    return entry.options.get(key, entry.data.get(key, default))


def _uc_is_configured(entry: ConfigEntry) -> bool:
    return bool(str(_entry_value(entry, CONF_UC_SOURCE_ENTITY, "") or "").strip())


def _get_entry(hass: HomeAssistant, entry_id: str | None = None) -> ConfigEntry:
    entries = hass.config_entries.async_entries(DOMAIN)
    if entry_id:
        for entry in entries:
            if entry.entry_id == entry_id:
                return entry
        raise HomeAssistantError(f"Waterius config entry not found: {entry_id}")
    if not entries:
        raise HomeAssistantError("No Waterius config entries found")
    return entries[0]


async def _get_token_for_entry(hass: HomeAssistant, entry: ConfigEntry) -> str:
    token = str(_entry_value(entry, CONF_TOKEN, "") or "").strip()
    if token:
        return token
    raise HomeAssistantError(
        "Waterius token is not configured. Open https://account.waterius.ru/api/user/token/ "
        "after logging in to Waterius and paste the token in integration options."
    )



def _get_raw_value(raw: dict[str, Any] | None, *names: str) -> Any:
    if not isinstance(raw, dict):
        return None
    lower_map = {str(k).lower(): v for k, v in raw.items()}
    for name in names:
        if name in raw and raw.get(name) not in (None, ""):
            return raw.get(name)
        low = name.lower()
        if low in lower_map and lower_map.get(low) not in (None, ""):
            return lower_map.get(low)
    return None


def _extract_email_from_raw(*raw_items: dict[str, Any] | None) -> str:
    for raw in raw_items:
        value = _get_raw_value(raw, "email", "user_email", "user", "account_email", "owner_email")
        if isinstance(value, str) and "@" in value:
            return value.strip()
        # Some Waterius exports store user contact here.
        value = _get_raw_value(raw, "user_contact", "contact")
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return ""


def _extract_uc_key_from_raw(token: str, *raw_items: dict[str, Any] | None) -> str:
    for raw in raw_items:
        value = _get_raw_value(raw, "key", "uc_key", "send_key", "api_key", "token")
        if isinstance(value, str) and value.strip():
            return value.strip()
    # In the current Waterius API the token returned by /dj-rest-auth/login/ is the working API key.
    # If a separate UC key is not exposed in API metadata, use the configured API token as a fallback.
    return token


def _entity_unique_id(hass: HomeAssistant, entity_id: str) -> str | None:
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    if entity_entry:
        return entity_entry.unique_id
    return None


def _find_channel_by_entity(hass: HomeAssistant, entry: ConfigEntry, coordinator, source_entity: str):
    unique_id = _entity_unique_id(hass, source_entity)
    if not unique_id:
        return None, None
    marker = f"{entry.entry_id}_source_"
    if not unique_id.startswith(marker) or "_channel_" not in unique_id:
        return None, None
    try:
        rest = unique_id[len(marker):]
        source_part, channel_part = rest.split("_channel_", 1)
        source_id = int(source_part)
        channel_id = int(channel_part.split("_", 1)[0])
    except Exception:
        return None, None
    for channel in coordinator.data.channels_by_source.get(source_id, []):
        if channel.channel_id == channel_id:
            return source_id, channel
    return None, None


def _guess_requested_data_type(hass: HomeAssistant, source_entity: str) -> int | None:
    text_parts = [source_entity]
    state = hass.states.get(source_entity)
    if state is not None:
        text_parts.extend([state.name or ""])
        attrs = state.attributes or {}
        text_parts.extend(str(v) for k, v in attrs.items() if k in ("friendly_name", "device_class", "unit_of_measurement"))
    text = " ".join(text_parts).lower()
    if any(x in text for x in ("cold", "хвс", "холод")):
        return 0
    if any(x in text for x in ("hot", "гвс", "горяч")):
        return 1
    return None


def _find_channel_for_source_sensor(hass: HomeAssistant, entry: ConfigEntry, coordinator, source_entity: str):
    source_id, channel = _find_channel_by_entity(hass, entry, coordinator, source_entity)
    if channel is not None:
        return source_id, channel

    all_channels = []
    for sid, channels in (coordinator.data.channels_by_source or {}).items():
        for channel in channels:
            all_channels.append((sid, channel))

    requested_data_type = _guess_requested_data_type(hass, source_entity)
    candidates = []
    if requested_data_type is not None:
        candidates = [(sid, ch) for sid, ch in all_channels if ch.raw.get("data_type") == requested_data_type]
        if len(candidates) == 1:
            return candidates[0]

    water_candidates = [(sid, ch) for sid, ch in all_channels if ch.raw.get("data_type") in (0, 1)]
    if len(water_candidates) == 1:
        return water_candidates[0]

    if requested_data_type is not None and candidates:
        raise HomeAssistantError(
            "Found several Waterius API channels for the selected sensor data type. "
            "Select a Waterius channel sensor as source, or leave only one matching channel."
        )

    raise HomeAssistantError(
        "Could not determine serial/data_type from Waterius API for the selected source sensor. "
        "Best option: select the corresponding Waterius channel sensor as source."
    )


def _find_related_export_raw(coordinator, source_id: int, channel_raw: dict[str, Any]) -> dict[str, Any] | None:
    export_id = channel_raw.get("export")
    try:
        export_id = int(export_id) if export_id is not None else None
    except Exception:
        export_id = None
    if export_id is None:
        exports = (coordinator.data.exports_by_source or {}).get(source_id, {})
        if len(exports) == 1:
            return next(iter(exports.values())).raw
        return None
    export = (coordinator.data.exports_by_source or {}).get(source_id, {}).get(export_id)
    return export.raw if export else None


async def async_send_configured_uc_reading(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Send selected HA sensor value to uc.waterius.ru.

    The value comes from the configured Home Assistant sensor.
    serial0 and data_type0 are resolved from Waterius API channel metadata.
    key/email are resolved from Waterius API metadata when available; key falls back to the API token.
    """
    source_entity = str(_entry_value(entry, CONF_UC_SOURCE_ENTITY, "") or "").strip()

    if not source_entity:
        raise HomeAssistantError("Waterius UC sending is not configured: source sensor is empty")

    state = hass.states.get(source_entity)
    if state is None:
        raise HomeAssistantError(f"Source entity not found: {source_entity}")
    if state.state in ("unknown", "unavailable", ""):
        raise HomeAssistantError(f"Source entity has invalid state: {source_entity}={state.state}")

    try:
        value = float(state.state)
    except (TypeError, ValueError) as err:
        raise HomeAssistantError(f"Source entity state is not numeric: {source_entity}={state.state}") from err

    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    if coordinator is None:
        raise HomeAssistantError("No active Waterius coordinator found")

    source_id, channel = _find_channel_for_source_sensor(hass, entry, coordinator, source_entity)
    channel_raw = channel.raw or {}
    export_raw = _find_related_export_raw(coordinator, source_id, channel_raw) or {}
    source_raw = (coordinator.data.sources or {}).get(source_id, {})

    serial = str(_get_raw_value(channel_raw, "serial", "serial_number", "meter_serial") or "").strip()
    data_type = channel_raw.get("data_type")

    if not serial:
        raise HomeAssistantError("Waterius API channel does not contain serial for selected source sensor")
    if data_type is None:
        raise HomeAssistantError("Waterius API channel does not contain data_type for selected source sensor")

    token = await _get_token_for_entry(hass, entry)
    key = _extract_uc_key_from_raw(token, channel_raw, source_raw, export_raw)
    email = _extract_email_from_raw(channel_raw, source_raw, export_raw)

    payload = {
        "ch0": f"{value:.2f}",
        "data_type0": int(data_type),
        "serial0": serial,
        "key": key,
        "email": email,
    }

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            UC_SEND_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            text = await resp.text()
            if resp.status < 200 or resp.status >= 300:
                raise HomeAssistantError(f"Waterius UC send failed: HTTP {resp.status}. Body: {text[:500]}")
            return {"status": resp.status, "body": text[:500], "payload": payload}
    except TimeoutError as err:
        raise HomeAssistantError("Timeout sending reading to Waterius UC") from err
    except aiohttp.ClientError as err:
        raise HomeAssistantError(f"Network error sending reading to Waterius UC: {err}") from err


async def async_send_all_readings_to_waterius(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Send current last_value for all Waterius API channels to account.waterius.ru."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    if coordinator is None:
        raise HomeAssistantError("No active Waterius coordinator found")

    sent = 0
    for channels in (coordinator.data.channels_by_source or {}).values():
        for channel in channels:
            url = CHANNEL_SEND_URL_TEMPLATE.format(channel_id=channel.channel_id)
            await coordinator.api.send_reading(url, channel.last_value)
            sent += 1

    if sent == 0:
        raise HomeAssistantError("No Waterius channels found to send")

    await coordinator.async_request_refresh()
    return sent

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    async def handle_send_reading(call: ServiceCall) -> None:
        channel_id = int(call.data["channel_id"])
        value = call.data["value"]
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict) or "coordinator" not in data:
                continue
            coordinator = data["coordinator"]
            url = CHANNEL_SEND_URL_TEMPLATE.format(channel_id=channel_id)
            await coordinator.api.send_reading(url, value)
            await coordinator.async_request_refresh()
            return
        raise HomeAssistantError("No active Waterius coordinator found")

    async def handle_send_all(call: ServiceCall) -> None:
        entry = _get_entry(hass, call.data.get("entry_id"))
        await async_send_all_readings_to_waterius(hass, entry)

    async def handle_send_configured_reading(call: ServiceCall) -> None:
        entry = _get_entry(hass, call.data.get("entry_id"))
        await async_send_configured_uc_reading(hass, entry)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_READING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_READING,
            handle_send_reading,
            schema=vol.Schema({vol.Required("channel_id"): cv.positive_int, vol.Required("value"): vol.Coerce(float)}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_ALL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_ALL,
            handle_send_all,
            schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_ALL_TO_WATERIUS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_ALL_TO_WATERIUS,
            handle_send_all,
            schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_CONFIGURED_READING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_CONFIGURED_READING,
            handle_send_configured_reading,
            schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
        )

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    token = await _get_token_for_entry(hass, entry)
    api = WateriusApi(session, token)

    interval_min = int(_entry_value(entry, CONF_SCAN_INTERVAL, 15) or 15)
    coordinator = WateriusCoordinator(hass, entry, api, update_interval=timedelta(minutes=max(1, interval_min)))
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator, "api": api}

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    if _uc_is_configured(entry):
        send_interval_min = int(_entry_value(entry, CONF_UC_SEND_INTERVAL, 1440) or 1440)

        @callback
        def _scheduled_send(now) -> None:
            hass.async_create_task(async_send_configured_uc_reading(hass, entry))

        unsub = async_track_time_interval(hass, _scheduled_send, timedelta(minutes=max(1, send_interval_min)))
        hass.data[DOMAIN][entry.entry_id]["uc_unsub"] = unsub
        entry.async_on_unload(unsub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(data, dict):
            unsub = data.get("uc_unsub")
            if unsub:
                unsub()
    return unload_ok
