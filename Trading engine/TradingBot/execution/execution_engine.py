from __future__ import annotations

import logging

from TradingBot.core.event_bus import EventBus
from TradingBot.core.events import Event, EventType
from TradingBot.exchange.binance_client import BinanceClient
from TradingBot.exchange.models import OrderRequest, OrderSide
from TradingBot.execution.account_manager import AccountManager
from TradingBot.execution.order_manager import OrderManager
from TradingBot.execution.position_manager import PositionManager
from TradingBot.risk.risk_manager import RiskManager
from TradingBot.strategy.strategy_base import Signal, SignalAction

LOGGER = logging.getLogger(__name__)


class ExecutionEngine:
    """Converts strategy signals into validated exchange orders."""

    def __init__(
        self,
        event_bus: EventBus,
        exchange: BinanceClient,
        account_manager: AccountManager,
        position_manager: PositionManager,
        order_manager: OrderManager,
        risk_manager: RiskManager,
    ) -> None:
        self._event_bus = event_bus
        self._exchange = exchange
        self._account_manager = account_manager
        self._position_manager = position_manager
        self._order_manager = order_manager
        self._risk_manager = risk_manager

    async def start(self) -> None:
        await self._event_bus.subscribe(EventType.STRATEGY_SIGNAL, self._on_signal_event)

    async def stop(self) -> None:
        await self._event_bus.unsubscribe(EventType.STRATEGY_SIGNAL, self._on_signal_event)

    async def execute_signal(self, signal: Signal) -> None:
        if signal.action == SignalAction.HOLD:
            return
        await self._account_manager.sync()
        await self._position_manager.sync()
        side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
        request = OrderRequest(
            symbol=signal.symbol,
            side=side,
            order_type=signal.order_type,
            quantity=signal.quantity,
            price=signal.price,
            stop_price=signal.stop_price,
            reduce_only=signal.reduce_only or signal.action == SignalAction.CLOSE,
            post_only=signal.post_only,
            client_order_id=signal.correlation_id,
        )
        rules = await self._exchange.get_symbol_rules(signal.symbol)
        await self._risk_manager.validate(request, rules)
        order = await self._order_manager.create(request)
        await self._event_bus.publish(Event(EventType.ORDER_CREATED, {"order": order}))
        result = await self._order_manager.submit(order)
        if result.status.value in {"FILLED", "PARTIALLY_FILLED"}:
            await self._position_manager.sync()
            await self._event_bus.publish(Event(EventType.ORDER_FILLED, {"result": result}))

    async def _on_signal_event(self, event: Event) -> None:
        signal = event.payload.get("signal")
        if not isinstance(signal, Signal):
            LOGGER.warning("invalid_signal_event", extra={"event_id": event.id})
            return
        await self.execute_signal(signal)

