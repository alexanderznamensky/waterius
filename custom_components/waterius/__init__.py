from __future__ import annotations

from collections import defaultdict
import asyncio
import re
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


def _get_safe_routine_value(
    hass: HomeAssistant,
    entity_id: str,
    data_type: int | None = None,
    *,
    current_waterius_value: float | None = None,
) -> float | None:
    """Return a safe cumulative value for routine synchronization.

    Unchanged values are intentionally NOT filtered out: every synchronization must
    transmit the current total so Waterius can refresh its last-received timestamp.

    Missing/unavailable/non-numeric values and negative totals are skipped. Any
    positive HA value is sent even when it is lower than the current Waterius value:
    a meter can legitimately be replaced/reset and HA remains the source of truth.

    Zero is the only protected value. It is sent only when Waterius already contains
    zero for that channel; otherwise it is skipped so a default/failed HA sensor cannot
    accidentally overwrite a real non-zero meter total.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in ("", "unknown", "unavailable"):
        _LOGGER.warning("Waterius: skip %s because its state is unavailable", entity_id)
        return None
    try:
        if data_type is None:
            value = float(state.state)
        else:
            value, _unit, _converted = normalize_ha_meter_value(state, int(data_type))
    except (TypeError, ValueError) as err:
        _LOGGER.warning("Waterius: skip %s because it is not a valid cumulative value: %s", entity_id, err)
        return None

    if value < 0:
        _LOGGER.warning("Waterius: skip %s because cumulative value is negative: %s", entity_id, value)
        return None

    if value == 0:
        try:
            current = float(current_waterius_value) if current_waterius_value is not None else None
        except (TypeError, ValueError):
            current = None
        if current != 0:
            _LOGGER.warning(
                "Waterius: skip %s because HA value is 0 while current Waterius value is %s",
                entity_id, current_waterius_value,
            )
            return None

    return value


def _current_waterius_value(coordinator, mapping: dict[str, Any]) -> float | None:
    """Resolve current Waterius total for a configured mapping, if available."""
    channel_id = mapping.get("channel_id")
    source_id = mapping.get("source_id")
    pools = []
    if source_id is not None:
        try:
            pools.append(coordinator.data.channels_by_source.get(int(source_id), []))
        except (TypeError, ValueError):
            pass
    if not pools:
        pools = list((coordinator.data.channels_by_source or {}).values())

    if channel_id is not None:
        try:
            wanted = int(channel_id)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for channels in pools:
                for channel in channels:
                    if channel.channel_id == wanted:
                        try:
                            return float(channel.last_value)
                        except (TypeError, ValueError):
                            return None

    # Fallback for migrated/bootstrap mappings where channel_id may be temporarily absent.
    data_type = mapping.get("data_type")
    serial = str(mapping.get("serial") or "").strip()
    for channels in pools:
        for channel in channels:
            raw = channel.raw or {}
            if raw.get("data_type") != data_type:
                continue
            ch_serial = str(raw.get("serial") or "").strip()
            if serial and ch_serial and serial != ch_serial:
                continue
            try:
                return float(channel.last_value)
            except (TypeError, ValueError):
                return None
    return None


def _mapping_matches(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True when two stored mappings describe the same Waterius channel."""
    a_channel = a.get("channel_id")
    b_channel = b.get("channel_id")
    if a_channel is not None and b_channel is not None:
        try:
            if int(a_channel) == int(b_channel):
                return True
        except (TypeError, ValueError):
            pass

    a_source = a.get("source_id")
    b_source = b.get("source_id")
    same_source = a_source is None or b_source is None or str(a_source) == str(b_source)
    same_type = a.get("data_type") == b.get("data_type")
    a_serial = str(a.get("serial") or "").strip()
    b_serial = str(b.get("serial") or "").strip()
    same_serial = not a_serial or not b_serial or a_serial == b_serial
    return same_source and same_type and same_serial


def _configured_mappings(entry: ConfigEntry) -> list[dict[str, Any]]:
    """Return effective mappings and recover Universal metadata if Options lost it.

    Versions up to 1.1.15 rebuilt mappings in Options as ``channel_api`` even for
    devices originally bootstrapped through uc.waterius.ru. ConfigEntry.data still
    contains the original Universal mappings, including their key. Merge that transport
    metadata back while keeping the currently selected HA entity_id from Options.
    """
    raw = _entry_value(entry, CONF_METER_MAPPINGS, []) or []
    mappings = [dict(x) for x in raw if isinstance(x, dict) and x.get("entity_id")]

    original_raw = entry.data.get(CONF_METER_MAPPINGS, []) or []
    original_universal = [
        x for x in original_raw
        if isinstance(x, dict) and x.get("transport") == "universal" and x.get("uc_key")
    ]

    for mapping in mappings:
        if mapping.get("transport") == "universal" and mapping.get("uc_key"):
            continue
        original = next((x for x in original_universal if _mapping_matches(mapping, x)), None)
        if original is None:
            continue
        for key in ("transport", "group_id", "group_name", "uc_key", "uc_channel"):
            if key in original:
                mapping[key] = original[key]
        _LOGGER.debug(
            "Waterius: recovered Universal transport for channel=%s source=%s entity=%s",
            mapping.get("channel_id"), mapping.get("source_id"), mapping.get("entity_id"),
        )

    return mappings


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


