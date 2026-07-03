from __future__ import annotations

import pytest

from TradingBot.exchange.models import OrderRequest, OrderSide, OrderType, SymbolRules
from TradingBot.execution.order_manager import OrderManager
from TradingBot.risk.risk_manager import RiskManager
from TradingBot.utils.exceptions import RiskRejectedError


async def test_risk_manager_rejects_oversized_quantity() -> None:
    order_manager = OrderManager()
    risk = RiskManager(0.01, 3, 1, order_manager)
    rules = SymbolRules("BTCUSDT", "0.10", "0.001", 0.001, 2, 3)
    request = OrderRequest("BTCUSDT", OrderSide.BUY, OrderType.MARKET, 1.0)

    with pytest.raises(RiskRejectedError):
        await risk.validate(request, rules)

