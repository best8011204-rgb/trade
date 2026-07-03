from __future__ import annotations

import json

from TradingBot.core.events import Event
from TradingBot.database.sqlite import SQLiteDatabase
from TradingBot.exchange.models import OrderResult
from TradingBot.execution.order_manager import ManagedOrder


class EventRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def save(self, event: Event) -> None:
        with self._database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?)",
                (
                    event.id,
                    event.type.value,
                    json.dumps(event.payload, default=str),
                    event.created_at.isoformat(),
                ),
            )


class OrderRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def save_order(self, order: ManagedOrder, result: OrderResult | None = None) -> None:
        request = order.request
        client_order_id = (result.client_order_id if result else request.client_order_id) or ""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO orders
                (client_order_id, exchange_order_id, symbol, side, order_type, quantity, price, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    result.exchange_order_id if result else None,
                    request.symbol,
                    request.side.value,
                    request.order_type.value,
                    request.quantity,
                    request.price,
                    order.status.value,
                    order.created_at.isoformat(),
                ),
            )

