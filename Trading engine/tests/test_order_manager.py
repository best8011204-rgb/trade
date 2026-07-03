from __future__ import annotations

from TradingBot.exchange.models import OrderRequest, OrderSide, OrderType
from TradingBot.execution.order_manager import OrderManager


async def test_order_manager_detects_duplicate_open_orders() -> None:
    manager = OrderManager()
    request = OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.01)

    await manager.create(request)

    assert await manager.has_open_duplicate(request) is True

