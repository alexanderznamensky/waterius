from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, Set

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WateriusApi, WateriusApiError
from .const import (
    CHANNELS_URL,
    SOURCES_URL,
    EXPORT_DETAIL_URL_TEMPLATE,
    CHANNEL_REPORTS_URL_TEMPLATE,
)
from .helpers import extract_export_id, extract_source_id, extract_uk_period_values


@dataclass
class WateriusChannel:
    channel_id: int
    last_value: Any
    raw: Dict[str, Any]
    uk_vals: Dict[str, Any]


@dataclass
class WateriusExport:
    export_id: int
    raw: Dict[str, Any]


@dataclass
class WateriusData:
    sources: Dict[int, str]
    channels_by_source: Dict[int, List[WateriusChannel]]
    exports_by_source: Dict[int, Dict[int, WateriusExport]]


class WateriusCoordinator(DataUpdateCoordinator[WateriusData]):
    def __init__(self, hass: HomeAssistant, api: WateriusApi, update_interval: timedelta) -> None:
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name="Waterius",
            update_interval=update_interval,
        )
        self.api = api

    async def _async_update_data(self) -> WateriusData:
        try:
            sources_raw = await self.api.fetch_sources(SOURCES_URL)
            sources: Dict[int, str] = {}
            for src in sources_raw:
                if "id" in src and "name" in src:
                    try:
                        sources[int(src["id"])] = str(src["name"])
                    except Exception:
                        continue

            channels_raw = await self.api.fetch_channels(CHANNELS_URL)

            channels_by_source: Dict[int, List[WateriusChannel]] = {}
            export_ids_all: Set[int] = set()

            for ch in channels_raw:
                if "id" not in ch:
                    continue
                try:
                    channel_id = int(ch["id"])
                except Exception:
                    continue

                sid = extract_source_id(ch)
                if sid is None:
                    continue

                export_id = extract_export_id(ch)
                if export_id is not None:
                    export_ids_all.add(export_id)

                last_value = ch.get("last_value", ch.get("value", ch.get("last")))

                rep_url = CHANNEL_REPORTS_URL_TEMPLATE.format(channel_id=channel_id)
                reports = await self.api.fetch_channel_reports(rep_url)
                uk_vals = extract_uk_period_values(reports)

                channels_by_source.setdefault(sid, []).append(
                    WateriusChannel(channel_id=channel_id, last_value=last_value, raw=ch, uk_vals=uk_vals)
                )

            for sid in sources.keys():
                channels_by_source.setdefault(sid, [])

            export_details: Dict[int, Dict[str, Any]] = {}
            for ex_id in sorted(export_ids_all):
                detail_url = EXPORT_DETAIL_URL_TEMPLATE.format(export_id=ex_id)
                detail = await self.api.fetch_export_detail(detail_url)
                export_details[ex_id] = detail if isinstance(detail, dict) else {"raw": detail}

            exports_by_source: Dict[int, Dict[int, WateriusExport]] = {}
            for sid, chs in channels_by_source.items():
                ex_ids: Set[int] = set()
                for ch in chs:
                    ex_id = extract_export_id(ch.raw)
                    if ex_id is not None:
                        ex_ids.add(ex_id)
                if ex_ids:
                    exports_by_source[sid] = {
                        ex_id: WateriusExport(export_id=ex_id, raw=export_details.get(ex_id, {}))
                        for ex_id in ex_ids
                    }

            return WateriusData(
                sources=sources,
                channels_by_source=channels_by_source,
                exports_by_source=exports_by_source,
            )

        except WateriusApiError as e:
            raise UpdateFailed(str(e)) from e
