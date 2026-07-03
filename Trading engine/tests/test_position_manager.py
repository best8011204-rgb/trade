from __future__ import annotations

from TradingBot.execution.models import Position, PositionSide
from TradingBot.execution.position_manager import PositionManager


async def test_position_manager_updates_and_clears_flat_position() -> None:
    manager = PositionManager()
    position = Position("BTCUSDT", PositionSide.LONG, 100.0, 1.0, 0.0, 0.0, 0.0, None, 100.0)

    await manager.update(position)
    assert await manager.get("BTCUSDT") == position

    await manager.update(Position("BTCUSDT", PositionSide.FLAT, 0, 0, 0, 0, 0, None, 0))
    assert await manager.get("BTCUSDT") is None

