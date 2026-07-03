"""공통 데이터 구조. 명세서 4장 '필요 데이터'에 대응."""

from dataclasses import dataclass, field
from enum import Enum


class Side(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class ForceOrder:
    """wss://fstream.binance.com/ws/btcusdt@forceOrder 1건.

    side: 청산되는 포지션의 방향. SELL 청산 = 롱 강제청산(하방 압력).
    """
    ts: float          # epoch seconds
    side: str           # "SELL" (롱 청산) | "BUY" (숏 청산)
    price: float
    qty: float

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    cvd_delta: float = 0.0   # 해당 봉의 순매수-순매도 델타 (aggTrade 기반)


@dataclass
class OIPoint:
    ts: float
    oi: float  # open interest, BTC 수량 기준


@dataclass
class Trade:
    setup: str            # "A" or "B"
    side: Side
    entry_ts: float
    entry_price: float
    exit_ts: float = None
    exit_price: float = None
    qty_fraction: float = 1.0   # 트랜치 비율
    reason: str = ""             # "TP1" | "TP2" | "SL" | "TIME"
    r_multiple: float = None
    bps: float = None
    tag: str = ""                # 캐스케이드/트랩 식별용 그룹 id
