from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import aiohttp


class WateriusApiError(Exception):
    """Raised for Waterius API errors."""


class WateriusApi:
    """Async client for account.waterius.ru API (Token auth)."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Token {self._token}",
            "Accept": "application/json",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 204:
                    return None

                if resp.status < 200 or resp.status >= 300:
                    body = (await resp.text())[:2000]
                    raise WateriusApiError(f"HTTP {resp.status} for {url}. Body: {body}")

                ct = (resp.headers.get("Content-Type") or "").lower()
                if "application/json" in ct:
                    return await resp.json()
                return await resp.text()
        except asyncio.TimeoutError as e:
            raise WateriusApiError(f"Timeout calling {url}") from e
        except aiohttp.ClientError as e:
            raise WateriusApiError(f"Network error calling {url}: {e}") from e

    async def get_paginated(self, url: str) -> List[Dict[str, Any]]:
        """Supports both DRF pagination dict and plain list."""
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url

        while next_url:
            data = await self._request_json("GET", next_url)

            if data is None:
                return items

            if isinstance(data, dict) and "results" in data:
                results = data.get("results") or []
                if isinstance(results, list):
                    items.extend([x for x in results if isinstance(x, dict)])
                nxt = data.get("next")
                next_url = nxt if isinstance(nxt, str) and nxt else None
                continue

            if isinstance(data, list):
                items.extend([x for x in data if isinstance(x, dict)])
                break

            raise WateriusApiError(f"Unexpected response format for {next_url}: {type(data)}")

        return items

    async def fetch_channels(self, url: str) -> List[Dict[str, Any]]:
        return await self.get_paginated(url)

    async def fetch_sources(self, url: str) -> List[Dict[str, Any]]:
        return await self.get_paginated(url)

    async def fetch_export_detail(self, url: str) -> Any:
        return await self._request_json("GET", url)

    async def fetch_channel_reports(self, url: str) -> List[Dict[str, Any]]:
        return await self.get_paginated(url)

    async def send_reading(self, url: str, value: Any) -> Any:
        """Send reading (value_obj) to reports endpoint."""
        return await self._request_json("POST", url, json_body={"value_obj": value})
