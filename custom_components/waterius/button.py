from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([WateriusUpdateNowButton(entry, coordinator)], update_before_add=False)


class WateriusUpdateNowButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Update now"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_update_now"

    async def async_press(self) -> None:
        await self._coordinator.async_request_refresh()