def _resolve_mapping_source_id(coordinator, mapping: dict[str, Any]) -> int | None:
    """Resolve the Waterius source that owns a mapping."""
    raw_source_id = mapping.get("source_id")
    if raw_source_id is not None:
        try:
            source_id = int(raw_source_id)
            if source_id in (coordinator.data.sources or {}) or source_id in (coordinator.data.channels_by_source or {}):
                return source_id
        except (TypeError, ValueError):
            pass

    channel_id = mapping.get("channel_id")
    if channel_id is not None:
        try:
            wanted_channel = int(channel_id)
        except (TypeError, ValueError):
            wanted_channel = None
        if wanted_channel is not None:
            for source_id, channels in (coordinator.data.channels_by_source or {}).items():
                if any(channel.channel_id == wanted_channel for channel in channels):
                    return int(source_id)

    # Last-resort match for old/bootstrap mappings that have no channel_id yet.
    wanted_type = mapping.get("data_type")
    wanted_serial = str(mapping.get("serial") or "").strip()
    candidates: list[int] = []
    for source_id, channels in (coordinator.data.channels_by_source or {}).items():
        for channel in channels:
            raw = channel.raw or {}
            if raw.get("data_type") != wanted_type:
                continue
            serial = str(raw.get("serial") or "").strip()
            if wanted_serial and serial and wanted_serial != serial:
                continue
            candidates.append(int(source_id))
            break
    return candidates[0] if len(candidates) == 1 else None


def _looks_like_universal_key(value: str) -> bool:
    value = str(value or "").strip()
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{16,256}", value))


def _extract_source_universal_key(
    source: dict[str, Any] | None,
    mappings: list[dict[str, Any]],
) -> str:
    """Return the full uc.waterius.ru device key without depending on display name.

    New entries store ``uc_key`` in their mapping. Older Waterius devices expose the
    full unique token inside ``device_info``; their ``name`` initially equals the key
    but may later be changed by the ``name`` field of a Universal payload, so it is
    only a fallback when it still looks like a long token.
    """
    for mapping in mappings:
        key = str(mapping.get("uc_key") or "").strip()
        if key:
            return key

    source = source if isinstance(source, dict) else {}
    for field in ("uc_key", "universal_key", "send_key", "api_key"):
        key = str(source.get(field) or "").strip()
        if key:
            return key

    device_info = str(source.get("device_info") or "")
    patterns = (
        r"Уникальный\s+токен\s*:\s*([A-Za-z0-9._-]{16,256})",
        r"Unique\s+token\s*:\s*([A-Za-z0-9._-]{16,256})",
    )
    for pattern in patterns:
        match = re.search(pattern, device_info, re.IGNORECASE)
        if match:
            return match.group(1)

    # A newly-created Waterius Home Assistant device initially uses the full key as
    # its display name. Never use short source["key"] values (often only 4 digits).
    name = str(source.get("name") or "").strip()
    if _looks_like_universal_key(name):
        return name

    long_key = str(source.get("key") or "").strip()
    if _looks_like_universal_key(long_key):
        return long_key
    return ""


def _source_group_name(source: dict[str, Any] | None, channels: list[Any]) -> str:
    """Choose a stable, human-readable Waterius device name."""
    source = source if isinstance(source, dict) else {}
    existing_name = str(source.get("name") or "").strip()
    if existing_name and not _looks_like_universal_key(existing_name) and set(existing_name) != {"?"}:
        return existing_name

    data_types = {channel.raw.get("data_type") for channel in channels if channel and channel.raw}
    if data_types and data_types.issubset({0, 1}):
        return "Вода"
    if data_types and data_types.issubset({2, 5, 6, 7, 8}):
        return "Электроэнергия"
    if data_types == {3}:
        return "Газ"
    if data_types == {4}:
        return "Отопление"
    if data_types == {9}:
        return "Питьевая вода"
    return "Home Assistant"


def _channel_number(channel: Any, fallback: int) -> int:
    try:
        return int((channel.raw or {}).get("number", fallback))
    except (TypeError, ValueError):
        return fallback


