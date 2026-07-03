from __future__ import annotations

from datetime import UTC, datetime

from TradingBot.exchange.models import MarketSnapshot
from TradingBot.exchange.rest import BinanceRestClient


class MarketData:
    """Provides normalized market data objects."""

    def __init__(self, rest: BinanceRestClient) -> None:
        self._rest = rest

    async def snapshot(self, symbol: str) -> MarketSnapshot:
        ticker = await self._rest.request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        price = await self._rest.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
        assert isinstance(ticker, dict)
        assert isinstance(price, dict)
        bid = float(ticker["bidPrice"])
        ask = float(ticker["askPrice"])
        return MarketSnapshot(
            symbol=symbol,
            last_price=float(price["price"]),
            bid=bid,
            ask=ask,
            spread=ask - bid,
            timestamp=datetime.now(UTC),
        )

    async def order_book(self, symbol: str, limit: int = 100) -> dict[str, object]:
        raw = await self._rest.request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        assert isinstance(raw, dict)
        return raw

    async def klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict[str, object]]:
        raw = await self._rest.request(
            "GET", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}
        )
        assert isinstance(raw, list)
        return [{"raw": item} for item in raw]

