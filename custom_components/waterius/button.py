from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, HA_DEVICE_MANUFACTURER, HA_DEVICE_MODEL
from . import async_synchronize_now


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # v1.1.13 and earlier exposed separate update/send buttons. Replace both with
    # one explicit synchronization action so the UI matches the unified timer.
    registry = er.async_get(hass)
    for old_unique_id in (
        f"{entry.entry_id}_update_now",
        f"{entry.entry_id}_send_all_to_waterius",
        f"{entry.entry_id}_send_configured_reading",
    ):
        entity_id = registry.async_get_entity_id("button", DOMAIN, old_unique_id)
        if entity_id:
            registry.async_remove(entity_id)

    async_add_entities([WateriusSyncButton(hass, entry)], update_before_add=False)


class WateriusSyncButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Синхронизация"
    _attr_icon = "mdi:sync"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_sync_now"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"entry_{self._entry.entry_id}")},
            "name": "Waterius",
            "manufacturer": HA_DEVICE_MANUFACTURER,
            "model": HA_DEVICE_MODEL,
        }

    async def async_press(self) -> None:
        await async_synchronize_now(self._hass, self._entry)
