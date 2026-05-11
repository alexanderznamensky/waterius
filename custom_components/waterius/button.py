from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HA_DEVICE_MANUFACTURER, HA_DEVICE_MODEL
from . import async_send_configured_uc_reading, async_send_all_readings_to_waterius


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            WateriusUpdateNowButton(entry, coordinator),
            WateriusSendConfiguredReadingButton(hass, entry),
            WateriusSendAllToWateriusButton(hass, entry),
        ],
        update_before_add=False,
    )


class _BaseWateriusButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    @property
    def device_info(self):
        """Attach buttons to a separate Waterius service device.

        Without device_info Home Assistant creates button entities, but they may not be
        visible on the integration device page. This makes them appear under the
        Waterius device together with other integration entities.
        """
        return {
            "identifiers": {(DOMAIN, f"entry_{self._entry.entry_id}")},
            "name": "Waterius",
            "manufacturer": HA_DEVICE_MANUFACTURER,
            "model": HA_DEVICE_MODEL,
        }


class WateriusUpdateNowButton(_BaseWateriusButton):
    _attr_name = "Update now"
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry, coordinator) -> None:
        super().__init__(entry)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_update_now"

    async def async_press(self) -> None:
        await self._coordinator.async_request_refresh()


class WateriusSendConfiguredReadingButton(_BaseWateriusButton):
    _attr_name = "Send configured reading"
    _attr_icon = "mdi:send"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_send_configured_reading"

    async def async_press(self) -> None:
        await async_send_configured_uc_reading(self._hass, self._entry)


class WateriusSendAllToWateriusButton(_BaseWateriusButton):
    _attr_name = "Send readings to Waterius"
    _attr_icon = "mdi:cloud-upload"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._hass = hass
        self._attr_unique_id = f"{entry.entry_id}_send_all_to_waterius"

    async def async_press(self) -> None:
        await async_send_all_readings_to_waterius(self._hass, self._entry)
