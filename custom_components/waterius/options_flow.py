from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WateriusApi, WateriusApiError
from .const import (
    CONF_TOKEN,
    CONF_SCAN_INTERVAL,
    SOURCES_URL,
    CONF_UC_SOURCE_ENTITY,
    CONF_UC_SEND_INTERVAL,
)


_LOGGER = logging.getLogger(__name__)


class WateriusOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    def _value(self, key: str, default=None):
        return self._config_entry.options.get(key, self._config_entry.data.get(key, default))

    def _schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_TOKEN, default=self._value(CONF_TOKEN, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_SCAN_INTERVAL, default=self._value(CONF_SCAN_INTERVAL, 15)): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Optional(CONF_UC_SOURCE_ENTITY, default=self._value(CONF_UC_SOURCE_ENTITY, "")): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_UC_SEND_INTERVAL, default=self._value(CONF_UC_SEND_INTERVAL, 1440)): vol.All(vol.Coerce(int), vol.Range(min=1, max=10080)),
            }
        )

    async def async_step_init(self, user_input=None):
        schema = self._schema()

        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        data = dict(user_input)
        for key in (CONF_TOKEN,):
            if key in data and isinstance(data[key], str):
                data[key] = data[key].strip()

        token = str(data.get(CONF_TOKEN, "") or "").strip()
        if not token:
            return self.async_show_form(
                step_id="init",
                data_schema=schema,
                errors={"base": "invalid_token"},
            )

        session = async_get_clientsession(self.hass)
        api = WateriusApi(session, token)
        try:
            await api.fetch_sources(SOURCES_URL)
        except WateriusApiError as err:
            _LOGGER.error("Waterius token validation error: %s", err)
            return self.async_show_form(
                step_id="init",
                data_schema=schema,
                errors={"base": "cannot_connect"},
            )

        return self.async_create_entry(title="", data=data)
