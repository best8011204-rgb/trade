"""Setup B — 트랩 플러시 (양방향: 박스 상단 돌파/트랩드롱 플러시 숏 + 박스 하단
돌파/트랩드숏 플러시 롱)

명세서 2장의 T1~T3 트리거와 2트랜치 실행 규칙을 구현한 상태 머신.
박스(레인지, 기본 2시간 — CascadeBParams.box_window_min) 상단 또는 하단
돌파 시도 -> OI 유입 확인 -> 트랩 확정 -> 연료(미청산 OI) 확인 순서로
진행하며, 확정 시 `pending_signal`에 1차 진입 정보를 채운다.

[양방향 확장] 상단 돌파(신규 롱 유입 함정 -> 숏 진입)와 하단 돌파(신규 숏
유입 함정 -> 롱 진입)를 breakout_side("up"|"down")로 구분해 같은 상태머신이
동시에 감시한다. T1(OI 증가 확인)은 방향과 무관하게 동일한 계산식을 쓴다 —
신규 포지션이 들어오면 어느 방향이든 OI가 늘어나기 때문이다.
"""

from dataclasses import dataclass
from .data_types import Candle, OIPoint


@dataclass
class CascadeBParams:
    box_window_min: int = 120          # 박스(레인지) 계산 창(분). 구 4시간(240분)에서 축소
                                        # — 짧을수록 상단-하단 폭이 좁아져 돌파가 더 쉽게 잡힌다.
                                        # live_feed.py 등 박스를 실제로 계산하는 쪽에서 이 값을 읽는다.
    oi_increase_pct: float = 0.006     # T1: 돌파 구간 OI 증가율 (양방향 공용)
    retest_window_s: float = 30 * 60   # T2: 재탈환 시도 허용 시간
    oi_return_tolerance: float = 0.002 # T3: '돌파 전 수준으로 회귀' 판정 오차
    sl_buffer_pct: float = 0.001       # 손절 = 스윕 극값 ± 버퍼
    tp1_fraction: float = 0.5          # 재하회/재상회 확인 시 1차 진입 비율
    time_exit_s: float = 8 * 3600      # 시간 청산 (8시간)
    lev_10x_liq_pct: float = 0.09      # 10x 청산가 근사 (평균단가 대비 ±9%)
    lev_25x_liq_pct: float = 0.038     # 25x 청산가 근사 (평균단가 대비 ±3.8%)


