"""Setup A — 청산 캐스케이드 소진 롱 (Cascade Exhaustion Long)

명세서 1장의 트리거(T1~T4)·실행 규칙을 순서대로 구현한 이벤트 기반 상태 머신.
스트리밍 데이터(ForceOrder, Candle)를 시간순으로 하나씩 `on_force_order` /
`on_candle` 에 흘려 넣으면 내부적으로 캐스케이드를 추적하고, 소진 확인 시
`pending_signal` 에 진입 신호를 채운다.
"""

from dataclasses import dataclass, field
from collections import deque
from .data_types import ForceOrder, Candle, Trade, Side


@dataclass
class CascadeAParams:
    lookback_hours: float = 24.0        # 기준 시간당 평균 산정 구간
    vol_multiplier: float = 8.0          # T1: 60s 합계 >= 시간당 평균 * N
    min_chain: int = 3                   # T1: 최소 연쇄 건수
    cascade_window_s: float = 60.0       # T1: 감지 윈도우
    min_move_pct: float = 0.008          # T2: 캐스케이드 시작 대비 하락률
    exhaustion_gap_s: float = 90.0       # T3: 마지막 청산 후 무청산 경과
    cvd_window_s: float = 60.0           # T3: 1분 CVD
    rebound_pct: float = 0.0015          # T3: 저점 대비 반등폭
    rebound_hold_s: float = 30.0         # T3: 반등 유지 시간
    sl_buffer_pct: float = 0.0015        # 손절 = 저점 - 버퍼
    tp1_retrace: float = 0.38            # 되돌림 38%
    tp2_retrace: float = 0.618           # 되돌림 61.8%
    tp1_fraction: float = 0.5            # TP1에서 청산할 비율
    time_exit_s: float = 90 * 60         # 시간 청산 (90분)
    max_reentries: int = 1               # 동일 캐스케이드 재진입 허용 횟수


