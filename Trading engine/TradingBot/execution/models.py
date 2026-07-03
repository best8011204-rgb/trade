from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass(frozen=True, slots=True)
class AccountState:
    wallet_balance: float
    available_balance: float
    margin_balance: float
    unrealized_pnl: float
    realized_pnl: float
    leverage: int
    margin_mode: str


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float
    unrealized_profit: float
    realized_profit: float
    roi: float
    liquidation_price: float | None
    position_value: float

