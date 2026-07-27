"""Setup A — 청산 캐스케이드 소진 롱 (Cascade Exhaustion Long)

[재설계] forceOrder(청산 틱) 없이 캔들 + 거래량 + OI만으로 동작한다.
이론적 근거는 setup_c.py의 Price x OI 상태 분류기와 동일: 가격 하락이
디레버리징(청산)인지 신규 숏(추세)인지는 OI로만 구분되고, 급격한 OI
하락은 청산 캐스케이드의 집계 발자국이다. forceOrder 틱을 직접 못 봐도
그 결과로 나타나는 "가격 급락 + OI 급감 + 거래량 폭증"은 캔들 스트림만으로
탐지 가능하다 — 이게 이 모듈이 지역 차단 등으로 forceOrder가 전혀 안 들어와도
정상 작동하는 이유다.

트리거 구조(T1~T4)는 원래 명세와 동일하게 유지하되, 데이터 소스만 교체했다:
  T1(캐스케이드 감지): 60~180초 창의 가격하락이 ATR의 N배 이상
                       & DeltaOI%가 최근 분포 하위 q% & RVOL이 상위 q%
                       (구: 60초 SELL청산 합계 >= 시간당평균*N & 최소 연쇄건수)
  T2(가격이탈 확인):   캐스케이드 시작 대비 추가 하락률 (변경 없음 — 원래도 캔들 기반)
  T3(소진 확인):       OI 감속(2차미분 부호전환) + 반전캔들(CLV/윅비대칭)
                       + CVD 양전환 + 반등 유지
                       (구: 무청산 경과시간 -> OI 감속 확인으로 대체.
                        CVD/반등은 원래도 forceOrder와 무관했으므로 변경 없음)
  T4(매크로 블랙아웃): 변경 없음

StrategyEngine과의 계약(state/cascade_id/log/pending_signal/compute_exits/
describe/conditions/reset/allow_reentry/register_reentry/macro_blackouts)은
전부 그대로 유지한다 — engine.py는 이 파일의 내부 구현을 몰라도 된다.
"""

import statistics
from dataclasses import dataclass
from collections import deque
from .data_types import Candle


