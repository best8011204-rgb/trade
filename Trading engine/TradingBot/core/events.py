from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EventType(StrEnum):
    PRICE_UPDATED = "PriceUpdated"
    ORDER_CREATED = "OrderCreated"
    ORDER_FILLED = "OrderFilled"
    POSITION_UPDATED = "PositionUpdated"
    ACCOUNT_UPDATED = "AccountUpdated"
    CONNECTION_LOST = "ConnectionLost"
    CONNECTION_RECOVERED = "ConnectionRecovered"
    STRATEGY_SIGNAL = "StrategySignal"
    ERROR = "Error"


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event published through the central bus."""

    type: EventType
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

