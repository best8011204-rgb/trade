from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from TradingBot.exchange.binance_client import BinanceClient
from TradingBot.exchange.models import OrderRequest, OrderResult, OrderStatus


@dataclass(slots=True)
class ManagedOrder:
    request: OrderRequest
    status: OrderStatus = OrderStatus.CREATED
    result: OrderResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class OrderManager:
    """Owns order lifecycle and exchange submission."""

    def __init__(self, exchange: BinanceClient | None = None) -> None:
        self._exchange = exchange
        self._orders: dict[str, ManagedOrder] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: OrderRequest) -> ManagedOrder:
        key = request.client_order_id or f"pending-{len(self._orders) + 1}"
        order = ManagedOrder(request=request)
        async with self._lock:
            self._orders[key] = order
        return order

    async def submit(self, order: ManagedOrder) -> OrderResult:
        if self._exchange is None:
            raise RuntimeError("Exchange client is required to submit live orders.")
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now(UTC)
        result = await self._exchange.submit_order(order.request)
        order.result = result
        order.status = result.status
        order.updated_at = datetime.now(UTC)
        async with self._lock:
            self._orders[result.client_order_id] = order
        return result

    async def get(self, client_order_id: str) -> ManagedOrder | None:
        async with self._lock:
            return self._orders.get(client_order_id)

    async def has_open_duplicate(self, request: OrderRequest) -> bool:
        async with self._lock:
            return any(
                order.request.symbol == request.symbol
                and order.request.side == request.side
                and order.request.quantity == request.quantity
                and order.status
                in {OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}
                for order in self._orders.values()
            )

