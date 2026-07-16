"""Setup B — 트랩드롱 플러시 숏 (Trapped-Long Flush Short)

명세서 2장의 T1~T3 트리거와 2트랜치 실행 규칙을 구현한 상태 머신.
박스(4h 레인지) 상단 돌파 시도 -> OI 유입 확인 -> 트랩 확정 -> 연료(미청산 OI)
확인 순서로 진행하며, 확정 시 `pending_signal`에 1차 진입 정보를 채운다.
"""

from dataclasses import dataclass
from .data_types import Candle, OIPoint


@dataclass
class CascadeBParams:
    oi_increase_pct: float = 0.006     # T1: 돌파 구간 OI 증가율 (구 0.015 — 발동 빈도 완화)
    retest_window_s: float = 30 * 60   # T2: 상단 재탈환 시도 허용 시간 (구 15분)
    oi_return_tolerance: float = 0.002 # T3: '돌파 전 수준으로 회귀' 판정 오차
    sl_buffer_pct: float = 0.001       # 손절 = sweep high + 0.1%
    tp1_fraction: float = 0.5          # 상단 복귀 확인 시 1차 진입 비율
    time_exit_s: float = 8 * 3600      # 시간 청산 (8시간)
    lev_10x_liq_pct: float = 0.09      # 10x 청산가 근사 (평균단가 대비 -9%)
    lev_25x_liq_pct: float = 0.038     # 25x 청산가 근사 (평균단가 대비 -3.8%)


class TrappedLongFlushShort:
    """T1~T3 감지 + 2트랜치 진입 관리."""

    def __init__(self, params: CascadeBParams = None, box_lookback_s: float = 4 * 3600):
        self.p = params or CascadeBParams()
        self.box_lookback_s = box_lookback_s

        self.state = "IDLE"  # IDLE -> BREAKOUT -> TRAP_CONFIRMED -> ARMED
        self.trap_id = 0

        self.box_high = None
        self.breakout_ts = None
        self.breakout_price = None
        self.oi_at_breakout = None
        self.oi_peak = None
        self.sweep_high = None
        self.reclaim_fail_deadline = None

        self.pending_signal = None
        self.pending_tranche2 = None
        self.log = []

    def update_box(self, box_high: float):
        self.box_high = box_high

    def on_oi(self, pt: OIPoint):
        if self.state in ("BREAKOUT", "TRAP_CONFIRMED"):
            if self.oi_peak is None or pt.oi > self.oi_peak:
                self.oi_peak = pt.oi
            # 무효화: 진입 전 OI 급감 = 트랩 롱 자발 탈출
            if self.state == "BREAKOUT" and self.oi_at_breakout:
                drawdown = (self.oi_peak - pt.oi) / self.oi_at_breakout
                if drawdown > self.p.oi_increase_pct:
                    self.log.append((pt.ts, "무효화: 진입 전 OI 급감 (자발 탈출)"))
                    self._reset()
                    return
            # T3 연료 확인: 트랩 확정 후 OI가 돌파 전 수준으로 복귀했는지
            if self.state == "TRAP_CONFIRMED" and self.oi_at_breakout:
                base = self.oi_at_breakout
                if pt.oi <= base * (1 + self.p.oi_return_tolerance):
                    self.log.append((pt.ts, "연료 소진: OI가 돌파 전 수준으로 회귀 -> 셋업 무효"))
                    self._reset()

    def on_candle(self, c: Candle, oi_now: float = None):
        if self.box_high is None:
            return

        if self.state == "IDLE":
            if c.high > self.box_high:
                self.trap_id += 1
                self.state = "BREAKOUT"
                self.breakout_ts = c.ts
                self.breakout_price = c.close
                self.sweep_high = c.high
                self.oi_at_breakout = oi_now
                self.oi_peak = oi_now
                self.reclaim_fail_deadline = c.ts + self.p.retest_window_s
                self.log.append((c.ts, f"돌파 시도 감지 (id={self.trap_id}) high={c.high:.1f}"))
            return

        if self.state == "BREAKOUT":
            if c.high > self.sweep_high:
                self.sweep_high = c.high  # 돌파 고점 갱신

            # T1: OI 증가 확인
            oi_confirmed = (
                self.oi_at_breakout
                and oi_now is not None
                and (oi_now - self.oi_at_breakout) / self.oi_at_breakout >= self.p.oi_increase_pct
            )

            if c.close < self.box_high:
                # 박스 상단 아래로 복귀
                if not oi_confirmed:
                    self.log.append((c.ts, "OI 증가 미확인 -> 숏커버 랠리로 분류, 셋업 아님"))
                    self._reset()
                    return
                self.state = "TRAP_CONFIRMED"
                self.log.append((c.ts, f"T2 트랩 확정 (박스상단 하회, OI+{(oi_now-self.oi_at_breakout)/self.oi_at_breakout*100:.2f}%)"))
                self.pending_signal = {
                    "ts": c.ts,
                    "price": c.close,
                    "sweep_high": self.sweep_high,
                    "box_high": self.box_high,
                    "tag": f"B-{self.trap_id}",
                    "tranche": 1,
                }
            elif c.ts > self.reclaim_fail_deadline:
                # 15분 내 재탈환도 이탈복귀도 없이 시간 초과 -> 계속 관찰 연장은 하지 않고 리셋
                self.log.append((c.ts, "재탈환 시도 시간초과, 관찰 종료"))
                self._reset()
            return

        if self.state == "TRAP_CONFIRMED":
            # 2트랜치: 15분 내 상단 재탈환 실패 시 2차 진입
            if c.ts <= self.reclaim_fail_deadline:
                if c.close > self.box_high:
                    # 상단 재탈환 성공 -> 트랩 무효, 2차 진입 없음. 이미 진입한 1트랜치는 SL로 관리됨
                    self.log.append((c.ts, "상단 재탈환 성공 -> 2차 진입 취소"))
                    self._reset()
                return
            # 재탈환 실패 확정
            self.pending_tranche2 = {
                "ts": c.ts,
                "price": c.close,
                "sweep_high": self.sweep_high,
                "box_high": self.box_high,
                "tag": f"B-{self.trap_id}",
                "tranche": 2,
            }
            self.log.append((c.ts, "T2 리테스트 실패 확정 -> 2차 진입 신호"))
            self._reset()

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def consume_tranche2(self):
        sig = self.pending_tranche2
        self.pending_tranche2 = None
        return sig

    def _reset(self):
        self.state = "IDLE"
        self.breakout_ts = None
        self.breakout_price = None
        self.oi_at_breakout = None
        self.oi_peak = None
        self.sweep_high = None
        self.reclaim_fail_deadline = None

    @staticmethod
    def compute_exits(entry_avg_price: float, sweep_high: float, box_high: float, box_low: float, p: CascadeBParams):
        sl = sweep_high * (1 + p.sl_buffer_pct)
        tp1 = (box_high + box_low) / 2  # 박스 중앙
        liq_10x = entry_avg_price * (1 - p.lev_10x_liq_pct)
        liq_25x = entry_avg_price * (1 - p.lev_25x_liq_pct)
        # "가까운 쪽" = 진입가 대비 더 가까운(작은 하락폭) 청산가
        tp2 = max(liq_10x, liq_25x)
        return sl, tp1, tp2
