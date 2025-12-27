from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .api import WateriusApi
from .const import DOMAIN, CONF_TOKEN, CONF_SCAN_INTERVAL, SERVICE_SEND_READING, SERVICE_SEND_ALL, CHANNEL_SEND_URL_TEMPLATE
from .coordinator import WateriusCoordinator

PLATFORMS = ["sensor", "button"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    api = WateriusApi(session, entry.data[CONF_TOKEN])

    interval_min = int(entry.data.get(CONF_SCAN_INTERVAL, 15))
    coordinator = WateriusCoordinator(hass, api, update_interval=timedelta(minutes=max(1, interval_min)))
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