class CascadeExhaustionLong:
    """T1~T4 감지 + 포지션 관리 상태 머신."""

    def __init__(self, params: CascadeAParams = None, macro_blackouts=None):
        self.p = params or CascadeAParams()
        # macro_blackouts: [(start_ts, end_ts), ...]  FOMC/CPI ±30분
        self.macro_blackouts = macro_blackouts or []

        self._hourly_baseline = None       # 시간당 평균 청산 금액(외부 주입)
        self._sell_liqs = deque()          # (ts, notional) 최근 청산 로그
        self._cvd_buf = deque()            # (ts, delta) 1분 CVD 롤링

        self.state = "IDLE"                # IDLE -> CASCADE -> WATCH_EXHAUST -> ARMED
        self.cascade_start_ts = None
        self.cascade_start_price = None
        self.cascade_low = None
        self.cascade_low_ts = None
        self.last_liq_ts = None
        self.rebound_since_ts = None
        self.reentry_count = 0
        self.cascade_id = 0

        self.pending_signal = None         # {"ts", "price", "tag"} 진입 신호
        self.log = []

    def set_hourly_baseline(self, notional_per_hour: float):
        self._hourly_baseline = notional_per_hour

    def _in_macro_blackout(self, ts: float) -> bool:
        return any(s <= ts <= e for s, e in self.macro_blackouts)

    def _prune(self, ts: float):
        while self._sell_liqs and ts - self._sell_liqs[0][0] > self.p.cascade_window_s:
            self._sell_liqs.popleft()
        while self._cvd_buf and ts - self._cvd_buf[0][0] > self.p.cvd_window_s:
            self._cvd_buf.popleft()

    def on_cvd_delta(self, ts: float, delta: float):
        self._cvd_buf.append((ts, delta))
        self._prune(ts)

    def _cvd_1m(self) -> float:
        return sum(d for _, d in self._cvd_buf)

    def on_force_order(self, fo: ForceOrder):
        if fo.side != "SELL":
            return  # 롱 청산(SELL)만 하방 캐스케이드 후보
        self._prune(fo.ts)
        self._sell_liqs.append((fo.ts, fo.notional))
        self.last_liq_ts = fo.ts

        window_sum = sum(n for _, n in self._sell_liqs)
        window_count = len(self._sell_liqs)

        if self.state == "IDLE":
            if (
                self._hourly_baseline
                and window_sum >= self._hourly_baseline * self.p.vol_multiplier
                and window_count >= self.p.min_chain
            ):
                self.cascade_id += 1
                self.state = "CASCADE"
                self.cascade_start_ts = fo.ts
                self.cascade_start_price = fo.price
                self.cascade_low = fo.price
                self.cascade_low_ts = fo.ts
                self.reentry_count = 0
                self.log.append((fo.ts, f"T1 캐스케이드 감지 (id={self.cascade_id}, sum={window_sum:.0f})"))
        elif self.state in ("CASCADE", "WATCH_EXHAUST"):
            if fo.price < self.cascade_low:
                self.cascade_low = fo.price
                self.cascade_low_ts = fo.ts
            # 신규 청산 발생 -> 소진 관찰 리셋
            self.state = "CASCADE"
            self.rebound_since_ts = None

            move = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
            if move >= self.p.min_move_pct:
                self.state = "WATCH_EXHAUST"
                self.log.append((fo.ts, f"T2 가격이탈 확인 move={move*100:.2f}%"))

    def on_candle(self, c: Candle):
        """가격/CVD 스트림 tick. T2 재평가(캔들 저가 기준)와 T3 소진 판정,
        열린 포지션 관리가 여기서 이뤄진다."""
        if self.state in ("CASCADE", "WATCH_EXHAUST"):
            # forceOrder는 1000ms당 대표 1건만 전송되므로(명세 4장 주의),
            # 캔들 저가로도 캐스케이드 저점/T2를 갱신해야 짧은 캐스케이드를 놓치지 않는다.
            if c.low < self.cascade_low:
                self.cascade_low = c.low
                self.cascade_low_ts = c.ts
            move = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
            if self.state == "CASCADE" and move >= self.p.min_move_pct:
                self.state = "WATCH_EXHAUST"
                self.log.append((c.ts, f"T2 가격이탈 확인(캔들) move={move*100:.2f}%"))

        if self.state == "WATCH_EXHAUST" and self.last_liq_ts is not None:
            gap_ok = (c.ts - self.last_liq_ts) >= self.p.exhaustion_gap_s
            cvd_ok = self._cvd_1m() >= 0
            rebound_pct_now = (c.close - self.cascade_low) / self.cascade_low

            if rebound_pct_now >= self.p.rebound_pct:
                if self.rebound_since_ts is None:
                    self.rebound_since_ts = c.ts
            else:
                self.rebound_since_ts = None

            rebound_hold_ok = (
                self.rebound_since_ts is not None
                and (c.ts - self.rebound_since_ts) >= self.p.rebound_hold_s
            )

            if gap_ok and cvd_ok and rebound_hold_ok:
                if self._in_macro_blackout(c.ts):
                    self.log.append((c.ts, "T4 매크로 블랙아웃 - 진입 차단"))
                    self.state = "IDLE"
                    return
                self.pending_signal = {
                    "ts": c.ts,
                    "price": c.close,
                    "cascade_low": self.cascade_low,
                    "cascade_start_price": self.cascade_start_price,
                    "tag": f"A-{self.cascade_id}",
                }
                self.log.append((c.ts, f"T3 소진 확인 -> 진입 신호 @ {c.close:.1f}"))
                self.state = "ARMED"

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def allow_reentry(self) -> bool:
        return self.reentry_count < self.p.max_reentries

    def register_reentry(self):
        self.reentry_count += 1
        self.state = "WATCH_EXHAUST"
        self.rebound_since_ts = None

    def reset(self):
        """탐지 상태만 IDLE로 되돌린다. cascade_low/start_price 등은 재진입
        판단(register_reentry)에 쓰일 수 있으므로 남겨둔다."""
        self.state = "IDLE"
        self.rebound_since_ts = None

    @staticmethod
    def compute_exits(entry_price: float, cascade_low: float, cascade_start_price: float, p: CascadeAParams):
        move = cascade_start_price - cascade_low
        sl = cascade_low * (1 - p.sl_buffer_pct)
        tp1 = cascade_low + p.tp1_retrace * move
        tp2 = cascade_low + p.tp2_retrace * move
        return sl, tp1, tp2