def _quantile(values: list, q: float):
    """표준 statistics 모듈에 임의 분위수 함수가 없어 직접 구현(선형보간)."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


@dataclass
class CascadeAParams:
    lookback_hours: float = 24.0          # DeltaOI%/RVOL 분포(분위수 게이트) 산정 구간
    cascade_window_s: float = 180.0       # T1: 가격/OI 변화 관찰 창(구 60초 — 다중 봉 관찰 위해 확장)
    price_drop_atr_mult: float = 1.5      # T1: 윈도우 하락폭이 ATR의 몇 배 이상이어야 하는가 (구 vol_multiplier 대체)
    oi_drop_quantile: float = 0.05        # T1: DeltaOI%가 최근 분포 하위 몇 %여야 디레버리징으로 보는가 (구 min_chain 대체)
    rvol_quantile: float = 0.90           # T1: RVOL이 최근 분포 상위 몇 %여야 강제활동으로 보는가 (신규)
    rvol_baseline_bars: int = 20          # RVOL 중앙값 기준 창(1분봉 개수)
    atr_window_bars: int = 14             # ATR 계산 창(1분봉 개수)
    min_move_pct: float = 0.004           # T2: 캐스케이드 시작 대비 하락률 (변경 없음)
    cvd_window_s: float = 60.0            # T3: 1분 CVD (변경 없음 — aggTrade 기반, forceOrder 무관)
    oi_decel_confirm_s: float = 45.0      # T3: OI 감속+반전캔들 유지 시간 (구 exhaustion_gap_s "무청산 경과" 대체)
    reversal_clv_min: float = 0.7         # T3: 반전 캔들 판정 — CLV 최소치(스케일 없는 비율이라 고정 상수)
    reversal_wick_ratio_min: float = 1.5  # T3: 반전 캔들 판정 — 하단윅/상단윅 최소 비율
    rebound_pct: float = 0.0008           # T3: 저점 대비 반등폭 (변경 없음)
    rebound_hold_s: float = 15.0          # T3: 반등 유지 시간 (변경 없음)
    sl_buffer_pct: float = 0.0015         # 손절 = 저점 - 버퍼
    tp1_retrace: float = 0.38             # 되돌림 38%
    tp2_retrace: float = 0.618            # 되돌림 61.8%
    tp1_fraction: float = 0.5             # TP1에서 청산할 비율
    time_exit_s: float = 90 * 60          # 시간 청산 (90분)
    max_reentries: int = 1                # 동일 캐스케이드 재진입 허용 횟수


class CascadeExhaustionLong:
    """T1~T4 감지 + 포지션 관리 상태 머신. 스트리밍(캔들 1건씩) 입력, pandas 무사용
    (setup_a.py/setup_b.py의 기존 관례 유지 — setup_c.py만 DataFrame 배치 처리)."""

    MIN_DIST_SAMPLES = 30    # 분위수 게이트가 유효하려면 최소 이만큼 표본이 쌓여야 한다
    GATE_REFRESH_TICKS = 5   # 분위수 게이트 재계산 주기(틱=캔들 수)

    def __init__(self, params: CascadeAParams = None, macro_blackouts=None):
        self.p = params or CascadeAParams()
        # macro_blackouts: [(start_ts, end_ts), ...]  FOMC/CPI ±30분
        self.macro_blackouts = macro_blackouts or []

        self._cvd_buf = deque()            # (ts, delta) 1분 CVD 롤링 — forceOrder와 무관, 그대로 유지

        # --- OI+거래량+ATR 프록시용 히스토리 ---
        self._prev_close = None
        self._tr_hist = deque()            # true range, 길이<=atr_window_bars
        self._vol_hist = deque()           # volume, 길이<=rvol_baseline_bars
        self._close_win = deque()          # (ts, close) — cascade_window_s+버퍼만 유지(하락폭 계산용)
        self._oi_win = deque()             # (ts, oi) — cascade_window_s+버퍼만 유지(DeltaOI%용)
        self._oi_chg_dist = deque()        # (ts, oi_chg_now) — lookback_hours 유지, 분위수 게이트용
        self._rvol_dist = deque()          # (ts, rvol_now) — lookback_hours 유지, 분위수 게이트용
        self._last_oi_chg_for_decel = None  # OI 감속(2차미분) 판정용 직전 tick 값

        # 분위수 게이트 캐시 — 매 틱(1분봉)마다 최대 lookback_hours*60개 값을 정렬하는
        # 건 라이브(1분당 1회)엔 무해하지만 그리드서치/장기 백테스트에서 병목이 된다.
        # 게이트가 나타내는 "최근 레짐"은 몇 분 지연돼도 사실상 동일하므로
        # GATE_REFRESH_TICKS 틱마다만 재계산한다.
        self._oi_gate = None
        self._rvol_gate = None
        self._gate_tick_count = 0

        self.state = "IDLE"                # IDLE -> CASCADE -> WATCH_EXHAUST -> ARMED
        self.cascade_start_ts = None
        self.cascade_start_price = None
        self.cascade_low = None
        self.cascade_low_ts = None
        self.rebound_since_ts = None
        self.reentry_count = 0
        self.cascade_id = 0
        self._exhaust_confirm_run = 0

        self.pending_signal = None         # {"ts", "price", "tag"} 진입 신호
        self.log = []

    def _in_macro_blackout(self, ts: float) -> bool:
        return any(s <= ts <= e for s, e in self.macro_blackouts)

    def on_cvd_delta(self, ts: float, delta: float):
        self._cvd_buf.append((ts, delta))
        while self._cvd_buf and ts - self._cvd_buf[0][0] > self.p.cvd_window_s:
            self._cvd_buf.popleft()

    def _cvd_1m(self) -> float:
        return sum(d for _, d in self._cvd_buf)

    # ---- 히스토리/피처 -------------------------------------------------
    @staticmethod
    def _clv(c: Candle) -> float:
        rng = c.high - c.low
        if rng <= 0:
            return 0.5
        return (c.close - c.low) / rng

    @staticmethod
    def _wick_ratio(c: Candle, cap: float = 100.0) -> float:
        body_hi = max(c.open, c.close)
        body_lo = min(c.open, c.close)
        lower = max(body_lo - c.low, 0.0)
        upper = max(c.high - body_hi, 0.0)
        if upper == 0 and lower == 0:
            return 1.0
        if upper == 0:
            return cap
        return min(lower / upper, cap)

    @staticmethod
    def _value_at_or_before(ts_val_deque, target_ts):
        result = None
        for ts_, val_ in ts_val_deque:
            if ts_ <= target_ts:
                result = val_
            else:
                break
        return result

    def _atr(self):
        if not self._tr_hist:
            return None
        return sum(self._tr_hist) / len(self._tr_hist)

    def _rvol_now(self, current_volume):
        if len(self._vol_hist) < max(5, self.p.rvol_baseline_bars // 2):
            return None
        med = statistics.median(self._vol_hist)
        if med == 0:
            return None
        return current_volume / med

    def _oi_change_now(self, now_ts):
        base_oi = self._value_at_or_before(self._oi_win, now_ts - self.p.cascade_window_s)
        if base_oi is None or base_oi == 0 or not self._oi_win:
            return None
        latest_oi = self._oi_win[-1][1]
        return (latest_oi - base_oi) / base_oi

    def _price_drop_atr(self, now_ts, now_close):
        base_close = self._value_at_or_before(self._close_win, now_ts - self.p.cascade_window_s)
        if base_close is None:
            return None, None
        atr_now = self._atr()
        if not atr_now:
            return None, None
        return (base_close - now_close) / atr_now, base_close

    def _quantile_gate(self, dist_deque, q):
        vals = [v for _, v in dist_deque]
        if len(vals) < self.MIN_DIST_SAMPLES:
            return None
        return _quantile(vals, q)

    def _update_history(self, c: Candle, oi_now):
        p = self.p
        if self._prev_close is not None:
            tr = max(c.high - c.low, abs(c.high - self._prev_close), abs(c.low - self._prev_close))
            self._tr_hist.append(tr)
            if len(self._tr_hist) > p.atr_window_bars:
                self._tr_hist.popleft()
        self._prev_close = c.close

        self._vol_hist.append(c.volume)
        if len(self._vol_hist) > p.rvol_baseline_bars:
            self._vol_hist.popleft()

        self._close_win.append((c.ts, c.close))
        while self._close_win and c.ts - self._close_win[0][0] > p.cascade_window_s + 60:
            self._close_win.popleft()

        if oi_now is not None:
            self._oi_win.append((c.ts, oi_now))
        while self._oi_win and c.ts - self._oi_win[0][0] > p.cascade_window_s + 300:
            self._oi_win.popleft()

        lookback_s = p.lookback_hours * 3600.0
        oi_chg_now = self._oi_change_now(c.ts)
        if oi_chg_now is not None:
            self._oi_chg_dist.append((c.ts, oi_chg_now))
        while self._oi_chg_dist and c.ts - self._oi_chg_dist[0][0] > lookback_s:
            self._oi_chg_dist.popleft()

        rvol_now = self._rvol_now(c.volume)
        if rvol_now is not None:
            self._rvol_dist.append((c.ts, rvol_now))
        while self._rvol_dist and c.ts - self._rvol_dist[0][0] > lookback_s:
            self._rvol_dist.popleft()

        self._gate_tick_count += 1
        if self._oi_gate is None or self._gate_tick_count % self.GATE_REFRESH_TICKS == 0:
            self._oi_gate = self._quantile_gate(self._oi_chg_dist, p.oi_drop_quantile)
            self._rvol_gate = self._quantile_gate(self._rvol_dist, p.rvol_quantile)

    # ---- 트리거 판정 ----------------------------------------------------
    def _check_t1(self, c: Candle):
        drop_atr, base_close = self._price_drop_atr(c.ts, c.close)
        oi_chg_now = self._oi_change_now(c.ts)
        rvol_now = self._rvol_now(c.volume)
        oi_gate = self._oi_gate
        rvol_gate = self._rvol_gate

        if (drop_atr is not None and drop_atr >= self.p.price_drop_atr_mult
                and oi_chg_now is not None and oi_gate is not None and oi_chg_now <= oi_gate
                and rvol_now is not None and rvol_gate is not None and rvol_now >= rvol_gate):
            self.cascade_id += 1
            self.state = "CASCADE"
            self.cascade_start_ts = c.ts
            self.cascade_start_price = base_close
            self.cascade_low = min(base_close, c.close)
            self.cascade_low_ts = c.ts
            self.reentry_count = 0
            self._exhaust_confirm_run = 0
            self.log.append((c.ts, f"T1 캐스케이드 감지 (id={self.cascade_id}, "
                                    f"하락={drop_atr:.2f}xATR, dOI={oi_chg_now*100:.2f}%, RVOL={rvol_now:.2f})"))

    def _check_t3(self, c: Candle):
        p = self.p
        oi_chg_now = self._oi_change_now(c.ts)
        oi_decel_ok = False
        if oi_chg_now is not None and self._last_oi_chg_for_decel is not None:
            oi_decel_ok = (oi_chg_now - self._last_oi_chg_for_decel) > 0  # 하락 속도 감속(2차미분 부호전환)
        if oi_chg_now is not None:
            self._last_oi_chg_for_decel = oi_chg_now

        reversal_ok = (self._clv(c) >= p.reversal_clv_min
                       and self._wick_ratio(c) >= p.reversal_wick_ratio_min)

        if oi_decel_ok and reversal_ok:
            self._exhaust_confirm_run += 1
        else:
            self._exhaust_confirm_run = 0
        decel_hold_ok = self._exhaust_confirm_run * 60.0 >= p.oi_decel_confirm_s  # 1분봉 가정

        cvd_ok = self._cvd_1m() >= 0
        rebound_pct_now = (c.close - self.cascade_low) / self.cascade_low
        if rebound_pct_now >= p.rebound_pct:
            if self.rebound_since_ts is None:
                self.rebound_since_ts = c.ts
        else:
            self.rebound_since_ts = None
        rebound_hold_ok = (
            self.rebound_since_ts is not None
            and (c.ts - self.rebound_since_ts) >= p.rebound_hold_s
        )

        if decel_hold_ok and cvd_ok and rebound_hold_ok:
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

    def on_candle(self, c: Candle, oi_now: float = None):
        """가격/거래량/OI 스트림 tick. T1(신규 진입 감지)·T2(가격이탈 재평가)·
        T3(소진 판정)가 전부 여기서 이뤄진다 — forceOrder 불필요."""
        self._update_history(c, oi_now)

        if self.state in ("CASCADE", "WATCH_EXHAUST"):
            if c.low < self.cascade_low:
                self.cascade_low = c.low
                self.cascade_low_ts = c.ts
            move = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
            if self.state == "CASCADE" and move >= self.p.min_move_pct:
                self.state = "WATCH_EXHAUST"
                self.log.append((c.ts, f"T2 가격이탈 확인 move={move*100:.2f}%"))

        if self.state == "IDLE":
            self._check_t1(c)
        elif self.state == "WATCH_EXHAUST":
            self._check_t3(c)

    def consume_signal(self):
        sig = self.pending_signal
        self.pending_signal = None
        return sig

    def describe(self):
        """현재 상태를 사람이 읽을 텍스트로 설명 (GUI/텔레그램 표시용).

        반환: (설명 텍스트, 참고 가격 또는 None). 참고 가격은 차트에
        점선으로 그릴 때 쓰는 '지금 전략이 주시 중인 레벨'이다.
        """
        p = self.p
        if self.state == "IDLE":
            return (
                f"청산 캐스케이드 대기 중 — {p.cascade_window_s:.0f}초 내 가격하락 ≥ ATR×{p.price_drop_atr_mult:.1f} "
                f"& DeltaOI 하위{p.oi_drop_quantile*100:.0f}% & RVOL 상위{(1-p.rvol_quantile)*100:.0f}% "
                "충족 시 추적 시작",
                None,
            )
        if self.state == "CASCADE":
            move_pct = 0.0
            if self.cascade_start_price:
                move_pct = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price * 100
            return (
                f"캐스케이드 진행 중(#{self.cascade_id}) — 저점 {self.cascade_low:,.1f} "
                f"(시작가 대비 -{move_pct:.2f}%). -{p.min_move_pct*100:.2f}% 하락 확인되면 소진 관찰 시작",
                self.cascade_low,
            )
        if self.state == "WATCH_EXHAUST":
            return (
                f"하락 소진 확인 중 — 저점 {self.cascade_low:,.1f} 대비 +{p.rebound_pct*100:.2f}% 반등이 "
                f"{p.rebound_hold_s:.0f}초 유지 + OI 감속·반전캔들 {p.oi_decel_confirm_s:.0f}초 + CVD 양전환 시 매수 진입",
                self.cascade_low,
            )
        if self.state == "ARMED":
            return "진입 신호 발생 — 체결 대기 중", self.cascade_low
        return self.state, None

    def conditions(self, now_ts: float):
        """T1~T4 하위 조건 각각의 실시간 충족 여부 (GUI 체크리스트 표시용).

        상태 단계와 무관하게 항상 전부 계산한다. 반환: [{"key","label","met","detail"}, ...] (8개 고정).
        """
        p = self.p
        out = []

        now_close = self._prev_close if self._prev_close is not None else 0.0
        drop_atr, _ = self._price_drop_atr(now_ts, now_close)
        oi_chg_now = self._oi_change_now(now_ts)
        rvol_now = self._rvol_now(self._vol_hist[-1]) if self._vol_hist else None
        oi_gate = self._oi_gate
        rvol_gate = self._rvol_gate

        out.append({
            "key": "t1_drop", "label": f"T1: {p.cascade_window_s:.0f}초 하락 ≥ ATR×{p.price_drop_atr_mult:.1f}",
            "met": bool(drop_atr is not None and drop_atr >= p.price_drop_atr_mult),
            "detail": f"{drop_atr:.2f}x" if drop_atr is not None else "계산불가",
        })
        out.append({
            "key": "t1_oi", "label": f"T1: DeltaOI 하위{p.oi_drop_quantile*100:.0f}%",
            "met": bool(oi_chg_now is not None and oi_gate is not None and oi_chg_now <= oi_gate),
            "detail": f"{oi_chg_now*100:.2f}%" if oi_chg_now is not None else "N/A",
        })
        out.append({
            "key": "t1_rvol", "label": f"T1: RVOL 상위{(1-p.rvol_quantile)*100:.0f}%",
            "met": bool(rvol_now is not None and rvol_gate is not None and rvol_now >= rvol_gate),
            "detail": f"{rvol_now:.2f}" if rvol_now is not None else "N/A",
        })

        if self.cascade_start_price and self.cascade_low is not None:
            move_pct = (self.cascade_start_price - self.cascade_low) / self.cascade_start_price
        else:
            move_pct = 0.0
        out.append({
            "key": "t2_move", "label": f"T2: 하락률 ≥ {p.min_move_pct*100:.2f}%",
            "met": move_pct >= p.min_move_pct,
            "detail": f"{move_pct*100:.2f}%",
        })

        out.append({
            "key": "t3_oi_decel", "label": f"T3: OI감속+반전캔들 {p.oi_decel_confirm_s:.0f}초 유지",
            "met": self._exhaust_confirm_run * 60.0 >= p.oi_decel_confirm_s,
            "detail": f"{self._exhaust_confirm_run}봉 연속" if self._exhaust_confirm_run else "미확인",
        })
        cvd = self._cvd_1m()
        out.append({
            "key": "t3_cvd", "label": "T3: 1분 CVD ≥ 0",
            "met": cvd >= 0, "detail": f"{cvd:+.2f}",
        })
        hold = (now_ts - self.rebound_since_ts) if self.rebound_since_ts is not None else 0.0
        out.append({
            "key": "t3_rebound", "label": f"T3: 반등 {p.rebound_pct*100:.2f}% 유지 ≥ {p.rebound_hold_s:.0f}초",
            "met": self.rebound_since_ts is not None and hold >= p.rebound_hold_s,
            "detail": f"{hold:.0f}초 유지 중" if self.rebound_since_ts is not None else "반등 미확인",
        })
        blackout = self._in_macro_blackout(now_ts)
        out.append({
            "key": "t4_macro", "label": "T4: 매크로 블랙아웃 아님",
            "met": not blackout, "detail": "차단 구간" if blackout else "정상",
        })
        return out

    def allow_reentry(self) -> bool:
        return self.reentry_count < self.p.max_reentries

    def register_reentry(self):
        self.reentry_count += 1
        self.state = "WATCH_EXHAUST"
        self.rebound_since_ts = None
        self._exhaust_confirm_run = 0

    def reset(self):
        """탐지 상태만 IDLE로 되돌린다. cascade_low/start_price 등은 재진입
        판단(register_reentry)에 쓰일 수 있으므로 남겨둔다."""
        self.state = "IDLE"
        self.rebound_since_ts = None
        self._exhaust_confirm_run = 0

    @staticmethod
    def compute_exits(entry_price: float, cascade_low: float, cascade_start_price: float, p: CascadeAParams):
        move = cascade_start_price - cascade_low
        sl = cascade_low * (1 - p.sl_buffer_pct)
        tp1 = cascade_low + p.tp1_retrace * move
        tp2 = cascade_low + p.tp2_retrace * move
        return sl, tp1, tp2
