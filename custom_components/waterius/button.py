from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, HA_DEVICE_MANUFACTURER, HA_DEVICE_MODEL
from . import async_send_all_configured_readings


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    # v1.1.9 and earlier exposed a duplicate legacy button. Remove its registry
    # entry once so it does not remain as a disabled/orphaned entity after upgrade.
    registry = er.async_get(hass)
    legacy_entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{entry.entry_id}_send_configured_reading"
    )
    if legacy_entity_id:
        registry.async_remove(legacy_entity_id)

    async_add_entities(
        [
            WateriusUpdateNowButton(entry, coordinator),
            WateriusSendMappedReadingsButton(hass, entry),
        ],
        update_before_add=False,
    )


class _BaseWateriusButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"entry_{self._entry.entry_id}")},
            "name": "Waterius",
            "manufacturer": HA_DEVICE_MANUFACTURER,
            "model": HA_DEVICE_MODEL,
        }


class WateriusUpdateNowButton(_BaseWateriusButton):
    _attr_name = "Обновить данные"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__(entry)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_update_now"

    async def async_press(self) -> None:
        await self._coordinator.async_request_refresh()


class WateriusSendMappedReadingsButton(_BaseWateriusButton):
    _attr_name = "Отправить показания сейчас"
    _attr_icon = "mdi:cloud-upload"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_send_all_to_waterius"

    async def async_press(self) -> None:
        await async_send_all_configured_readings(self._hass, self._entry)