class TrappedLongFlushShort:
    """T1~T3 감지 + 2트랜치 진입 관리 (양방향)."""

    def __init__(self, params: CascadeBParams = None):
        self.p = params or CascadeBParams()

        self.state = "IDLE"  # IDLE -> BREAKOUT -> TRAP_CONFIRMED -> ARMED
        self.trap_id = 0

        self.box_high = None
        self.box_low = None
        self.breakout_side = None  # "up"(상단 돌파 -> 숏 준비) | "down"(하단 돌파 -> 롱 준비)
        self.breakout_ts = None
        self.breakout_price = None
        self.oi_at_breakout = None
        self.oi_peak = None
        self.sweep_extreme = None  # up: 돌파 고점, down: 돌파 저점
        self.reclaim_fail_deadline = None

        self.pending_signal = None
        self.pending_tranche2 = None
        self.log = []

        self.last_oi = None  # 가장 최근 관측 OI (조건 체크리스트 표시용)

    def update_box(self, box_low: float, box_high: float):
        self.box_low = box_low
        self.box_high = box_high

    def on_oi(self, pt: OIPoint):
        self.last_oi = pt.oi
        if self.state in ("BREAKOUT", "TRAP_CONFIRMED"):
            if self.oi_peak is None or pt.oi > self.oi_peak:
                self.oi_peak = pt.oi
            # 무효화: 진입 전 OI 급감 = 트랩 포지션 자발 탈출
            if self.state == "BREAKOUT" and self.oi_at_breakout:
                drawdown = (self.oi_peak - pt.oi) / self.oi_at_breakout
                if drawdown > self.p.oi_increase_pct:
                    label = "롱" if self.breakout_side == "up" else "숏"
                    self.log.append((pt.ts, f"무효화: 진입 전 OI 급감 (트랩 {label} 자발 탈출)"))
                    self._reset()
                    return
            # T3 연료 확인: 트랩 확정 후 OI가 돌파 전 수준으로 복귀했는지
            if self.state == "TRAP_CONFIRMED" and self.oi_at_breakout:
                base = self.oi_at_breakout
                if pt.oi <= base * (1 + self.p.oi_return_tolerance):
                    self.log.append((pt.ts, "연료 소진: OI가 돌파 전 수준으로 회귀 -> 셋업 무효"))
                    self._reset()

    def on_candle(self, c: Candle, oi_now: float = None):
        if self.box_high is None or self.box_low is None:
            return

        if self.state == "IDLE":
            if c.high > self.box_high:
                self._start_breakout(c, oi_now, "up")
            elif c.low < self.box_low:
                self._start_breakout(c, oi_now, "down")
            return

        up = self.breakout_side == "up"

        if self.state == "BREAKOUT":
            if up:
                if c.high > self.sweep_extreme:
                    self.sweep_extreme = c.high
            else:
                if c.low < self.sweep_extreme:
                    self.sweep_extreme = c.low

            # T1: OI 증가 확인(방향 무관 — 신규 포지션 유입은 항상 OI 증가로 나타남)
            oi_confirmed = (
                self.oi_at_breakout
                and oi_now is not None
                and (oi_now - self.oi_at_breakout) / self.oi_at_breakout >= self.p.oi_increase_pct
            )

            reclaimed = (c.close < self.box_high) if up else (c.close > self.box_low)
            if reclaimed:
                fade_label = "숏커버" if up else "롱커버"
                boundary = "박스상단 하회" if up else "박스하단 상회"
                if not oi_confirmed:
                    self.log.append((c.ts, f"OI 증가 미확인 -> {fade_label} 랠리로 분류, 셋업 아님"))
                    self._reset()
                    return
                self.state = "TRAP_CONFIRMED"
                entry_label = "숏" if up else "롱"
                self.log.append((c.ts, f"T2 트랩 확정({boundary}, OI+{(oi_now-self.oi_at_breakout)/self.oi_at_breakout*100:.2f}%) "
                                        f"-> {entry_label} 진입 예정"))
                self.pending_signal = {
                    "ts": c.ts,
                    "price": c.close,
                    "side": "short" if up else "long",
                    "sweep_extreme": self.sweep_extreme,
                    "box_high": self.box_high,
                    "box_low": self.box_low,
                    "tag": f"B-{self.trap_id}",
                    "tranche": 1,
                }
            elif c.ts > self.reclaim_fail_deadline:
                # 재탈환도 이탈복귀도 없이 시간 초과 -> 계속 관찰 연장은 하지 않고 리셋
                self.log.append((c.ts, "재탈환 시도 시간초과, 관찰 종료"))
                self._reset()
            return

        if self.state == "TRAP_CONFIRMED":
            # 2트랜치: retest_window_s 내 반대편 재탈환 실패 시 2차 진입
            if c.ts <= self.reclaim_fail_deadline:
                reclaim_success = (c.close > self.box_high) if up else (c.close < self.box_low)
                if reclaim_success:
                    boundary = "상단" if up else "하단"
                    self.log.append((c.ts, f"{boundary} 재탈환 성공 -> 2차 진입 취소"))
                    self._reset()
                return
            # 재탈환 실패 확정
            self.pending_tranche2 = {
                "ts": c.ts,
                "price": c.close,
                "side": "short" if up else "long",
                "sweep_extreme": self.sweep_extreme,
                "box_high": self.box_high,
                "box_low": self.box_low,
                "tag": f"B-{self.trap_id}",
                "tranche": 2,
            }
            self.log.append((c.ts, "T2 리테스트 실패 확정 -> 2차 진입 신호"))
            self._reset()

    def _start_breakout(self, c: Candle, oi_now, side: str):
        self.trap_id += 1
        self.state = "BREAKOUT"
        self.breakout_side = side
        self.breakout_ts = c.ts
        self.breakout_price = c.close
        self.sweep_extreme = c.high if side == "up" else c.low
        self.oi_at_breakout = oi_now
        self.oi_peak = oi_now
        self.reclaim_fail_deadline = c.ts + self.p.retest_window_s
        boundary = "상단" if side == "up" else "하단"
        self.log.append((c.ts, f"박스 {boundary} 돌파 시도 감지 (id={self.trap_id}) {self.sweep_extreme:.1f}"))

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def describe(self, current_price: float = None):
        """현재 상태를 사람이 읽을 텍스트로 설명 (GUI/텔레그램 표시용).

        current_price: 제공하면 BREAKOUT 상태의 돌파 극값 표시가 확정봉 사이에도
        라이브로 갱신된다(체결 틱이 새 극값을 만들면 즉시 반영).

        반환: (설명 텍스트, 참고 가격 또는 None).
        """
        p = self.p
        if self.state == "IDLE":
            bh = f"{self.box_high:,.1f}" if self.box_high is not None else "-"
            bl = f"{self.box_low:,.1f}" if self.box_low is not None else "-"
            return (
                f"박스 상단({bh})/하단({bl}) 돌파 감시 중(양방향) — 돌파 시 OI +{p.oi_increase_pct*100:.1f}% "
                f"이상 유입 확인되면 트랩(반대매매 유입)으로 판단",
                None,
            )
        up = self.breakout_side == "up"
        if self.state == "BREAKOUT":
            if current_price is not None:
                eff = max(self.sweep_extreme, current_price) if up else min(self.sweep_extreme, current_price)
            else:
                eff = self.sweep_extreme
            boundary = "상단" if up else "하단"
            fade_label = "숏커버" if up else "롱커버"
            entry_label = "숏" if up else "롱"
            extreme_word = "고점" if up else "저점"
            return (
                f"{boundary} 돌파 감지({extreme_word} {eff:,.1f}) — OI 유입 확인 중. 박스 {boundary} "
                f"{'아래로' if up else '위로'} 복귀하면 트랩 확정({entry_label} 진입), 재탈환 성공 시 {fade_label}로 분류해 취소",
                eff,
            )
        if self.state == "TRAP_CONFIRMED":
            boundary = "상단" if up else "하단"
            entry_label = "숏" if up else "롱"
            extreme_word = "고점" if up else "저점"
            return (
                f"트랩 확정({entry_label} 진입, {extreme_word} {self.sweep_extreme:,.1f}) — "
                f"{p.retest_window_s/60:.0f}분 내 {boundary} 재탈환 실패 시 2차 진입, 재탈환 성공 시 취소, "
                "OI가 돌파 전 수준으로 회귀하면 연료소진으로 취소",
                self.sweep_extreme,
            )
        return self.state, None

    def conditions(self, now_ts: float, current_price: float = None):
        """T1~T3 하위 조건 각각의 실시간 충족 여부 (GUI/텔레그램 체크리스트 표시용).

        current_price: 제공하면 IDLE의 "상/하단까지 거리"/BREAKOUT의 돌파 극값이
        확정봉 사이에도 매초 라이브로 갱신된다. OI 관련 조건(T1_oi/T3)은 OI
        자체가 5분 단위 데이터라 틱 단위로 세분화할 수 없어 항상 마지막 확정치를 쓴다.

        상태 단계와 무관하게 항상 전부 계산한다. 반환: [{"key","label","met","detail"}, ...] (4개 고정).
        """
        p = self.p
        out = []

        breakout_happened = self.sweep_extreme is not None
        up = self.breakout_side == "up" if breakout_happened else None
        if breakout_happened:
            eff = (max(self.sweep_extreme, current_price) if up else min(self.sweep_extreme, current_price)) \
                if current_price is not None else self.sweep_extreme
            boundary = "상단" if up else "하단"
            detail = f"{boundary} 돌파 {'고점' if up else '저점'} {eff:,.1f}"
        elif self.box_high is not None and self.box_low is not None:
            if current_price is not None:
                gap_hi = (current_price - self.box_high) / self.box_high * 100
                gap_lo = (current_price - self.box_low) / self.box_low * 100
                detail = f"상단 {self.box_high:,.1f}({gap_hi:+.2f}%) / 하단 {self.box_low:,.1f}({gap_lo:+.2f}%)"
            else:
                detail = f"상단 {self.box_high:,.1f} / 하단 {self.box_low:,.1f} 대기"
        else:
            detail = "박스 미형성"
        out.append({
            "key": "t1_breakout",
            "label": "T1: 박스 상/하단 돌파(양방향)",
            "met": breakout_happened,
            "detail": detail,
        })

        if self.oi_at_breakout and self.oi_peak is not None:
            oi_incr = (self.oi_peak - self.oi_at_breakout) / self.oi_at_breakout
        else:
            oi_incr = 0.0
        out.append({
            "key": "t1_oi",
            "label": f"T1: OI 증가 ≥ {p.oi_increase_pct*100:.1f}%",
            "met": self.oi_at_breakout is not None and oi_incr >= p.oi_increase_pct,
            "detail": f"{oi_incr*100:.2f}%" if self.oi_at_breakout is not None else "돌파 전",
        })

        out.append({
            "key": "t2_trap",
            "label": "T2: 박스 반대편 재진입(트랩 확정)",
            "met": self.state == "TRAP_CONFIRMED",
            "detail": "확정됨" if self.state == "TRAP_CONFIRMED" else "미확정",
        })

        if self.oi_at_breakout is not None and self.last_oi is not None:
            fuel_met = self.last_oi > self.oi_at_breakout * (1 + p.oi_return_tolerance)
            detail = f"OI {self.last_oi:,.0f} (기준 {self.oi_at_breakout:,.0f})"
        else:
            fuel_met = False
            detail = "관찰 전"
        out.append({
            "key": "t3_fuel",
            "label": "T3: 연료 유지(OI 미회귀)",
            "met": fuel_met,
            "detail": detail,
        })
        return out

    def consume_tranche2(self):
        sig = self.pending_tranche2
        self.pending_tranche2 = None
        return sig

    def _reset(self):
        self.state = "IDLE"
        self.breakout_side = None
        self.breakout_ts = None
        self.breakout_price = None
        self.oi_at_breakout = None
        self.oi_peak = None
        self.sweep_extreme = None
        self.reclaim_fail_deadline = None

    @staticmethod
    def compute_exits(entry_avg_price: float, sweep_extreme: float, box_high: float, box_low: float,
                       p: CascadeBParams, side: str = "short"):
        """side="short"(상단 돌파 트랩): 기존과 동일 — 스윕 고점 위 손절, 박스
        중앙 1차 목표, 레버리지 청산가 근사 중 가까운 쪽이 2차 목표(둘 다 하락 방향).
        side="long"(하단 돌파 트랩): 거울상 — 스윕 저점 아래 손절, 레버리지
        청산가 근사가 둘 다 상승 방향이며 가까운(작은 상승폭) 쪽이 2차 목표."""
        tp1 = (box_high + box_low) / 2  # 박스 중앙 (양방향 공용)
        if side == "short":
            sl = sweep_extreme * (1 + p.sl_buffer_pct)
            liq_10x = entry_avg_price * (1 - p.lev_10x_liq_pct)
            liq_25x = entry_avg_price * (1 - p.lev_25x_liq_pct)
            tp2 = max(liq_10x, liq_25x)  # "가까운 쪽" = 진입가 대비 더 가까운(작은 하락폭) 청산가
        else:
            sl = sweep_extreme * (1 - p.sl_buffer_pct)
            liq_10x = entry_avg_price * (1 + p.lev_10x_liq_pct)
            liq_25x = entry_avg_price * (1 + p.lev_25x_liq_pct)
            tp2 = min(liq_10x, liq_25x)  # "가까운 쪽" = 더 작은 상승폭
        return sl, tp1, tp2
