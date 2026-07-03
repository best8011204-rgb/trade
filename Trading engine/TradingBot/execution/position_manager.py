from __future__ import annotations

import asyncio

from TradingBot.exchange.account_data import AccountData
from TradingBot.execution.models import Position, PositionSide


class PositionManager:
    """Single source of truth for live positions."""

    def __init__(self, account_data: AccountData | None = None) -> None:
        self._account_data = account_data
        self._positions: dict[str, Position] = {}
        self._lock = asyncio.Lock()

    async def sync(self) -> dict[str, Position]:
        if self._account_data is None:
            return await self.all()
        raw_positions = await self._account_data.positions()
        positions = {
            item["symbol"]: self._from_exchange(item)
            for item in raw_positions
            if float(item.get("positionAmt", 0)) != 0
        }
        async with self._lock:
            self._positions = positions
        return positions

    async def update(self, position: Position) -> None:
        async with self._lock:
            if position.side == PositionSide.FLAT or position.quantity == 0:
                self._positions.pop(position.symbol, None)
            else:
                self._positions[position.symbol] = position

    async def get(self, symbol: str) -> Position | None:
        async with self._lock:
            return self._positions.get(symbol)

    async def all(self) -> dict[str, Position]:
        async with self._lock:
            return dict(self._positions)

    def _from_exchange(self, item: dict[str, object]) -> Position:
        quantity = float(item.get("positionAmt", 0))
        entry_price = float(item.get("entryPrice", 0))
        unrealized = float(item.get("unRealizedProfit", 0))
        value = abs(quantity * float(item.get("markPrice", entry_price)))
        side = PositionSide.LONG if quantity > 0 else PositionSide.SHORT if quantity < 0 else PositionSide.FLAT
        roi = unrealized / value if value else 0.0
        return Position(
            symbol=str(item["symbol"]),
            side=side,
            entry_price=entry_price,
            quantity=abs(quantity),
            unrealized_profit=unrealized,
            realized_profit=0.0,
            roi=roi,
            liquidation_price=float(item["liquidationPrice"]) if item.get("liquidationPrice") else None,
            position_value=value,
        )

