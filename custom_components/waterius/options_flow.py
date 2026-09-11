from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WateriusApi, WateriusApiError
from .const import (
    CHANNELS_URL,
    CONF_METER_MAPPINGS,
    CONF_RECONFIGURE_MAPPINGS,
    CONF_TOKEN,
    CONF_SYNC_INTERVAL,
    DATA_TYPE_NAMES,
    DEFAULT_SYNC_INTERVAL,
    SOURCES_URL,
)
from .helpers import extract_source_id

_LOGGER = logging.getLogger(__name__)


class WateriusOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._api: WateriusApi | None = None
        self._base_options: dict[str, Any] = {}
        self._sources: list[dict[str, Any]] = []
        self._channels: list[dict[str, Any]] = []
        self._index = 0
        self._mappings: list[dict[str, Any]] = []

    def _value(self, key: str, default=None):
        return self._config_entry.options.get(key, self._config_entry.data.get(key, default))

    def _existing_entity_for_channel(self, channel: dict[str, Any]) -> str:
        mappings = self._value(CONF_METER_MAPPINGS, []) or []
        channel_id = channel.get("id")
        channel_sid = extract_source_id(channel)
        channel_type = channel.get("data_type")
        channel_serial = str(channel.get("serial") or "")

        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            mapped_channel = mapping.get("channel_id")
            if mapped_channel is not None and channel_id is not None:
                try:
                    if int(mapped_channel) == int(channel_id):
                        return str(mapping.get("entity_id") or "")
                except (TypeError, ValueError):
                    pass

            # A freshly bootstrapped Universal mapping may not yet know channel_id.
            # Match it to the now-visible API channel by source/type/serial when possible.
            mapped_sid = mapping.get("source_id")
            same_source = mapped_sid is None or channel_sid is None or mapped_sid == channel_sid
            same_type = mapping.get("data_type") == channel_type
            mapped_serial = str(mapping.get("serial") or "")
            same_serial = not mapped_serial or not channel_serial or mapped_serial == channel_serial
            if same_source and same_type and same_serial:
                return str(mapping.get("entity_id") or "")
        return ""

    @staticmethod
    def _mapping_matches_channel(mapping: dict[str, Any], channel: dict[str, Any]) -> bool:
        mapped_channel = mapping.get("channel_id")
        channel_id = channel.get("id")
        if mapped_channel is not None and channel_id is not None:
            try:
                if int(mapped_channel) == int(channel_id):
                    return True
            except (TypeError, ValueError):
                pass

        mapped_sid = mapping.get("source_id")
        channel_sid = extract_source_id(channel)
        same_source = mapped_sid is None or channel_sid is None or str(mapped_sid) == str(channel_sid)
        same_type = mapping.get("data_type") == channel.get("data_type")
        mapped_serial = str(mapping.get("serial") or "").strip()
        channel_serial = str(channel.get("serial") or "").strip()
        same_serial = not mapped_serial or not channel_serial or mapped_serial == channel_serial
        return same_source and same_type and same_serial

    def _original_universal_mapping(self, channel: dict[str, Any]) -> dict[str, Any] | None:
        """Recover original Universal metadata even after an older Options rewrite."""
        original = self._config_entry.data.get(CONF_METER_MAPPINGS, []) or []
        for mapping in original:
            if not isinstance(mapping, dict):
                continue
            if mapping.get("transport") != "universal" or not mapping.get("uc_key"):
                continue
            if self._mapping_matches_channel(mapping, channel):
                return mapping
        return None

    async def async_step_init(self, user_input=None):
        schema = vol.Schema(
            {
                vol.Required(CONF_TOKEN, default=self._value(CONF_TOKEN, "")): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    CONF_SYNC_INTERVAL,
                    default=self._value(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
                vol.Required(CONF_RECONFIGURE_MAPPINGS, default=False): cv.boolean,
            }
        )
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        token = str(user_input.get(CONF_TOKEN, "") or "").strip()
        if not token:
            return self.async_show_form(step_id="init", data_schema=schema, errors={"base": "invalid_token"})

        session = async_get_clientsession(self.hass)
        api = WateriusApi(session, token)
        try:
            self._sources = await api.fetch_sources(SOURCES_URL)
            self._channels = await api.fetch_channels(CHANNELS_URL)
        except WateriusApiError as err:
            _LOGGER.error("Waterius token validation error: %s", err)
            return self.async_show_form(step_id="init", data_schema=schema, errors={"base": "cannot_connect"})

        self._api = api
        self._base_options = {
            CONF_TOKEN: token,
            CONF_SYNC_INTERVAL: int(user_input[CONF_SYNC_INTERVAL]),
            # Preserve previous mappings unless the user explicitly rebuilds them.
            CONF_METER_MAPPINGS: self._value(CONF_METER_MAPPINGS, []) or [],
        }

        if not user_input.get(CONF_RECONFIGURE_MAPPINGS):
            return self.async_create_entry(title="", data=self._base_options)

        if not self._channels:
            return self.async_show_form(step_id="init", data_schema=schema, errors={"base": "no_channels"})

        self._index = 0
        self._mappings = []
        return await self.async_step_mapping()

    async def async_step_mapping(self, user_input=None):
        if user_input is not None:
            channel = self._channels[self._index]
            entity_id = str(user_input.get("entity_id", "") or "").strip()
            if entity_id:
                mapping = {
                    "transport": "channel_api",
                    "source_id": extract_source_id(channel),
                    "channel_id": int(channel["id"]),
                    "data_type": channel.get("data_type"),
                    "serial": str(channel.get("serial") or ""),
                    "entity_id": entity_id,
                }

                # A Universal device must continue to be sent through uc.waterius.ru.
                # Up to 1.1.15 the reconfiguration wizard accidentally replaced that
                # transport with channel_api and discarded key/channel-position metadata.
                # Recover it from ConfigEntry.data, which still holds the original setup.
                original = self._original_universal_mapping(channel)
                if original is not None:
                    for key in ("transport", "group_id", "group_name", "uc_key", "uc_channel"):
                        if key in original:
                            mapping[key] = original[key]

                self._mappings.append(mapping)
            self._index += 1

        if self._index >= len(self._channels):
            data = dict(self._base_options)
            data[CONF_METER_MAPPINGS] = self._mappings
            return self.async_create_entry(title="", data=data)

        channel = self._channels[self._index]
        channel_id = int(channel["id"])
        data_type = channel.get("data_type")
        label = DATA_TYPE_NAMES.get(data_type, f"Тип {data_type}")
        serial = str(channel.get("serial") or "—")
        default_entity = self._existing_entity_for_channel(channel)

        field = vol.Optional("entity_id", default=default_entity) if default_entity else vol.Optional("entity_id")
        return self.async_show_form(
            step_id="mapping",
            data_schema=vol.Schema(
                {
                    field: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["sensor", "input_number"])
                    ),
                }
            ),
            description_placeholders={
                "number": str(self._index + 1),
                "total": str(len(self._channels)),
                "meter": label,
                "serial": serial,
            },
        )
