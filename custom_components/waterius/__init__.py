from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
import homeassistant.helpers.config_validation as cv

from .api import WateriusApi, WateriusApiError
from .const import (
    CHANNEL_SEND_URL_TEMPLATE,
    CONF_METER_MAPPINGS,
    CONF_SCAN_INTERVAL,
    CONF_SYNC_INTERVAL,
    CONF_TOKEN,
    CONF_UC_SEND_INTERVAL,
    CONF_UC_SOURCE_ENTITY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SYNC_INTERVAL,
    DEFAULT_SEND_INTERVAL,
    DOMAIN,
    SERVICE_SEND_ALL,
    SERVICE_SEND_ALL_TO_WATERIUS,
    SERVICE_SEND_CONFIGURED_READING,
    SERVICE_SEND_READING,
    UC_SEND_URL,
)
from .coordinator import WateriusCoordinator
from .helpers import normalize_ha_meter_value

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


def _entry_value(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    return entry.options.get(key, entry.data.get(key, default))


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


def _get_token_for_entry(entry: ConfigEntry) -> str:
    token = str(_entry_value(entry, CONF_TOKEN, "") or "").strip()
    if token:
        return token
    raise HomeAssistantError("Waterius API token is not configured")


def _get_numeric_state(hass: HomeAssistant, entity_id: str, data_type: int | None = None) -> float:
    state = hass.states.get(entity_id)
    if state is None:
        raise HomeAssistantError(f"Source entity not found: {entity_id}")
    if state.state in ("", "unknown", "unavailable"):
        raise HomeAssistantError(f"Source entity has invalid state: {entity_id}={state.state}")
    try:
        if data_type is None:
            return float(state.state)
        value, _unit, _converted = normalize_ha_meter_value(state, int(data_type))
        return value
    except (TypeError, ValueError) as err:
        raise HomeAssistantError(f"Invalid cumulative meter sensor {entity_id}: {err}") from err


def _configured_mappings(entry: ConfigEntry) -> list[dict[str, Any]]:
    raw = _entry_value(entry, CONF_METER_MAPPINGS, []) or []
    return [x for x in raw if isinstance(x, dict) and x.get("entity_id")]


def _is_sending_configured(entry: ConfigEntry) -> bool:
    if _configured_mappings(entry):
        return True
    # Backward compatibility with v1.x entries.
    return bool(str(_entry_value(entry, CONF_UC_SOURCE_ENTITY, "") or "").strip())


def _entity_unique_id(hass: HomeAssistant, entity_id: str) -> str | None:
    registry = er.async_get(hass)
    entity_entry = registry.async_get(entity_id)
    return entity_entry.unique_id if entity_entry else None


def _find_legacy_channel_by_entity(hass: HomeAssistant, entry: ConfigEntry, coordinator, entity_id: str):
    """Resolve the old uc_source_entity setting to an existing Waterius channel."""
    unique_id = _entity_unique_id(hass, entity_id)
    if unique_id:
        marker = f"{entry.entry_id}_source_"
        if unique_id.startswith(marker) and "_channel_" in unique_id:
            try:
                rest = unique_id[len(marker):]
                source_part, channel_part = rest.split("_channel_", 1)
                source_id = int(source_part)
                channel_id = int(channel_part.split("_", 1)[0])
                for channel in coordinator.data.channels_by_source.get(source_id, []):
                    if channel.channel_id == channel_id:
                        return channel
            except (TypeError, ValueError):
                pass

    # Old config often pointed at an arbitrary HA sensor. Preserve the old
    # cold/hot heuristic only as a migration fallback, not for new entries.
    state = hass.states.get(entity_id)
    text = " ".join(
        [
            entity_id,
            state.name if state else "",
            str((state.attributes or {}).get("friendly_name", "")) if state else "",
        ]
    ).lower()
    requested_type = None
    if any(x in text for x in ("cold", "хвс", "холод")):
        requested_type = 0
    elif any(x in text for x in ("hot", "гвс", "горяч")):
        requested_type = 1

    candidates = []
    for channels in (coordinator.data.channels_by_source or {}).values():
        for channel in channels:
            if requested_type is None or channel.raw.get("data_type") == requested_type:
                candidates.append(channel)
    if len(candidates) == 1:
        return candidates[0]
    raise HomeAssistantError(
        "Legacy Waterius source sensor cannot be mapped unambiguously. "
        "Open integration options and map Home Assistant sensors to Waterius channels."
    )


async def async_send_all_configured_readings(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Send current Home Assistant totals according to the configured mappings."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    api: WateriusApi | None = runtime.get("api")
    if coordinator is None or api is None:
        raise HomeAssistantError("No active Waterius coordinator found")

    mappings = _configured_mappings(entry)
    if not mappings:
        # v1.x migration fallback: one configured HA sensor -> one existing channel.
        legacy_entity = str(_entry_value(entry, CONF_UC_SOURCE_ENTITY, "") or "").strip()
        if not legacy_entity:
            raise HomeAssistantError("No Home Assistant source sensors are mapped to Waterius")
        channel = _find_legacy_channel_by_entity(hass, entry, coordinator, legacy_entity)
        value = _get_numeric_state(hass, legacy_entity)
        url = CHANNEL_SEND_URL_TEMPLATE.format(channel_id=channel.channel_id)
        await api.send_reading(url, value)
        await coordinator.async_request_refresh()
        return 1

    sent = 0

    # Existing Waterius channels can be updated directly through the account API.
    for mapping in mappings:
        if mapping.get("transport") != "channel_api":
            continue
        channel_id = mapping.get("channel_id")
        if not channel_id:
            raise HomeAssistantError(f"Waterius mapping has no channel_id: {mapping}")
        value = _get_numeric_state(hass, str(mapping["entity_id"]), mapping.get("data_type"))
        url = CHANNEL_SEND_URL_TEMPLATE.format(channel_id=int(channel_id))
        try:
            await api.send_reading(url, value)
        except WateriusApiError as err:
            raise HomeAssistantError(f"Failed to send Waterius channel {channel_id}: {err}") from err
        sent += 1

    # Newly-created Universal sources keep using their dedicated key. Group all
    # tariff channels of the same logical meter into one UC request.
    universal_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mapping in mappings:
        if mapping.get("transport") == "universal":
            universal_groups[str(mapping.get("group_id") or mapping.get("uc_key") or "default")].append(mapping)

    for group_mappings in universal_groups.values():
        group_mappings.sort(key=lambda item: int(item.get("uc_channel", 0)))
        key = str(group_mappings[0].get("uc_key") or "").strip()
        if not key:
            raise HomeAssistantError("Universal Waterius mapping has no device key")
        first_mapping = group_mappings[0]
        group_name = str(first_mapping.get("group_name") or "").strip()
        if not group_name:
            first_type = int(first_mapping.get("data_type", 10))
            if first_type in (0, 1):
                group_name = "Вода"
            elif first_type in (2, 5, 6, 7, 8):
                group_name = "Электроэнергия"
            elif first_type == 3:
                group_name = "Газ"
            elif first_type == 4:
                group_name = "Отопление"
            elif first_type == 9:
                group_name = "Питьевая вода"
            else:
                group_name = "Home Assistant"
        payload: dict[str, Any] = {"key": key, "name": group_name}
        for mapping in group_mappings:
            index = int(mapping.get("uc_channel", 0))
            value = _get_numeric_state(hass, str(mapping["entity_id"]), int(mapping["data_type"]))
            payload[f"ch{index}"] = value
            payload[f"data_type{index}"] = int(mapping["data_type"])
            serial = str(mapping.get("serial") or "").strip()
            if serial:
                payload[f"serial{index}"] = serial
            sent += 1
        try:
            await api.send_universal_payload(UC_SEND_URL, payload)
        except WateriusApiError as err:
            raise HomeAssistantError(f"Failed to send Universal Waterius readings: {err}") from err

    if sent == 0:
        raise HomeAssistantError("No mapped Waterius readings found")

    await coordinator.async_request_refresh()
    return sent


# Compatibility names used by old services/buttons.
async def async_send_configured_uc_reading(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    sent = await async_send_all_configured_readings(hass, entry)
    return {"sent": sent}


async def async_send_all_readings_to_waterius(hass: HomeAssistant, entry: ConfigEntry) -> int:
    return await async_send_all_configured_readings(hass, entry)


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
        await async_send_all_configured_readings(hass, entry)

    if not hass.services.has_service(DOMAIN, SERVICE_SEND_READING):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_READING,
            handle_send_reading,
            schema=vol.Schema({vol.Required("channel_id"): cv.positive_int, vol.Required("value"): vol.Coerce(float)}),
        )

    common_schema = vol.Schema({vol.Optional("entry_id"): cv.string})
    for service_name in (SERVICE_SEND_ALL, SERVICE_SEND_ALL_TO_WATERIUS, SERVICE_SEND_CONFIGURED_READING):
        if not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(DOMAIN, service_name, handle_send_all, schema=common_schema)

    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    token = _get_token_for_entry(entry)
    api = WateriusApi(session, token)

    # One timer owns both directions of synchronization. The coordinator itself does not
    # schedule a second poll; this avoids duplicate GETs immediately after a successful send.
    sync_interval_min = int(
        _entry_value(entry, CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL) or DEFAULT_SYNC_INTERVAL
    )
    coordinator = WateriusCoordinator(hass, entry, api, update_interval=None)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator, "api": api}
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    async def _scheduled_sync_task() -> None:
        if _is_sending_configured(entry):
            try:
                # A successful send already refreshes the coordinator at the end.
                await async_send_all_configured_readings(hass, entry)
                return
            except Exception as err:
                _LOGGER.error("Scheduled Waterius send failed: %s", err)
        # No mapped readings, or the send failed: still refresh data from Waterius.
        try:
            await coordinator.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Scheduled Waterius refresh failed: %s", err)

    @callback
    def _scheduled_sync(now) -> None:
        hass.async_create_task(_scheduled_sync_task())

    unsub = async_track_time_interval(
        hass, _scheduled_sync, timedelta(minutes=max(1, sync_interval_min))
    )
    hass.data[DOMAIN][entry.entry_id]["sync_unsub"] = unsub
    entry.async_on_unload(unsub)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if isinstance(data, dict):
            unsub = data.get("sync_unsub") or data.get("send_unsub") or data.get("uc_unsub")
            if unsub:
                unsub()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entries to the unified synchronization interval."""
    if entry.version < 3:
        data = dict(entry.data)
        options = dict(entry.options)

        # Resolve old effective values before removing them. The former untouched defaults
        # (15 min read / 30 min send) become the new 20-minute default. Custom send cadence
        # wins over custom read cadence because sending is the more consequential operation.
        scan_interval = int(
            options.get(CONF_SCAN_INTERVAL, data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        )
        send_interval = int(
            options.get(CONF_UC_SEND_INTERVAL, data.get(CONF_UC_SEND_INTERVAL, DEFAULT_SEND_INTERVAL))
        )
        if scan_interval == DEFAULT_SCAN_INTERVAL and send_interval == DEFAULT_SEND_INTERVAL:
            sync_interval = DEFAULT_SYNC_INTERVAL
        elif send_interval != DEFAULT_SEND_INTERVAL:
            sync_interval = send_interval
        elif scan_interval != DEFAULT_SCAN_INTERVAL:
            sync_interval = scan_interval
        else:
            sync_interval = DEFAULT_SYNC_INTERVAL

        data[CONF_SYNC_INTERVAL] = sync_interval
        data.pop(CONF_SCAN_INTERVAL, None)
        data.pop(CONF_UC_SEND_INTERVAL, None)
        options.pop(CONF_SCAN_INTERVAL, None)
        options.pop(CONF_UC_SEND_INTERVAL, None)

        hass.config_entries.async_update_entry(entry, data=data, options=options, version=3)
    return True
