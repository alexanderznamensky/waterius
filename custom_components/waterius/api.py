from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

import aiohttp


class WateriusApiError(Exception):
    """Raised for Waterius API errors."""


def _extract_token_from_response(data: Any) -> str | None:
    """Extract token from JSON, plain text, or simple HTML response."""
    if isinstance(data, dict):
        for key in ("token", "key", "auth_token", "access_token"):
            value = data.get(key)
            if value:
                return str(value).strip()

    text = str(data or "").strip()
    if not text:
        return None

    # Common case: endpoint returns the token itself as text.
    if "<" not in text and "\n" not in text and 16 <= len(text) <= 256:
        return text.strip().strip('"')

    # JSON rendered as text or token in HTML/pre/body.
    patterns = [
        r'"(?:token|key|auth_token|access_token)"\s*:\s*"([^"]+)"',
        r"'(?:token|key|auth_token|access_token)'\s*:\s*'([^']+)'",
        r"Token\s*[:=]\s*([A-Za-z0-9._\-]+)",
        r"Bearer\s+([A-Za-z0-9._\-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return None


async def _read_response(resp: aiohttp.ClientResponse) -> Any:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/json" in ct:
        try:
            return await resp.json()
        except Exception:
            return await resp.text()
    return await resp.text()


async def fetch_token(
    session: aiohttp.ClientSession,
    username: str,
    password: str,
    url: str,
) -> str:
    """Fetch Waterius API token by logging in via dj-rest-auth.

    The Waterius web app authenticates at:
    https://account.waterius.ru/dj-rest-auth/login/

    A successful response returns JSON like:
    {"key": "<api token>"}

    The token is then used for API calls as:
    Authorization: Token <api token>

    The `url` argument is kept for backward compatibility with older calls,
    but token extraction is based on the login endpoint discovered from the
    Waterius frontend request.
    """
    from urllib.parse import urljoin

    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        raise WateriusApiError("Username and password are required")

    base_url = urljoin(url, "/")
    login_page_url = urljoin(base_url, "login")
    login_api_url = urljoin(base_url, "dj-rest-auth/login/")

    async def read_response(resp: aiohttp.ClientResponse) -> Any:
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ct:
            try:
                return await resp.json()
            except Exception:
                return await resp.text()
        return await resp.text()

    try:
        async with session.get(
            login_page_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": base_url,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ):
            pass

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": base_url.rstrip("/"),
            "Referer": login_page_url,
        }

        try:
            cookies = session.cookie_jar.filter_cookies(base_url)
            csrf_cookie = cookies.get("csrftoken")
            if csrf_cookie and csrf_cookie.value:
                headers["X-CSRFToken"] = csrf_cookie.value
        except Exception:
            pass

        payload = {
            "email": username,
            "password": password,
            "brand": "waterius",
        }

        async with session.post(
            login_api_url,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await read_response(resp)
            if resp.status < 200 or resp.status >= 300:
                raise WateriusApiError(
                    f"HTTP {resp.status} while logging in to Waterius. Body: {str(data)[:1000]}"
                )

            token = _extract_token_from_response(data)
            if token:
                return token

            raise WateriusApiError(
                f"Token key not found in Waterius login response. Body: {str(data)[:1000]}"
            )

    except asyncio.TimeoutError as e:
        raise WateriusApiError("Timeout while logging in to Waterius") from e
    except aiohttp.ClientError as e:
        raise WateriusApiError(f"Network error while logging in to Waterius: {e}") from e


class WateriusApi:
    """Async client for account.waterius.ru API."""

    def __init__(self, session: aiohttp.ClientSession, token: str, auth_scheme: str = "Token") -> None:
        self._session = session
        self._token = token
        self._auth_scheme = auth_scheme

    def _headers(self, auth_scheme: str | None = None) -> Dict[str, str]:
        scheme = auth_scheme or self._auth_scheme
        return {
            "Authorization": f"{scheme} {self._token}",
            "Accept": "application/json",
        }

    async def _request_json_once(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        auth_scheme: str | None = None,
    ) -> tuple[int, Any, str]:
        async with self._session.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=self._headers(auth_scheme),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 204:
                return resp.status, None, ""

            ct = (resp.headers.get("Content-Type") or "").lower()
            if "application/json" in ct:
                try:
                    data = await resp.json()
                    return resp.status, data, ""
                except Exception:
                    text = await resp.text()
                    return resp.status, text, text

            text = await resp.text()
            return resp.status, text, text

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
            status, data, text = await self._request_json_once(
                method,
                url,
                params=params,
                json_body=json_body,
                timeout=timeout,
            )

            # Compatibility fallback: the Waterius API uses "Token" in its examples,
            # but retry once with "Bearer" if a future endpoint expects it.
            if status in (401, 403) and self._auth_scheme == "Token":
                status, data, text = await self._request_json_once(
                    method,
                    url,
                    params=params,
                    json_body=json_body,
                    timeout=timeout,
                    auth_scheme="Bearer",
                )
                if 200 <= status < 300:
                    self._auth_scheme = "Bearer"

            if status < 200 or status >= 300:
                body = str(text or data)[:2000]
                raise WateriusApiError(f"HTTP {status} for {url}. Body: {body}")

            return data
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
