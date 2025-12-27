from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.util import dt as dt_util

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    DATA_TYPE_NAMES,
    DATA_TYPE_DEVICE_CLASS,
    DATA_TYPE_UNIT,
    DATA_TYPE_STATE_CLASS,
    DEVICE_CLASS_TITLES,
    HA_DEVICE_MANUFACTURER,
    HA_DEVICE_MODEL,
)
from .helpers import build_channel_attrs, normalize_tarif_ended, compute_days_left, parse_personal_account


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities: list[SensorEntity] = [WateriusSummarySensor(entry, coordinator)]

    for source_id, source_name in (coordinator.data.sources or {}).items():
        channels = coordinator.data.channels_by_source.get(source_id, [])

        group_dc: Optional[str] = None
        for ch in channels:
            dt = ch.raw.get("data_type")
            dc = DATA_TYPE_DEVICE_CLASS.get(dt)
            if dc:
                group_dc = dc
                break

        for ch in channels:
            entities.append(
                WateriusChannelSensor(
                    entry,
                    coordinator,
                    source_id=source_id,
                    source_name=source_name,
                    channel_id=ch.channel_id,
                    group_device_class=group_dc,
                )
            )

        exports = (coordinator.data.exports_by_source or {}).get(source_id, {})
        for export_id in sorted(exports.keys()):
            entities.append(
                WateriusExportDiagnosticSensor(
                    entry,
                    coordinator,
                    source_id=source_id,
                    source_name=source_name,
                    export_id=export_id,
                    group_device_class=group_dc,
                )
            )

    async_add_entities(entities, update_before_add=False)


class _BaseWateriusEntity(SensorEntity):
    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        self._entry = entry
        self._coordinator = coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))


class WateriusSummarySensor(_BaseWateriusEntity):
    _attr_has_entity_name = True
    _attr_name = "Summary"
    _attr_icon = "mdi:counter"

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_summary"

    @property
    def native_value(self) -> int:
        return len(self._coordinator.data.sources or {})

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        channels_total = sum(len(v) for v in (self._coordinator.data.channels_by_source or {}).values())
        exports_total = sum(len(v) for v in (self._coordinator.data.exports_by_source or {}).values())
        return {
            "sources_count": len(self._coordinator.data.sources or {}),
            "channels_count": channels_total,
            "exports_count": exports_total,
        }


class WateriusChannelSensor(_BaseWateriusEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:counter"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator,
        *,
        source_id: int,
        source_name: str,
        channel_id: int,
        group_device_class: Optional[str],
    ) -> None:
        super().__init__(entry, coordinator)
        self._source_id = source_id
        self._source_name = (source_name or "").strip() or f"Source {source_id}"
        self._channel_id = channel_id
        self._group_device_class = group_device_class

        self._attr_unique_id = f"{entry.entry_id}_source_{source_id}_channel_{channel_id}"

        ch = self._find_channel()
        dt = (ch.raw.get("data_type") if ch else None)
        serial = (ch.raw.get("serial") if ch else None)
        dt_name = DATA_TYPE_NAMES.get(dt, f"Data type {dt}")
        self._attr_name = f"{dt_name} ({serial})" if serial else f"{dt_name} (channel {channel_id})"

        if dt in DATA_TYPE_DEVICE_CLASS:
            self._attr_device_class = DATA_TYPE_DEVICE_CLASS[dt]
        if dt in DATA_TYPE_UNIT:
            self._attr_native_unit_of_measurement = DATA_TYPE_UNIT[dt]
        if dt in DATA_TYPE_STATE_CLASS:
            self._attr_state_class = DATA_TYPE_STATE_CLASS[dt]

    def _find_channel(self):
        for ch in self._coordinator.data.channels_by_source.get(self._source_id, []):
            if ch.channel_id == self._channel_id:
                return ch
        return None

    @property
    def device_info(self):
        device_class_title = DEVICE_CLASS_TITLES.get(self._group_device_class, "Устройство")
        return {
            "identifiers": {(DOMAIN, f"source_{self._source_id}")},
            "name": f"Ватериус • {device_class_title}",
            "manufacturer": HA_DEVICE_MANUFACTURER,
            "model": HA_DEVICE_MODEL,
        }

    @property
    def native_value(self) -> Any:
        ch = self._find_channel()
        return ch.last_value if ch else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        ch = self._find_channel()
        if not ch:
            return {}
        attrs = build_channel_attrs(ch.raw, ch.uk_vals)
        return attrs


class WateriusExportDiagnosticSensor(_BaseWateriusEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:calendar"
    _attr_device_class = "timestamp"
    _attr_name = "Срок оплаты"

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator,
        *,
        source_id: int,
        source_name: str,
        export_id: int,
        group_device_class: Optional[str],
    ) -> None:
        super().__init__(entry, coordinator)
        self._source_id = source_id
        self._source_name = (source_name or "").strip() or f"Source {source_id}"
        self._export_id = export_id
        self._group_device_class = group_device_class
        self._attr_unique_id = f"{entry.entry_id}_source_{source_id}_export_{export_id}_diag"

    @property
    def device_info(self):
        device_class_title = DEVICE_CLASS_TITLES.get(self._group_device_class, "Устройство")
        return {
            "identifiers": {(DOMAIN, f"source_{self._source_id}")},
            "name": f"Ватериус • {device_class_title}",
            "manufacturer": HA_DEVICE_MANUFACTURER,
            "model": HA_DEVICE_MODEL,
        }

    def _find_export_raw(self) -> Optional[Dict[str, Any]]:
        ex = (self._coordinator.data.exports_by_source or {}).get(self._source_id, {}).get(self._export_id)
        return ex.raw if ex else None

    @property
    def native_value(self):
        """Return due date as timezone-aware datetime for device_class=timestamp."""
        raw = self._find_export_raw() or {}
        tarif_raw = (raw.get("tarif_ended") or "").strip()
        norm = normalize_tarif_ended(tarif_raw)
        if not norm:
            return None
        try:
            dt = dt_util.parse_datetime(norm)
        except Exception:
            dt = None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_util.UTC)
        return dt

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        raw = self._find_export_raw() or {}
        tarif_raw = (raw.get("tarif_ended") or "").strip()
        days_left = compute_days_left(tarif_raw)
        personal_account = parse_personal_account(raw.get("title4"))
        return {
            "Название устройства": self._source_name,
            "УК": raw.get("title2"),
            "Лицевой счёт": personal_account,
            "Дата отправки": raw.get("send_date_description"),
            "Телефон пользователя": raw.get("user_contact"),
            "Дней до оплаты": days_left,
        }
