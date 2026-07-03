"""
BTC 청산 흐름 전략 v1.0 — 사전등록 명세 구현체

Setup A: Cascade Exhaustion Long (청산 캐스케이드 소진 롱)
Setup B: Trapped-Long Flush Short (트랩드롱 플러시 숏)

이 패키지는 명세서(liquidationstrategyv1preregistration.md)의 트리거·실행·
기각 조건을 코드로 옮긴 것이다. 실거래 데이터 접속 없이도 검증 절차의
"섀도 단계"(가상 체결) 로직을 합성 데이터 위에서 그대로 재현할 수 있도록
구성했다.
"""

from .data_types import ForceOrder, Candle, OIPoint, Trade
from .setup_a import CascadeExhaustionLong
from .setup_b import TrappedLongFlushShort
from .engine import StrategyEngine

__all__ = [
    "ForceOrder",
    "Candle",
    "OIPoint",
    "Trade",
    "CascadeExhaustionLong",
    "TrappedLongFlushShort",
    "StrategyEngine",
]
