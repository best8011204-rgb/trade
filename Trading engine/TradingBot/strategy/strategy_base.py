from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from TradingBot.exchange.models import MarketSnapshot, OrderType


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    action: SignalAction
    quantity: float
    order_type: OrderType = OrderType.MARKET
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    post_only: bool = False
    correlation_id: str | None = None


class StrategyBase(Protocol):
    """Protocol implemented by user strategies."""

    name: str

    async def on_market_update(self, market: MarketSnapshot) -> Signal:
        """Return a standardized signal without using exchange-specific clients."""

