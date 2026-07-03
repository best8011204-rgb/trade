from __future__ import annotations

from TradingBot.exchange.models import MarketSnapshot
from TradingBot.strategy.strategy_base import Signal, SignalAction


class EmptyStrategy:
    """No-op strategy useful for wiring tests and dry startup."""

    name = "empty"

    async def on_market_update(self, market: MarketSnapshot) -> Signal:
        return Signal(symbol=market.symbol, action=SignalAction.HOLD, quantity=0)