def _current_channel_number_value(channel: Any) -> float | None:
    try:
        return float(channel.last_value)
    except (TypeError, ValueError):
        return None


def _mapping_for_channel(
    mappings: list[dict[str, Any]],
    source_id: int,
    channel: Any,
) -> dict[str, Any] | None:
    """Find the HA source mapping corresponding to a Waterius channel."""
    channel_id = channel.channel_id
    raw = channel.raw or {}
    data_type = raw.get("data_type")
    serial = str(raw.get("serial") or "").strip()
    for mapping in mappings:
        mapped_source = mapping.get("source_id")
        if mapped_source is not None:
            try:
                if int(mapped_source) != int(source_id):
                    continue
            except (TypeError, ValueError):
                continue

        mapped_channel = mapping.get("channel_id")
        if mapped_channel is not None:
            try:
                if int(mapped_channel) == int(channel_id):
                    return mapping
            except (TypeError, ValueError):
                pass

        if mapping.get("data_type") != data_type:
            continue
        mapped_serial = str(mapping.get("serial") or "").strip()
        if mapped_serial and serial and mapped_serial != serial:
            continue
        return mapping
    return None


async def async_send_all_configured_readings(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Send mapped HA readings to Waterius through Universal Cloud.

    ``/api/channel/<id>/reports/`` is read-only on the current Waterius API (POST
    returns 405), therefore routine synchronization must never use ``channel_api``.
    Existing and newly-created Waterius devices are both sent to ``uc.waterius.ru``.

    Each request contains the complete source device. Mapped channels take their
    current HA value; unmapped/temporarily-invalid channels keep their current
    Waterius value. This prevents a bad sensor from overwriting a valid total while
    still allowing the other channels and the device last-wakeup timestamp to update.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    api: WateriusApi | None = runtime.get("api")
    if coordinator is None or api is None:
        raise HomeAssistantError("No active Waterius coordinator found")

    mappings = _configured_mappings(entry)

    # Convert the old single-source option into an in-memory mapping so old config
    # entries also use the supported Universal endpoint.
    if not mappings:
        legacy_entity = str(_entry_value(entry, CONF_UC_SOURCE_ENTITY, "") or "").strip()
        if legacy_entity:
            channel = _find_legacy_channel_by_entity(hass, entry, coordinator, legacy_entity)
            raw = channel.raw or {}
            mappings = [{
                "source_id": raw.get("source"),
                "channel_id": channel.channel_id,
                "data_type": raw.get("data_type"),
                "serial": str(raw.get("serial") or ""),
                "entity_id": legacy_entity,
            }]
        else:
            await coordinator.async_request_refresh()
            return 0

    mappings_by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[str] = []
    for mapping in mappings:
        source_id = _resolve_mapping_source_id(coordinator, mapping)
        if source_id is None:
            unresolved.append(str(mapping.get("entity_id") or mapping.get("channel_id") or "?"))
            continue
        effective = dict(mapping)
        effective["source_id"] = source_id
        mappings_by_source[source_id].append(effective)

    if unresolved:
        _LOGGER.warning("Waterius: could not resolve source for mappings: %s", ", ".join(unresolved))

    sent_from_ha = 0
    posted_devices = 0

    for source_id, source_mappings in mappings_by_source.items():
        channels = list((coordinator.data.channels_by_source or {}).get(source_id, []))
        if not channels:
            _LOGGER.warning("Waterius: source %s has no channels; synchronization skipped", source_id)
            continue

        source = (coordinator.data.sources or {}).get(source_id)
        key = _extract_source_universal_key(source, source_mappings)
        if not key:
            _LOGGER.error(
                "Waterius: cannot determine Universal key for source %s. "
                "Reconfigure the mapping or recreate the integration entry.",
                source_id,
            )
            continue

        group_name = _source_group_name(source, channels)
        payload: dict[str, Any] = {"key": key, "name": group_name}
        payload_values: dict[str, float] = {}
        missing_values: list[str] = []
        ha_values_used = 0

        sorted_channels = sorted(
            channels,
            key=lambda ch: (_channel_number(ch, 999), ch.channel_id),
        )
        for fallback_index, channel in enumerate(sorted_channels):
            raw = channel.raw or {}
            index = _channel_number(channel, fallback_index)
            mapping = _mapping_for_channel(source_mappings, source_id, channel)
            current = _current_channel_number_value(channel)
            value: float | None = current

            if mapping is not None:
                candidate = _get_safe_routine_value(
                    hass,
                    str(mapping["entity_id"]),
                    raw.get("data_type"),
                    current_waterius_value=current,
                )
                if candidate is not None:
                    value = candidate
                    ha_values_used += 1
                elif current is not None:
                    _LOGGER.warning(
                        "Waterius: preserving current value %s for source=%s channel=%s "
                        "because HA entity %s is not safe to send",
                        current, source_id, channel.channel_id, mapping.get("entity_id"),
                    )

            if value is None:
                missing_values.append(str(channel.channel_id))
                continue

            payload[f"ch{index}"] = value
            payload[f"data_type{index}"] = int(raw.get("data_type", 10))
            serial = str(raw.get("serial") or "").strip()
            if serial:
                payload[f"serial{index}"] = serial
            payload_values[f"ch{index}"] = value

        if missing_values:
            _LOGGER.error(
                "Waterius: source %s (%s) has channels without any safe value: %s; device skipped",
                source_id, group_name, ", ".join(missing_values),
            )
            continue

        if posted_devices > 0:
            await asyncio.sleep(2)
        try:
            response = await api.send_universal_payload(UC_SEND_URL, payload)
        except WateriusApiError as err:
            _LOGGER.error(
                "Failed to send Universal Waterius source %s (%s): %s",
                source_id, group_name, err,
            )
            continue

        posted_devices += 1
        sent_from_ha += ha_values_used
        _LOGGER.info(
            "Waterius synchronization sent source=%s device=%s HA=%s/%s values=%s response=%s",
            source_id,
            group_name,
            ha_values_used,
            len(source_mappings),
            payload_values,
            response,
        )

    # Refresh the account API after all Universal POSTs so sensors and last_wakeup
    # reflect exactly what Waterius accepted.
    await coordinator.async_request_refresh()
    return sent_from_ha


async def async_synchronize_now(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Perform one complete manual synchronization cycle.

    If HA sources are mapped, push safe totals first and then refresh. If nothing is
    mapped, simply refresh Waterius. The button therefore never fails merely because
    mappings are absent.
    """
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = runtime.get("coordinator")
    if coordinator is None:
        raise HomeAssistantError("No active Waterius coordinator found")

    if not _is_sending_configured(entry):
        await coordinator.async_request_refresh()
        return 0
    return await async_send_all_configured_readings(hass, entry)


# Compatibility names used by old services/buttons.
async def async_send_configured_uc_reading(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    sent = await async_send_all_configured_readings(hass, entry)
    return {"sent": sent}


async def async_send_all_readings_to_waterius(hass: HomeAssistant, entry: ConfigEntry) -> int:
    return await async_send_all_configured_readings(hass, entry)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    async def handle_send_reading(call: ServiceCall) -> None:
        """Send one explicit channel value by posting the complete Universal device."""
        channel_id = int(call.data["channel_id"])
        requested_value = float(call.data["value"])
        for data in hass.data.get(DOMAIN, {}).values():
            if not isinstance(data, dict) or "coordinator" not in data or "api" not in data:
                continue
            coordinator = data["coordinator"]
            api: WateriusApi = data["api"]
            for source_id, channels in (coordinator.data.channels_by_source or {}).items():
                target = next((ch for ch in channels if ch.channel_id == channel_id), None)
                if target is None:
                    continue
                source = (coordinator.data.sources or {}).get(source_id)
                key = _extract_source_universal_key(source, [])
                if not key:
                    raise HomeAssistantError(f"Cannot determine Waterius Universal key for source {source_id}")
                payload: dict[str, Any] = {
                    "key": key,
                    "name": _source_group_name(source, list(channels)),
                }
                for fallback_index, channel in enumerate(sorted(channels, key=lambda ch: (_channel_number(ch, 999), ch.channel_id))):
                    raw = channel.raw or {}
                    index = _channel_number(channel, fallback_index)
                    value = requested_value if channel.channel_id == channel_id else _current_channel_number_value(channel)
                    if value is None:
                        raise HomeAssistantError(f"Waterius channel {channel.channel_id} has no current value")
                    payload[f"ch{index}"] = value
                    payload[f"data_type{index}"] = int(raw.get("data_type", 10))
                    serial = str(raw.get("serial") or "").strip()
                    if serial:
                        payload[f"serial{index}"] = serial
                await api.send_universal_payload(UC_SEND_URL, payload)
                await coordinator.async_request_refresh()
                return
        raise HomeAssistantError(f"Waterius channel not found: {channel_id}")

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
        try:
            await async_synchronize_now(hass, entry)
        except Exception as err:
            _LOGGER.error("Scheduled Waterius synchronization failed: %s", err)
            try:
                await coordinator.async_request_refresh()
            except Exception as refresh_err:
                _LOGGER.error("Scheduled Waterius refresh after sync failure failed: %s", refresh_err)

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
