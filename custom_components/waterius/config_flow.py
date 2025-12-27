from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, CONF_TOKEN, CONF_NAME, CONF_SCAN_INTERVAL, DEFAULT_NAME


class WateriusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is None:
            schema = vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
                    vol.Required(CONF_TOKEN): cv.string,
                    vol.Optional(CONF_SCAN_INTERVAL, default=15): vol.Coerce(int),
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        token = user_input[CONF_TOKEN].strip()
        if not token:
            return self.async_show_form(step_id="user", errors={"base": "invalid_token"})

        await self.async_set_unique_id(f"waterius_{token[-8:]}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=user_input[CONF_NAME],
            data={
                CONF_NAME: user_input[CONF_NAME],
                CONF_TOKEN: token,
                CONF_SCAN_INTERVAL: int(user_input.get(CONF_SCAN_INTERVAL, 15)),
            },
        )



@staticmethod
def async_get_options_flow(config_entry):
    from .options_flow import WateriusOptionsFlowHandler
    return WateriusOptionsFlowHandler(config_entry)
