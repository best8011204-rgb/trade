from __future__ import annotations

from TradingBot.exchange.rest import BinanceRestClient


class AccountData:
    """Reads account and position state from Binance."""

    def __init__(self, rest: BinanceRestClient) -> None:
        self._rest = rest

    async def account(self) -> dict[str, object]:
        raw = await self._rest.request("GET", "/fapi/v2/account", signed=True)
        assert isinstance(raw, dict)
        return raw

    async def positions(self) -> list[dict[str, object]]:
        raw = await self._rest.request("GET", "/fapi/v2/positionRisk", signed=True)
        assert isinstance(raw, list)
        return raw

