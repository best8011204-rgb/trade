from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    tick_size: str
    lot_size: str
    min_quantity: float
    price_precision: int
    quantity_precision: int


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    last_price: float
    bid: float
    ask: float
    spread: float
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    reduce_only: bool = False
    post_only: bool = False
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    exchange_order_id: str
    client_order_id: str
    status: OrderStatus
    filled_quantity: float
    average_price: float | None
    raw: dict[str, object]
