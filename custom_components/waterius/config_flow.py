from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WateriusApi, WateriusApiError
from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_NAME,
    SOURCES_URL,
    CONF_UC_SOURCE_ENTITY,
    CONF_UC_SEND_INTERVAL,
)


_LOGGER = logging.getLogger(__name__)


class WateriusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def _schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_SCAN_INTERVAL, default=15): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Optional(CONF_UC_SOURCE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_UC_SEND_INTERVAL, default=1440): vol.All(vol.Coerce(int), vol.Range(min=1, max=10080)),
            }
        )

    async def async_step_user(self, user_input=None):
        schema = self._schema()

        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=schema)

        token = str(user_input.get(CONF_TOKEN, "") or "").strip()
        if not token:
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                errors={"base": "invalid_token"},
            )

        # Проверяем token сразу при настройке, чтобы не создавать заведомо нерабочую запись.
        session = async_get_clientsession(self.hass)
        api = WateriusApi(session, token)
        try:
            await api.fetch_sources(SOURCES_URL)
        except WateriusApiError as err:
            _LOGGER.error("Waterius token validation error: %s", err)
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                errors={"base": "cannot_connect"},
            )

        await self.async_set_unique_id(f"waterius_{token[-8:]}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=user_input[CONF_NAME],
            data={
                CONF_NAME: user_input[CONF_NAME],
                CONF_TOKEN: token,
                CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, 15)),
                CONF_UC_SOURCE_ENTITY: user_input.get(CONF_UC_SOURCE_ENTITY, ""),
                CONF_UC_SEND_INTERVAL: int(user_input.get(CONF_UC_SEND_INTERVAL, 1440)),
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        from .options_flow import WateriusOptionsFlowHandler

        return WateriusOptionsFlowHandler(config_entry)
