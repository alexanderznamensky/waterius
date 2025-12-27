from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .const import CONF_TOKEN


class WateriusOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        token = self.config_entry.options.get(CONF_TOKEN, self.config_entry.data.get(CONF_TOKEN, ""))
        scan = self.config_entry.options.get(CONF_SCAN_INTERVAL, self.config_entry.data.get(CONF_SCAN_INTERVAL))

        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN, default=token): str,
                vol.Required(CONF_SCAN_INTERVAL, default=scan): vol.All(int, vol.Range(min=10, max=86400)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
