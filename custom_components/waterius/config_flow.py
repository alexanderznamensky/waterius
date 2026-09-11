from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WateriusApi, WateriusApiError
from .const import (
    HOME_ASSISTANT_ADD_URL,
    CHANNELS_URL,
    CONF_METER_MAPPINGS,
    CONF_METER_TYPES,
    CONF_NAME,
    CONF_TOKEN,
    CONF_SYNC_INTERVAL,
    DATA_TYPE_NAMES,
    DEFAULT_NAME,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
    ELECTRICITY_TARIFF_CHANNELS,
    ELECTRICITY_TARIFF_LABELS,
    METER_TYPE_LABELS,
    SOURCES_URL,
    UC_SEND_URL,
)
from .helpers import extract_source_id, normalize_ha_meter_value

_LOGGER = logging.getLogger(__name__)


def _source_entity_selector() -> selector.EntitySelector:
    """Select a cumulative value source from Home Assistant."""
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["sensor", "input_number"])
    )


def _text_selector() -> selector.TextSelector:
    return selector.TextSelector(selector.TextSelectorConfig())


def _source_id(raw: dict[str, Any] | None) -> int | None:
    if not isinstance(raw, dict):
        return None
    value = raw.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_universal_key(raw: dict[str, Any] | None) -> str:
    if not isinstance(raw, dict):
        return ""
    for name in ("key", "uc_key", "send_key", "api_key", "universal_key"):
        value = raw.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class WateriusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure Waterius and map Home Assistant total sensors to Waterius."""

    VERSION = 3

    def __init__(self) -> None:
        self._base_data: dict[str, Any] = {}
        self._api: WateriusApi | None = None
        self._sources: list[dict[str, Any]] = []
        self._channels: list[dict[str, Any]] = []
        self._mappings: list[dict[str, Any]] = []

        self._existing_index = 0
        self._selected_meter_types: list[str] = []
        self._electricity_tariff = "single"
        self._new_groups: list[dict[str, Any]] = []
        self._group_index = 0
        self._bootstrap_before_ids: set[int] = set()
        self._bootstrap_source: dict[str, Any] | None = None
        self._bootstrap_error = ""
        self._bootstrap_form_error = ""
        self._used_universal_keys: set[str] = set()
        self._pending_bootstrap_key = ""
        self._bootstrap_details = ""
        self._bootstrap_post_accepted = False

    def _base_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_SYNC_INTERVAL, default=DEFAULT_SYNC_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=1440)
                ),
            }
        )

    async def async_step_user(self, user_input=None):
        schema = self._base_schema()
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=schema)

        token = str(user_input.get(CONF_TOKEN, "") or "").strip()
        if not token:
            return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "invalid_token"})

        session = async_get_clientsession(self.hass)
        api = WateriusApi(session, token)
        try:
            sources = await api.fetch_sources(SOURCES_URL)
            channels = await api.fetch_channels(CHANNELS_URL)
        except WateriusApiError as err:
            _LOGGER.error("Waterius token validation error: %s", err)
            return self.async_show_form(step_id="user", data_schema=schema, errors={"base": "cannot_connect"})

        await self.async_set_unique_id(f"waterius_{token[-8:]}")
        self._abort_if_unique_id_configured()

        self._base_data = {
            CONF_NAME: user_input[CONF_NAME],
            CONF_TOKEN: token,
            CONF_SYNC_INTERVAL: int(user_input.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL)),
        }
        self._api = api
        self._sources = sources
        self._channels = channels

        # Existing channels mean Waterius is already configured. We only map HA sensors.
        if channels:
            self._existing_index = 0
            return await self.async_step_existing_channel()

        # No channels: run the complete meter setup wizard.
        return await self.async_step_meter_types()

    async def async_step_existing_channel(self, user_input=None):
        if user_input is not None:
            channel = self._channels[self._existing_index]
            entity_id = str(user_input.get("entity_id", "") or "").strip()
            if entity_id:
                sid = extract_source_id(channel)
                self._mappings.append(
                    {
                        "transport": "channel_api",
                        "source_id": sid,
                        "channel_id": int(channel["id"]),
                        "data_type": channel.get("data_type"),
                        "serial": str(channel.get("serial") or ""),
                        "entity_id": entity_id,
                    }
                )
            self._existing_index += 1

        if self._existing_index >= len(self._channels):
            return self._finish_entry()

        channel = self._channels[self._existing_index]
        sid = extract_source_id(channel)
        source_name = ""
        for source in self._sources:
            if _source_id(source) == sid:
                source_name = str(source.get("name") or "")
                break
        data_type = channel.get("data_type")
        label = DATA_TYPE_NAMES.get(data_type, f"Тип {data_type}")
        serial = str(channel.get("serial") or "—")
        return self.async_show_form(
            step_id="existing_channel",
            data_schema=vol.Schema({vol.Optional("entity_id"): _source_entity_selector()}),
            description_placeholders={
                "number": str(self._existing_index + 1),
                "total": str(len(self._channels)),
                "source": source_name or f"Source {sid}",
                "meter": label,
                "serial": serial,
            },
        )

    async def async_step_meter_types(self, user_input=None):
        schema = vol.Schema({vol.Required(CONF_METER_TYPES): cv.multi_select(METER_TYPE_LABELS)})
        if user_input is None:
            return self.async_show_form(step_id="meter_types", data_schema=schema)

        selected = list(user_input.get(CONF_METER_TYPES) or [])
        if not selected:
            return self.async_show_form(step_id="meter_types", data_schema=schema, errors={"base": "select_meter"})

        self._selected_meter_types = selected
        if "electricity" in selected:
            return await self.async_step_electricity_tariff()

        self._build_new_groups()
        return await self._next_meter_details()

    async def async_step_electricity_tariff(self, user_input=None):
        schema = vol.Schema({vol.Required("electricity_tariff", default="single"): vol.In(ELECTRICITY_TARIFF_LABELS)})
        if user_input is None:
            return self.async_show_form(step_id="electricity_tariff", data_schema=schema)

        self._electricity_tariff = str(user_input["electricity_tariff"])
        self._build_new_groups()
        return await self._next_meter_details()

    def _build_new_groups(self) -> None:
        groups: list[dict[str, Any]] = []

        water_channels = []
        if "cold_water" in self._selected_meter_types:
            water_channels.append({"code": "cold", "data_type": 0, "label": "Холодная вода"})
        if "hot_water" in self._selected_meter_types:
            water_channels.append({"code": "hot", "data_type": 1, "label": "Горячая вода"})
        if water_channels:
            groups.append({"kind": "water", "name": "Вода", "channels": water_channels})

        if "electricity" in self._selected_meter_types:
            channels = [
                {"code": code, "data_type": data_type, "label": label}
                for code, data_type, label in ELECTRICITY_TARIFF_CHANNELS[self._electricity_tariff]
            ]
            groups.append({"kind": "electricity", "name": "Электроэнергия", "channels": channels})

        singles = (
            ("gas", "Газ", 3),
            ("heat", "Отопление / тепло", 4),
            ("drinking_water", "Питьевая вода", 9),
            ("other", "Другой счётчик", 10),
        )
        for kind, name, data_type in singles:
            if kind in self._selected_meter_types:
                groups.append(
                    {"kind": kind, "name": name, "channels": [{"code": kind, "data_type": data_type, "label": name}]}
                )

        self._new_groups = groups
        self._group_index = 0

    async def _next_meter_details(self):
        if self._group_index >= len(self._new_groups):
            return await self.async_step_review()
        kind = self._new_groups[self._group_index]["kind"]
        return await getattr(self, f"async_step_meter_{kind}")()

    async def async_step_meter_water(self, user_input=None):
        group = self._new_groups[self._group_index]
        schema_dict: dict[Any, Any] = {}
        for channel in group["channels"]:
            code = channel["code"]
            schema_dict[vol.Required(f"{code}_serial")] = _text_selector()
            schema_dict[vol.Required(f"{code}_entity")] = _source_entity_selector()
        schema = vol.Schema(schema_dict)
        if user_input is None:
            return self.async_show_form(step_id="meter_water", data_schema=schema)

        for channel in group["channels"]:
            code = channel["code"]
            channel["serial"] = str(user_input[f"{code}_serial"]).strip()
            channel["entity_id"] = str(user_input[f"{code}_entity"]).strip()
        self._group_index += 1
        return await self._next_meter_details()

    async def async_step_meter_electricity(self, user_input=None):
        group = self._new_groups[self._group_index]
        schema_dict: dict[Any, Any] = {vol.Required("electricity_serial"): _text_selector()}
        for channel in group["channels"]:
            schema_dict[vol.Required(f"entity_{channel['code']}")] = _source_entity_selector()
        schema = vol.Schema(schema_dict)
        if user_input is None:
            return self.async_show_form(
                step_id="meter_electricity",
                data_schema=schema,
                description_placeholders={"tariff": ELECTRICITY_TARIFF_LABELS[self._electricity_tariff]},
            )

        serial = str(user_input["electricity_serial"]).strip()
        for channel in group["channels"]:
            channel["serial"] = serial
            channel["entity_id"] = str(user_input[f"entity_{channel['code']}"]).strip()
        self._group_index += 1
        return await self._next_meter_details()

    async def _single_meter_step(self, step_id: str, user_input=None):
        group = self._new_groups[self._group_index]
        kind = group["kind"]
        schema = vol.Schema(
            {
                vol.Required("serial"): _text_selector(),
                vol.Required("entity_id"): _source_entity_selector(),
            }
        )
        if user_input is None:
            return self.async_show_form(
                step_id=step_id,
                data_schema=schema,
                description_placeholders={"meter": group["name"]},
            )

        group["channels"][0]["serial"] = str(user_input["serial"]).strip()
        group["channels"][0]["entity_id"] = str(user_input["entity_id"]).strip()
        self._group_index += 1
        return await self._next_meter_details()

    async def async_step_meter_gas(self, user_input=None):
        return await self._single_meter_step("meter_gas", user_input)

    async def async_step_meter_heat(self, user_input=None):
        return await self._single_meter_step("meter_heat", user_input)

    async def async_step_meter_drinking_water(self, user_input=None):
        return await self._single_meter_step("meter_drinking_water", user_input)

    async def async_step_meter_other(self, user_input=None):
        return await self._single_meter_step("meter_other", user_input)

    def _validate_new_entities(self) -> tuple[bool, str]:
        lines: list[str] = []
        valid = True
        for group in self._new_groups:
            lines.append(group["name"] + ":")
            for channel in group["channels"]:
                entity_id = channel["entity_id"]
                state = self.hass.states.get(entity_id)
                if state is None or state.state in ("", "unknown", "unavailable"):
                    lines.append(f"  {channel['label']}: {entity_id} — недоступен")
                    valid = False
                    continue
                try:
                    normalized, normalized_unit, converted = normalize_ha_meter_value(state, int(channel["data_type"]))
                except ValueError as err:
                    lines.append(f"  {channel['label']}: {entity_id} — {err}")
                    valid = False
                    continue
                unit = state.attributes.get("unit_of_measurement") or ""
                if converted:
                    lines.append(
                        f"  {channel['label']}: {state.state} {unit} → {normalized:.6g} {normalized_unit} ({entity_id})"
                    )
                else:
                    lines.append(f"  {channel['label']}: {state.state} {unit} ({entity_id})".strip())
        return valid, "\n".join(lines)

    async def async_step_review(self, user_input=None):
        valid, summary = self._validate_new_entities()
        schema = vol.Schema({vol.Required("confirm", default=True): cv.boolean})
        if user_input is None:
            return self.async_show_form(
                step_id="review",
                data_schema=schema,
                description_placeholders={"summary": summary},
                errors={} if valid else {"base": "invalid_source_sensor"},
            )

        if not user_input.get("confirm"):
            self._group_index = 0
            return await self._next_meter_details()
        if not valid:
            return self.async_show_form(
                step_id="review",
                data_schema=schema,
                description_placeholders={"summary": summary},
                errors={"base": "invalid_source_sensor"},
            )

        self._group_index = 0
        return await self.async_step_bootstrap()

    async def async_step_bootstrap(self, user_input=None):
        """Start setup of a Waterius Home Assistant device for one meter group.

        Current Waterius flow (2026): the user creates a Home Assistant device at
        /devices/add/home-assistant, Waterius immediately shows its unique key, and
        counters/channels are created after the first readings are sent.
        """
        if self._group_index >= len(self._new_groups):
            return self._finish_entry()

        assert self._api is not None

        # Snapshot existing sources BEFORE the user opens Waterius and creates the
        # new Home Assistant device. This lets us bind the pasted key to the newly
        # created source without guessing.
        try:
            fresh_sources = await self._api.fetch_sources(SOURCES_URL)
            self._sources = fresh_sources
        except WateriusApiError as err:
            _LOGGER.error("Could not refresh Waterius sources before device creation: %s", err)
            return self.async_show_form(
                step_id="universal_key",
                data_schema=self._universal_key_schema(),
                description_placeholders={
                    "meter": self._new_groups[self._group_index]["name"],
                    "url": HOME_ASSISTANT_ADD_URL,
                },
                errors={"base": "cannot_connect"},
            )

        self._bootstrap_before_ids = {
            sid for sid in (_source_id(x) for x in self._sources) if sid is not None
        }
        self._bootstrap_source = None
        self._bootstrap_form_error = ""
        self._pending_bootstrap_key = ""
        self._bootstrap_details = ""
        self._bootstrap_post_accepted = False
        return await self.async_step_universal_key()

    def _universal_key_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required("universal_key"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )

    async def async_step_universal_key(self, user_input=None):
        """Ask for the key shown by Waterius after creating a Home Assistant device."""
        group = self._new_groups[self._group_index]
        schema = self._universal_key_schema()

        if user_input is None:
            errors = {"base": self._bootstrap_form_error} if self._bootstrap_form_error else {}
            self._bootstrap_form_error = ""
            return self.async_show_form(
                step_id="universal_key",
                data_schema=schema,
                description_placeholders={"meter": group["name"], "url": HOME_ASSISTANT_ADD_URL},
                errors=errors,
            )

        key = str(user_input.get("universal_key", "") or "").strip()
        if not key:
            return self.async_show_form(
                step_id="universal_key",
                data_schema=schema,
                description_placeholders={"meter": group["name"], "url": HOME_ASSISTANT_ADD_URL},
                errors={"base": "invalid_universal_key"},
            )
        if key in self._used_universal_keys:
            return self.async_show_form(
                step_id="universal_key",
                data_schema=schema,
                description_placeholders={"meter": group["name"], "url": HOME_ASSISTANT_ADD_URL},
                errors={"base": "duplicate_universal_key"},
            )

        # The Home Assistant device exists at this point, but its counters do not.
        # The first payload must create them, and v1.1.7 verifies that they really
        # appeared before the wizard is allowed to continue.
        self._pending_bootstrap_key = key
        return await self._send_bootstrap_group(key)

    def _matching_bootstrap_channels(
        self, fresh_channels: list[dict[str, Any]], sid: int | None, group: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Return expected channels that are visible in Waterius after bootstrap."""
        pool = fresh_channels
        if sid is not None:
            pool = [ch for ch in fresh_channels if extract_source_id(ch) == sid]

        matches: list[dict[str, Any]] = []
        used_ids: set[Any] = set()
        for expected in group["channels"]:
            expected_type = int(expected["data_type"])
            expected_serial = str(expected.get("serial") or "").strip()
            found = None
            for ch in pool:
                if ch.get("id") in used_ids:
                    continue
                try:
                    actual_type = int(ch.get("data_type"))
                except (TypeError, ValueError):
                    continue
                if actual_type != expected_type:
                    continue
                actual_serial = str(ch.get("serial") or "").strip()
                # Do not accept an intermediate channel record with an empty or
                # different serial. Waterius updates channel metadata asynchronously.
                if expected_serial and actual_serial != expected_serial:
                    continue
                found = ch
                break
            if found is not None:
                matches.append(found)
                used_ids.add(found.get("id"))
        return matches

    async def _poll_bootstrap_result(
        self, group: dict[str, Any], timeout_seconds: int = 30, poll_interval: float = 2.0
    ) -> tuple[int | None, list[dict[str, Any]], list[dict[str, Any]]]:
        """Wait for Waterius eventual consistency after a successful universal POST.

        uc.waterius.ru acknowledges the payload before account.waterius.ru necessarily
        exposes all channel metadata. Poll until every expected data_type + serial is
        visible or the timeout expires.
        """
        sid = _source_id(self._bootstrap_source)
        last_sources: list[dict[str, Any]] = []
        last_channels: list[dict[str, Any]] = []
        deadline = time.monotonic() + max(1, timeout_seconds)
        attempt = 0

        while True:
            attempt += 1
            try:
                fresh_sources = await self._api.fetch_sources(SOURCES_URL)
                fresh_channels = await self._api.fetch_channels(CHANNELS_URL)
                self._sources = fresh_sources
                self._channels = fresh_channels
                last_sources = fresh_sources
                last_channels = fresh_channels

                if sid is None:
                    new_sources = [
                        x for x in fresh_sources if _source_id(x) not in self._bootstrap_before_ids
                    ]
                    if new_sources:
                        self._bootstrap_source = new_sources[-1]
                        sid = _source_id(self._bootstrap_source)

                matches = self._matching_bootstrap_channels(fresh_channels, sid, group)
                _LOGGER.debug(
                    "Waterius bootstrap poll %s attempt=%s source_id=%s matches=%s/%s",
                    group["name"], attempt, sid, len(matches), len(group["channels"])
                )
                if len(matches) == len(group["channels"]):
                    return sid, last_sources, last_channels
            except WateriusApiError as err:
                _LOGGER.debug("Waterius metadata refresh after bootstrap failed: %s", err)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        return sid, last_sources, last_channels

    def _complete_bootstrap_group(
        self, key: str, sid: int | None, fresh_channels: list[dict[str, Any]]
    ):
        """Persist mappings once Waterius exposes every expected channel."""
        group = self._new_groups[self._group_index]
        matches = self._matching_bootstrap_channels(fresh_channels, sid, group)
        if len(matches) != len(group["channels"]):
            return None

        group_id = f"{group['kind']}_{self._group_index + 1}"
        for index, channel in enumerate(group["channels"]):
            matched = matches[index]
            self._mappings.append(
                {
                    "transport": "universal",
                    "group_id": group_id,
                    "group_name": group["name"],
                    "source_id": sid,
                    "channel_id": matched.get("id"),
                    "uc_key": key,
                    "uc_channel": index,
                    "data_type": int(channel["data_type"]),
                    "serial": channel["serial"],
                    "entity_id": channel["entity_id"],
                }
            )
        self._used_universal_keys.add(key)
        self._pending_bootstrap_key = ""
        self._bootstrap_details = ""
        self._bootstrap_post_accepted = False
        self._group_index += 1
        return True

    def _set_bootstrap_wait_details(
        self, group: dict[str, Any], sid: int | None, responses: list[str], last_error: Exception | None = None
    ) -> None:
        actual = []
        for ch in self._channels:
            if sid is None or extract_source_id(ch) == sid:
                actual.append(
                    f"id={ch.get('id')} type={ch.get('data_type')} serial={ch.get('serial')} "
                    f"value={ch.get('last_value', ch.get('value'))}"
                )
        expected = [
            f"{ch['label']}: type={ch['data_type']} serial={ch.get('serial')}"
            for ch in group["channels"]
        ]
        self._bootstrap_details = (
            "Ожидалось: " + "; ".join(expected) + "\n"
            + "Source ID: " + str(sid or "не определён") + "\n"
            + "Каналы API: " + ("; ".join(actual) if actual else "нет") + "\n"
            + "Ответ POST: " + (" | ".join(responses) if responses else "нет")
        )
        if last_error is not None:
            self._bootstrap_details += f"\nПоследняя ошибка: {last_error}"

    async def _send_bootstrap_group(
        self, key: str, *, max_posts: int = 2, poll_seconds: int = 15
    ):
        """Send bootstrap data and verify that Waterius applied channel metadata.

        Empirically, a freshly-created Home Assistant device may acknowledge the first
        universal POST with HTTP 200 before it is ready to apply ch/data_type/serial
        metadata.  In that state account.waterius.ru keeps exposing the placeholder
        channel.  Re-sending the *same* cumulative readings with the *same* key is safe
        and is required for some newly provisioned devices.
        """
        assert self._api is not None
        group = self._new_groups[self._group_index]
        payload: dict[str, Any] = {"key": key, "name": group["name"]}

        # The user creates the Home Assistant device in the Waterius cabinet before
        # pasting its key. Detect the corresponding new source if the account API
        # already exposes it.
        try:
            fresh_sources = await self._api.fetch_sources(SOURCES_URL)
            new_sources = [x for x in fresh_sources if _source_id(x) not in self._bootstrap_before_ids]
            if new_sources:
                self._bootstrap_source = new_sources[-1]
        except WateriusApiError:
            pass

        for index, channel in enumerate(group["channels"]):
            state = self.hass.states.get(channel["entity_id"])
            if state is None or state.state in ("", "unknown", "unavailable"):
                return await self.async_step_review()
            try:
                value, _unit, _converted = normalize_ha_meter_value(state, int(channel["data_type"]))
            except ValueError:
                return await self.async_step_review()
            payload[f"ch{index}"] = value
            payload[f"data_type{index}"] = int(channel["data_type"])
            payload[f"serial{index}"] = channel["serial"]

        safe_payload = {k: ("***" if k == "key" else v) for k, v in payload.items()}
        responses: list[str] = []
        last_error: WateriusApiError | None = None
        accepted_any = False

        for post_attempt in range(1, max(1, max_posts) + 1):
            try:
                response = await self._api.send_universal_payload(UC_SEND_URL, payload)
                accepted_any = True
                responses.append(
                    f"POST {post_attempt}: {UC_SEND_URL} -> {str(response)[:500]}"
                )
                _LOGGER.info(
                    "Waterius bootstrap %s POST attempt=%s payload=%s response=%s",
                    group["name"], post_attempt, safe_payload, str(response)[:1000]
                )
            except WateriusApiError as err:
                last_error = err
                responses.append(f"POST {post_attempt}: {UC_SEND_URL} -> ERROR {err}")
                _LOGGER.warning(
                    "Waterius bootstrap %s POST attempt=%s failed: %s; payload=%s",
                    group["name"], post_attempt, err, safe_payload
                )
                # A transport/HTTP failure can be retried by the same loop.
                if post_attempt < max_posts:
                    await asyncio.sleep(3)
                    continue
                break

            self._bootstrap_post_accepted = True
            sid, _fresh_sources, fresh_channels = await self._poll_bootstrap_result(
                group, timeout_seconds=max(1, poll_seconds), poll_interval=2.0
            )
            if self._complete_bootstrap_group(key, sid, fresh_channels):
                return await self.async_step_bootstrap()

            # Waterius sometimes returns HTTP 200 while a brand-new device is still
            # only a placeholder in account.waterius.ru. Waiting alone does not always
            # apply channel metadata; retry the identical payload with the same key.
            if post_attempt < max_posts:
                _LOGGER.warning(
                    "Waterius bootstrap %s POST %s accepted but metadata incomplete; "
                    "re-sending the same payload with the same key after 5 seconds",
                    group["name"], post_attempt
                )
                await asyncio.sleep(5)
                continue

        sid = _source_id(self._bootstrap_source)
        self._bootstrap_post_accepted = accepted_any
        self._set_bootstrap_wait_details(group, sid, responses, last_error)
        if accepted_any:
            _LOGGER.warning(
                "Waterius bootstrap accepted but metadata is still incomplete for %s after %s POST(s). %s",
                group["name"], max(1, max_posts), self._bootstrap_details
            )
        else:
            _LOGGER.error(
                "Waterius bootstrap POST failed for %s. %s", group["name"], self._bootstrap_details
            )
        return await self.async_step_bootstrap_verify()

    async def async_step_bootstrap_verify(self, user_input=None):
        """Retry the same bootstrap payload when Waterius still exposes a placeholder."""
        group = self._new_groups[self._group_index]
        if user_input is not None:
            if not self._pending_bootstrap_key:
                return await self.async_step_universal_key()

            # Do not create another Waterius device and do not ask for another key.
            # Re-send exactly this group's current readings with the already registered
            # key, then give account.waterius.ru up to 30 seconds to expose metadata.
            return await self._send_bootstrap_group(
                self._pending_bootstrap_key, max_posts=1, poll_seconds=30
            )

        return self.async_show_form(
            step_id="bootstrap_verify",
            data_schema=vol.Schema({}),
            description_placeholders={
                "meter": group["name"],
                "details": self._bootstrap_details or "Нет диагностических данных",
            },
        )

    def _finish_entry(self):
        data = dict(self._base_data)
        data[CONF_METER_MAPPINGS] = self._mappings
        return self.async_create_entry(title=data[CONF_NAME], data=data)

    @staticmethod
    def async_get_options_flow(config_entry):
        from .options_flow import WateriusOptionsFlowHandler

        return WateriusOptionsFlowHandler(config_entry)
