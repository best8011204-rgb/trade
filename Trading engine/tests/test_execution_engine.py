from __future__ import annotations

from TradingBot.core.event_bus import EventBus
from TradingBot.exchange.models import OrderResult, OrderStatus, SymbolRules
from TradingBot.execution.execution_engine import ExecutionEngine
from TradingBot.execution.order_manager import OrderManager
from TradingBot.execution.position_manager import PositionManager
from TradingBot.risk.risk_manager import RiskManager
from TradingBot.strategy.strategy_base import Signal, SignalAction


class FakeExchange:
    async def get_symbol_rules(self, symbol: str) -> SymbolRules:
        return SymbolRules(symbol, "0.10", "0.001", 0.001, 2, 3)

    async def submit_order(self, request):
        return OrderResult("1", request.client_order_id or "client", OrderStatus.FILLED, request.quantity, 100.0, {})


class FakeAccountManager:
    async def sync(self):
        return None


async def test_execution_engine_ignores_hold_signal() -> None:
    order_manager = OrderManager(FakeExchange())
    risk = RiskManager(1.0, 3, 1, order_manager)
    engine = ExecutionEngine(
        EventBus(),
        FakeExchange(),
        FakeAccountManager(),
        PositionManager(),
        order_manager,
        risk,
    )

    await engine.execute_signal(Signal("BTCUSDT", SignalAction.HOLD, 0))

    assert await order_manager.get("client") is None

