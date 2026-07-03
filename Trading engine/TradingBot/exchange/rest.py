from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from TradingBot.utils.exceptions import ExchangeError


class BinanceRestClient:
    """Minimal Binance USD-M Futures REST client with retries and signing."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool,
        timeout_seconds: float,
        retries: int,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret.encode()
        self._base_url = (
            "https://testnet.binancefuture.com" if testnet else "https://fapi.binance.com"
        )
        self._timeout = timeout_seconds
        self._retries = retries
        self._time_offset_ms = 0
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def sync_time(self) -> None:
        data = await self.request("GET", "/fapi/v1/time")
        server_time = int(data["serverTime"])
        self._time_offset_ms = server_time - int(time.time() * 1000)

    async def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, object] | None = None,
        signed: bool = False,
    ) -> dict[str, object] | list[dict[str, object]]:
        request_params = dict(params or {})
        headers = {"X-MBX-APIKEY": self._api_key} if self._api_key else {}
        if signed:
            request_params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            query = urlencode(request_params)
            request_params["signature"] = hmac.new(
                self._api_secret, query.encode(), hashlib.sha256
            ).hexdigest()

        last_error: Exception | None = None
        for attempt in range(self._retries):
            try:
                response = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=request_params,
                    headers=headers,
                )
                if response.status_code in {418, 429}:
                    await asyncio.sleep(2**attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and "code" in payload and int(payload["code"]) < 0:
                    raise ExchangeError(str(payload))
                return payload
            except (httpx.HTTPError, ExchangeError) as exc:
                last_error = exc
                await asyncio.sleep(2**attempt)
        raise ExchangeError(f"Binance REST request failed: {last_error}")

