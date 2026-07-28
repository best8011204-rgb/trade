"""Setup C — Price x OI 상태 분류기 (선물 forceOrder 없이 완전 백테스트 가능).

이론적 코어: 가격이 움직일 때 그게 신규 포지션인지 청산인지는 OI로만
구분된다(캔들·거래량만으로는 불가능한 유일한 정보 차원).

  가격 상승 + OI 상승 = 신규 롱(건강한 추세)
  가격 상승 + OI 하락 = 숏 커버링(약한 랠리, 페이드 후보)
  가격 하락 + OI 상승 = 신규 숏(진짜 추세 — 역추세 금지)
  가격 하락 + OI 하락 = 롱 청산/디레버리징(페이드 가능 — 기계적 과매도)

이 파일은 두 개의 독립 상태머신을 구현한다:
  C1 = OI-Flush Reversal Long  (청산 캐스케이드 소진 롱, forceOrder 대신
       OI 붕괴를 캐스케이드의 집계 발자국으로 씀. setup_a.py의
       IDLE->CASCADE->WATCH_EXHAUST 상태머신 구조를 그대로 재사용)
  C2 = Coil-Break with OI Filter (좁은 레인지 응축 후 돌파 시 OI 거동으로
       진짜 모멘텀 vs 스퀴즈/트랩을 가른다. setup_b.py의 일반화)

Setup 3(OI 다이버전스 소진)은 스펙 상 최우선순위가 아니라 이번엔 구현하지
않는다.

StrategyEngine 무수정 원칙 유지: 이 모듈은 klines(+OI 병합) DataFrame과
정수 인덱스 i를 받아 신호/상태를 반환하는 순수 함수 집합이다. 포지션
라이프사이클(트랜치/재진입/쿨다운)은 backtest_c.py/live_setup_c.py가 담당.

파라미터는 하드코딩 상수 대신 rolling_quantile 기반 적응형 임계값을 쓴다
(레짐 강건성, DSR 페널티 축소). 단, 캔들 형태 비율(CLV/윅비대칭)처럼
이미 스케일이 없는[0,1] 또는 [0,inf) 값은 고정 상수로 둬도 무방하다
(변동성 레짐에 의존하지 않으므로).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from . import oi_features as feat

COST_BPS = 10.0

# DeltaOI% 다중 윈도우(5/15/60분 @ 5분봉) — 튜닝 대상이 아닌 고정 피처 정의라
# dataclass 필드가 아니라 모듈 상수로 둔다(ParamGroup 폼이 tuple을 못 다룸).
OI_CHANGE_WINDOWS_BARS = (1, 3, 12)  # 5m, 15m, 60m


@dataclass
class ParamsC:
    # --- 공유 ---
    atr_window: int = 14
    quantile_lookback_bars: int = 2016   # 5m x 2016 = 7일. 분위수 게이트의 롤링 창.

    # --- C1: OI-Flush Reversal Long (탐지 knob 3개: price_drop/oi_drop/rvol) ---
    c1_price_drop_atr_mult: float = 2.0      # N봉 하락폭이 ATR의 몇 배 이상이어야 캐스케이드 후보인가
    c1_oi_drop_quantile: float = 0.05        # DeltaOI%(가격창과 동일 N봉)가 최근 분포 하위 몇 %여야 디레버리징으로 보는가
    c1_rvol_quantile: float = 0.95           # RVOL이 최근 분포 상위 몇 %여야 강제활동으로 보는가
    c1_cascade_window_bars: int = 6          # 트리거 판정에 쓰는 N봉(30분 @5분봉)
    c1_exhaustion_confirm_bars: int = 2      # 소진 확인(감속+반전캔들) 연속 필요 봉수
    c1_reversal_clv_min: float = 0.7         # 반전 캔들 판정: CLV 최소치(스케일 없는 비율이라 고정 상수)
    c1_reversal_wick_ratio_min: float = 2.0  # 반전 캔들 판정: 하단윅/상단윅 최소 비율
    c1_sl_buffer_pct: float = 0.001
    c1_tp1_retrace: float = 0.382
    c1_tp2_retrace: float = 0.618
    c1_tp1_fraction: float = 0.5
    c1_time_exit_bars: int = 24              # 2시간 @5분봉
    c1_max_reentries: int = 1

    # --- C2: Coil-Break with OI Filter (탐지 knob: 코일 3개 + 돌파분류 2개) ---
    c2_coil_window_bars: int = 48            # 4시간 @5분봉 (기존 박스 관례와 동일)
    c2_coil_range_quantile: float = 0.20     # 실현 레인지가 최근 분포 하위 몇 %여야 응축(코일)으로 보는가
    c2_oi_rise_quantile: float = 0.80        # 코일 구간 OI 증가율이 최근 분포 상위 몇 %여야 하는가
    c2_vol_contraction_quantile: float = 0.20  # 코일 구간 중앙거래량이 최근 분포 하위 몇 %여야 하는가
    c2_breakout_oi_drop_quantile: float = 0.05   # 돌파봉 DeltaOI%가 하위 몇 %면 스퀴즈/트랩(페이드)로 보는가
    c2_breakout_vol_expansion_quantile: float = 0.90  # 돌파봉 RVOL이 상위 몇 %면 모멘텀(진짜)으로 보는가
    c2_sl_buffer_pct: float = 0.001
    c2_tp1_fraction: float = 0.5
    c2_time_exit_bars: int = 96              # 8시간 @5분봉
    c2_max_reentries: int = 1


def compute_features(df: pd.DataFrame, p: ParamsC) -> pd.DataFrame:
    """df: columns=[ts,open,high,low,close,volume,oi], DatetimeIndex(ts).
    C1/C2 상태머신이 공유하는 모든 피처 컬럼을 한 번에 계산해 붙인다."""
    out = df.copy()
    out["atr"] = feat.atr(out, p.atr_window)
    out["rvol"] = feat.rvol(out["volume"], p.c1_cascade_window_bars)
    out["clv"] = feat.clv(out)
    out["wick_asym"] = feat.wick_asymmetry(out)
    out["body_range"] = feat.body_range_ratio(out)
    out["range_exp"] = feat.range_expansion(out, p.atr_window)

    for n, label in zip(OI_CHANGE_WINDOWS_BARS, ("w1", "w2", "w3")):
        out[f"oi_chg_{label}"] = feat.oi_change_pct(out["oi"], n)

    # C1 게이트: 캐스케이드 창(N봉)과 동일한 창의 OI 변화율을 기본으로 쓴다.
    out["oi_chg_c1"] = feat.oi_change_pct(out["oi"], p.c1_cascade_window_bars)
    out["oi_chg_c1_qlo"] = feat.rolling_quantile(out["oi_chg_c1"], p.quantile_lookback_bars, p.c1_oi_drop_quantile)
    out["rvol_qhi"] = feat.rolling_quantile(out["rvol"], p.quantile_lookback_bars, p.c1_rvol_quantile)

    # C2 게이트
    out["oi_chg_c2_breakout"] = feat.oi_change_pct(out["oi"], 1)  # 돌파봉 즉시(5분) 변화
    out["oi_chg_c2_qlo"] = feat.rolling_quantile(out["oi_chg_c2_breakout"], p.quantile_lookback_bars, p.c2_breakout_oi_drop_quantile)
    out["rvol_c2_qhi"] = feat.rolling_quantile(out["rvol"], p.quantile_lookback_bars, p.c2_breakout_vol_expansion_quantile)

    realized_range = (out["high"].rolling(p.c2_coil_window_bars).max()
                       - out["low"].rolling(p.c2_coil_window_bars).min()) / out["close"]
    out["realized_range_c2"] = realized_range
    out["realized_range_c2_qlo"] = feat.rolling_quantile(realized_range, p.quantile_lookback_bars, p.c2_coil_range_quantile)

    vol_med_window = out["volume"].rolling(p.c2_coil_window_bars).median()
    out["vol_med_c2"] = vol_med_window
    out["vol_med_c2_qlo"] = feat.rolling_quantile(vol_med_window, p.quantile_lookback_bars, p.c2_vol_contraction_quantile)

    oi_trend_pct = out["oi"].pct_change(p.c2_coil_window_bars)
    out["oi_trend_c2"] = oi_trend_pct
    out["oi_trend_c2_qhi"] = feat.rolling_quantile(oi_trend_pct, p.quantile_lookback_bars, p.c2_oi_rise_quantile)

    return out


def tranche_exit_step(side: str, pos: dict, high: float, low: float, close: float,
                       bars_held: int, time_exit_bars: int):
    """C1(항상 side='long')과 C2(양방향)가 공유하는 SL/TP1(부분)/TP2/TIME 판정.
    pos: {"stop","tp1","tp2","tp1_fraction","tp1_hit"} — tp1_hit는 in-place 갱신.
    반환: (reason|None, exit_price|None, exit_fraction|None)."""
    remaining_fraction = 1.0 if not pos["tp1_hit"] else 1.0 - pos["tp1_fraction"]
    if side == "long":
        if low <= pos["stop"]:
            return "SL", pos["stop"], remaining_fraction
        if not pos["tp1_hit"] and high >= pos["tp1"]:
            pos["tp1_hit"] = True
            return "TP1", pos["tp1"], pos["tp1_fraction"]
        if pos["tp1_hit"] and high >= pos["tp2"]:
            return "TP2", pos["tp2"], remaining_fraction
    else:
        if high >= pos["stop"]:
            return "SL", pos["stop"], remaining_fraction
        if not pos["tp1_hit"] and low <= pos["tp1"]:
            pos["tp1_hit"] = True
            return "TP1", pos["tp1"], pos["tp1_fraction"]
        if pos["tp1_hit"] and low <= pos["tp2"]:
            return "TP2", pos["tp2"], remaining_fraction
    if bars_held >= time_exit_bars:
        return "TIME", close, remaining_fraction
    return None, None, None


# ---------------------------------------------------------------- C1: OI-Flush Reversal Long

@dataclass
class SignalC1:
    ts: pd.Timestamp
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp1_fraction: float
    time_exit_bars: int
    cascade_low: float
    meta: dict = field(default_factory=dict)


@dataclass
class C1State:
    state: str = "IDLE"   # IDLE -> CASCADE -> WATCH_EXHAUST
    cascade_start_i: int | None = None
    cascade_low: float | None = None
    cascade_start_price: float | None = None
    oi_at_cascade_start: float | None = None
    exhaust_confirm_run: int = 0


def _min_history_c1(p: ParamsC) -> int:
    return p.quantile_lookback_bars + p.atr_window + 2


def advance_c1(f: pd.DataFrame, i: int, state: C1State, p: ParamsC):
    if i < _min_history_c1(p):
        return state, None
    row = f.iloc[i]

    if state.state == "IDLE":
        window = f.iloc[i - p.c1_cascade_window_bars + 1: i + 1]
        if len(window) < p.c1_cascade_window_bars:
            return state, None
        atr_now = row["atr"]
        if not np.isfinite(atr_now) or atr_now <= 0:
            return state, None
        drop_atr = (window["close"].iloc[0] - window["close"].iloc[-1]) / atr_now
        oi_chg, oi_gate = row["oi_chg_c1"], row["oi_chg_c1_qlo"]
        rvol_now, rvol_gate = row["rvol"], row["rvol_qhi"]
        if (drop_atr >= p.c1_price_drop_atr_mult
                and np.isfinite(oi_chg) and np.isfinite(oi_gate) and oi_chg <= oi_gate
                and np.isfinite(rvol_now) and np.isfinite(rvol_gate) and rvol_now >= rvol_gate):
            return C1State(
                state="CASCADE", cascade_start_i=i - p.c1_cascade_window_bars + 1,
                cascade_low=float(window["low"].min()),
                cascade_start_price=float(window["close"].iloc[0]),
                oi_at_cascade_start=float(window["oi"].iloc[0]),
            ), None
        return state, None

    if state.state == "CASCADE":
        # 거부 조건: 하락 지속 중 OI가 순증(신규 숏 우세) -> 페이드 금지, IDLE로 리셋
        if row["oi"] > state.oi_at_cascade_start and row["oi_chg_c1"] is not None and row["oi_chg_c1"] > 0:
            return C1State(), None
        return replace(state, state="WATCH_EXHAUST",
                        cascade_low=min(state.cascade_low, float(row["low"]))), None

    if state.state == "WATCH_EXHAUST":
        if row["oi"] > state.oi_at_cascade_start and row["oi_chg_c1"] is not None and row["oi_chg_c1"] > 0:
            return C1State(), None  # 소진 관찰 중에도 신규 숏 유입되면 무효화
        state = replace(state, cascade_low=min(state.cascade_low, float(row["low"])))

        oi_accel = row["oi_chg_c1"] - f["oi_chg_c1"].iloc[i - 1] if i > 0 else np.nan
        decel = np.isfinite(oi_accel) and oi_accel > 0
        reversal = (row["clv"] >= p.c1_reversal_clv_min) and (row["wick_asym"] >= p.c1_reversal_wick_ratio_min)

        if decel and reversal:
            state = replace(state, exhaust_confirm_run=state.exhaust_confirm_run + 1)
        else:
            state = replace(state, exhaust_confirm_run=0)

        if state.exhaust_confirm_run >= p.c1_exhaustion_confirm_bars:
            entry = float(row["close"])
            cascade_range = state.cascade_start_price - state.cascade_low
            if cascade_range <= 0:
                return C1State(), None
            tp1 = state.cascade_low + p.c1_tp1_retrace * cascade_range
            tp2 = state.cascade_low + p.c1_tp2_retrace * cascade_range
            stop = state.cascade_low * (1 - p.c1_sl_buffer_pct)
            sig = SignalC1(ts=f.index[i], entry=entry, stop=stop, tp1=tp1, tp2=tp2,
                            tp1_fraction=p.c1_tp1_fraction, time_exit_bars=p.c1_time_exit_bars,
                            cascade_low=state.cascade_low,
                            meta={"cascade_start_i": state.cascade_start_i})
            return C1State(), sig

        # 소진 무한 대기 방지: 캐스케이드 창의 6배 지나도 안 되면 포기
        if i - state.cascade_start_i > p.c1_cascade_window_bars * 6:
            return C1State(), None
        return state, None

    return C1State(), None


def conditions_c1(f: pd.DataFrame, i: int, state: C1State, p: ParamsC) -> list[dict]:
    """GUI 실시간 체크리스트. 상태에 따라 관련 있는 조건만 표시."""
    if i < _min_history_c1(p):
        return [{"key": "c1_hist", "label": "C1: 히스토리 축적 중", "met": False, "detail": "대기"}]
    row = f.iloc[i]
    out = [{"key": "c1_state", "label": f"C1 상태: {state.state}", "met": state.state != "IDLE",
            "detail": state.state}]
    if state.state == "IDLE":
        window = f.iloc[max(0, i - p.c1_cascade_window_bars + 1): i + 1]
        atr_now = row["atr"]
        drop_atr = ((window["close"].iloc[0] - window["close"].iloc[-1]) / atr_now
                    if np.isfinite(atr_now) and atr_now > 0 and len(window) == p.c1_cascade_window_bars else np.nan)
        out.append({"key": "c1_drop", "label": f"가격하락 >= ATRx{p.c1_price_drop_atr_mult}",
                    "met": bool(np.isfinite(drop_atr) and drop_atr >= p.c1_price_drop_atr_mult),
                    "detail": f"{drop_atr:.2f}x" if np.isfinite(drop_atr) else "계산불가"})
        out.append({"key": "c1_oi", "label": f"DeltaOI 하위{p.c1_oi_drop_quantile*100:.0f}%",
                    "met": bool(np.isfinite(row["oi_chg_c1"]) and np.isfinite(row["oi_chg_c1_qlo"])
                                and row["oi_chg_c1"] <= row["oi_chg_c1_qlo"]),
                    "detail": f"{row['oi_chg_c1']*100:.2f}%" if np.isfinite(row["oi_chg_c1"]) else "N/A"})
        out.append({"key": "c1_rvol", "label": f"RVOL 상위{(1-p.c1_rvol_quantile)*100:.0f}%",
                    "met": bool(np.isfinite(row["rvol"]) and np.isfinite(row["rvol_qhi"])
                                and row["rvol"] >= row["rvol_qhi"]),
                    "detail": f"{row['rvol']:.2f}" if np.isfinite(row["rvol"]) else "N/A"})
    elif state.state == "WATCH_EXHAUST":
        out.append({"key": "c1_exhaust", "label": f"소진확인 {state.exhaust_confirm_run}/{p.c1_exhaustion_confirm_bars}봉",
                    "met": state.exhaust_confirm_run >= p.c1_exhaustion_confirm_bars,
                    "detail": f"캐스케이드저점 {state.cascade_low:,.1f}"})
    return out


def conditions_c1_live(rows: list, state: C1State, cache: dict, live_price: float,
                        live_oi: float, p: ParamsC) -> list[dict]:
    """conditions_c1()의 실시간(체결 틱) 버전 — 5분봉 확정을 기다리지 않고 매초
    갱신하기 위한 것. rows: 확정봉 원시 dict 리스트(라이브 러너의 캔들 히스토리).
    cache: 마지막 확정봉에서 계산해둔 {"atr","oi_chg_c1_qlo","rvol_qhi","rvol",
    "last_conditions"}. IDLE 상태에서만 가격/OI를 라이브 틱으로 다시 계산한다
    (RVOL은 아직 진행 중인 5분봉의 거래량을 추적하지 않아 마지막 확정치를 그대로 쓴다).
    IDLE이 아니면(WATCH_EXHAUST 등, 봉 카운트 기반 조건이라 틱 단위 라이브화가
    의미 없음) 마지막으로 계산된 확정봉 기준 conditions_c1() 결과를 그대로 반환."""
    if state.state != "IDLE":
        return cache.get("last_conditions") or [
            {"key": "c1_state", "label": f"C1 상태: {state.state}", "met": True, "detail": state.state}]

    out = [{"key": "c1_state", "label": "C1 상태: IDLE", "met": False, "detail": "IDLE"}]
    if len(rows) < p.c1_cascade_window_bars:
        out.append({"key": "c1_drop", "label": f"가격하락 >= ATRx{p.c1_price_drop_atr_mult}",
                    "met": False, "detail": "히스토리 부족"})
        return out

    base_close = rows[-p.c1_cascade_window_bars]["close"]
    base_oi = rows[-p.c1_cascade_window_bars].get("oi")
    atr_now = cache.get("atr")
    drop_atr = ((base_close - live_price) / atr_now
                if atr_now is not None and np.isfinite(atr_now) and atr_now > 0 else None)
    oi_chg_now = ((live_oi - base_oi) / base_oi
                  if base_oi and live_oi is not None else None)
    oi_gate = cache.get("oi_chg_c1_qlo")
    rvol_now = cache.get("rvol")     # 진행 중인 봉의 거래량은 아직 모르므로 마지막 확정치
    rvol_gate = cache.get("rvol_qhi")

    out.append({"key": "c1_drop", "label": f"가격하락 >= ATRx{p.c1_price_drop_atr_mult}",
                "met": bool(drop_atr is not None and drop_atr >= p.c1_price_drop_atr_mult),
                "detail": f"{drop_atr:.2f}x" if drop_atr is not None else "계산불가"})
    out.append({"key": "c1_oi", "label": f"DeltaOI 하위{p.c1_oi_drop_quantile*100:.0f}%",
                "met": bool(oi_chg_now is not None and oi_gate is not None
                            and np.isfinite(oi_gate) and oi_chg_now <= oi_gate),
                "detail": f"{oi_chg_now*100:.2f}%" if oi_chg_now is not None else "N/A"})
    out.append({"key": "c1_rvol", "label": f"RVOL 상위{(1-p.c1_rvol_quantile)*100:.0f}%",
                "met": bool(rvol_now is not None and rvol_gate is not None
                            and np.isfinite(rvol_now) and np.isfinite(rvol_gate) and rvol_now >= rvol_gate),
                "detail": f"{rvol_now:.2f}" if rvol_now is not None and np.isfinite(rvol_now) else "N/A"})
    return out


# ---------------------------------------------------------------- C2: Coil-Break with OI Filter

@dataclass
class SignalC2:
    ts: pd.Timestamp
    side: str          # "long" | "short"
    kind: str           # "momentum" | "fade"
    entry: float
    stop: float
    tp1: float
    tp2: float
    tp1_fraction: float
    time_exit_bars: int
    meta: dict = field(default_factory=dict)


@dataclass
class C2State:
    state: str = "IDLE"  # IDLE -> COIL -> BREAKOUT_PENDING
    coil_start_i: int | None = None
    box_low: float | None = None
    box_high: float | None = None
    oi_at_coil_start: float | None = None
    breakout_i: int | None = None
    breakout_side: str | None = None  # "up" | "down"
    breakout_sweep: float | None = None


def _min_history_c2(p: ParamsC) -> int:
    return p.quantile_lookback_bars + max(p.atr_window, p.c2_coil_window_bars) + 2


def _box_exits(entry: float, state: C2State, side: str, kind: str, p: ParamsC):
    rng = state.box_high - state.box_low
    if kind == "fade":
        if side == "short":
            stop = state.breakout_sweep * (1 + p.c2_sl_buffer_pct)
            tp2 = state.box_low
            tp1 = entry - 0.5 * (entry - tp2)
        else:
            stop = state.breakout_sweep * (1 - p.c2_sl_buffer_pct)
            tp2 = state.box_high
            tp1 = entry + 0.5 * (tp2 - entry)
    else:  # momentum
        if side == "long":
            stop = state.box_high * (1 - p.c2_sl_buffer_pct)
            tp1, tp2 = entry + rng, entry + 2 * rng
        else:
            stop = state.box_low * (1 + p.c2_sl_buffer_pct)
            tp1, tp2 = entry - rng, entry - 2 * rng
    return stop, tp1, tp2


def advance_c2(f: pd.DataFrame, i: int, state: C2State, p: ParamsC):
    if i < _min_history_c2(p):
        return state, None
    row = f.iloc[i]

    if state.state == "IDLE":
        window = f.iloc[i - p.c2_coil_window_bars + 1: i + 1]
        if len(window) < p.c2_coil_window_bars:
            return state, None
        rr, rr_gate = row["realized_range_c2"], row["realized_range_c2_qlo"]
        oi_trend, oi_gate = row["oi_trend_c2"], row["oi_trend_c2_qhi"]
        vol_med, vol_gate = row["vol_med_c2"], row["vol_med_c2_qlo"]
        if (np.isfinite(rr) and np.isfinite(rr_gate) and rr <= rr_gate
                and np.isfinite(oi_trend) and np.isfinite(oi_gate) and oi_trend >= oi_gate
                and np.isfinite(vol_med) and np.isfinite(vol_gate) and vol_med <= vol_gate):
            return C2State(
                state="COIL", coil_start_i=i - p.c2_coil_window_bars + 1,
                box_low=float(window["low"].min()), box_high=float(window["high"].max()),
                oi_at_coil_start=float(window["oi"].iloc[0]),
            ), None
        return state, None

    if state.state == "COIL":
        if row["close"] > state.box_high:
            return replace(state, state="BREAKOUT_PENDING", breakout_i=i,
                            breakout_side="up", breakout_sweep=float(row["high"])), None
        if row["close"] < state.box_low:
            return replace(state, state="BREAKOUT_PENDING", breakout_i=i,
                            breakout_side="down", breakout_sweep=float(row["low"])), None
        if i - state.coil_start_i > p.c2_coil_window_bars * 3:
            return C2State(), None
        return replace(state, box_low=min(state.box_low, float(row["low"])),
                        box_high=max(state.box_high, float(row["high"]))), None

    if state.state == "BREAKOUT_PENDING":
        entry = float(row["close"])
        oi_chg, fade_gate = row["oi_chg_c2_breakout"], row["oi_chg_c2_qlo"]
        rvol_now, momentum_gate = row["rvol"], row["rvol_c2_qhi"]

        if np.isfinite(oi_chg) and np.isfinite(fade_gate) and oi_chg <= fade_gate:
            side = "short" if state.breakout_side == "up" else "long"
            stop, tp1, tp2 = _box_exits(entry, state, side, "fade", p)
            sig = SignalC2(ts=f.index[i], side=side, kind="fade", entry=entry, stop=stop,
                            tp1=tp1, tp2=tp2, tp1_fraction=p.c2_tp1_fraction,
                            time_exit_bars=p.c2_time_exit_bars, meta={"box": (state.box_low, state.box_high)})
            return C2State(), sig

        if (row["oi"] > state.oi_at_coil_start and np.isfinite(rvol_now)
                and np.isfinite(momentum_gate) and rvol_now >= momentum_gate):
            side = "long" if state.breakout_side == "up" else "short"
            stop, tp1, tp2 = _box_exits(entry, state, side, "momentum", p)
            sig = SignalC2(ts=f.index[i], side=side, kind="momentum", entry=entry, stop=stop,
                            tp1=tp1, tp2=tp2, tp1_fraction=p.c2_tp1_fraction,
                            time_exit_bars=p.c2_time_exit_bars, meta={"box": (state.box_low, state.box_high)})
            return C2State(), sig

        if i - state.breakout_i > 2:   # 판정 보류 2봉까지, 그 이상은 포기
            return C2State(), None
        return state, None

    return C2State(), None


def conditions_c2(f: pd.DataFrame, i: int, state: C2State, p: ParamsC) -> list[dict]:
    if i < _min_history_c2(p):
        return [{"key": "c2_hist", "label": "C2: 히스토리 축적 중", "met": False, "detail": "대기"}]
    row = f.iloc[i]
    out = [{"key": "c2_state", "label": f"C2 상태: {state.state}", "met": state.state != "IDLE",
            "detail": state.state}]
    if state.state == "IDLE":
        out.append({"key": "c2_range", "label": f"레인지 응축(하위{p.c2_coil_range_quantile*100:.0f}%)",
                    "met": bool(np.isfinite(row["realized_range_c2"]) and np.isfinite(row["realized_range_c2_qlo"])
                                and row["realized_range_c2"] <= row["realized_range_c2_qlo"]),
                    "detail": f"{row['realized_range_c2']*100:.2f}%" if np.isfinite(row["realized_range_c2"]) else "N/A"})
        out.append({"key": "c2_oi", "label": f"OI 축적(상위{(1-p.c2_oi_rise_quantile)*100:.0f}%)",
                    "met": bool(np.isfinite(row["oi_trend_c2"]) and np.isfinite(row["oi_trend_c2_qhi"])
                                and row["oi_trend_c2"] >= row["oi_trend_c2_qhi"]),
                    "detail": f"{row['oi_trend_c2']*100:.2f}%" if np.isfinite(row["oi_trend_c2"]) else "N/A"})
        out.append({"key": "c2_vol", "label": f"거래량 수축(하위{p.c2_vol_contraction_quantile*100:.0f}%)",
                    "met": bool(np.isfinite(row["vol_med_c2"]) and np.isfinite(row["vol_med_c2_qlo"])
                                and row["vol_med_c2"] <= row["vol_med_c2_qlo"]),
                    "detail": f"{row['vol_med_c2']:.2f}" if np.isfinite(row["vol_med_c2"]) else "N/A"})
    elif state.state in ("COIL", "BREAKOUT_PENDING"):
        # COIL/BREAKOUT_PENDING 모두 박스가 존재하는 상태인데, 예전엔 여기서
        # 박스 값 자체를 어디에도 안 보여줬다(Setup B는 상/하단을 보여주는데
        # C2만 상태 이름만 나오고 실제 값이 안 보이던 문제).
        gap_hi = (row["close"] - state.box_high) / state.box_high * 100
        gap_lo = (row["close"] - state.box_low) / state.box_low * 100
        out.append({"key": "c2_box", "label": "C2 박스 상/하단", "met": True,
                    "detail": f"상단 {state.box_high:,.1f}({gap_hi:+.2f}%) / "
                              f"하단 {state.box_low:,.1f}({gap_lo:+.2f}%)"})
        if state.state == "BREAKOUT_PENDING":
            out.append({"key": "c2_dir", "label": "돌파 방향", "met": True, "detail": state.breakout_side})
    return out


def conditions_c2_live(rows: list, state: C2State, cache: dict, live_price: float,
                        live_oi: float, p: ParamsC) -> list[dict]:
    """conditions_c2()의 실시간(체결 틱) 버전 — conditions_c1_live()와 동일한 목적.
    거래량 수축 조건은 진행 중인 봉의 거래량을 추적하지 않으므로 마지막 확정치를 쓴다."""
    if state.state != "IDLE":
        return cache.get("last_conditions") or [
            {"key": "c2_state", "label": f"C2 상태: {state.state}", "met": True, "detail": state.state}]

    out = [{"key": "c2_state", "label": "C2 상태: IDLE", "met": False, "detail": "IDLE"}]
    if len(rows) < p.c2_coil_window_bars:
        out.append({"key": "c2_range", "label": f"레인지 응축(하위{p.c2_coil_range_quantile*100:.0f}%)",
                    "met": False, "detail": "히스토리 부족"})
        return out

    window = rows[-p.c2_coil_window_bars:]
    win_high = max(r["high"] for r in window)
    win_low = min(r["low"] for r in window)
    if live_price is not None:
        win_high = max(win_high, live_price)
        win_low = min(win_low, live_price)
    realized_range = (win_high - win_low) / live_price if live_price else None
    rr_gate = cache.get("realized_range_c2_qlo")

    base_oi = window[0].get("oi")
    oi_trend_now = ((live_oi - base_oi) / base_oi) if base_oi and live_oi is not None else None
    oi_gate = cache.get("oi_trend_c2_qhi")

    vol_med_now = cache.get("vol_med_c2")     # 진행 중인 봉의 거래량은 아직 모르므로 마지막 확정치
    vol_gate = cache.get("vol_med_c2_qlo")

    out.append({"key": "c2_range", "label": f"레인지 응축(하위{p.c2_coil_range_quantile*100:.0f}%)",
                "met": bool(realized_range is not None and rr_gate is not None
                            and np.isfinite(rr_gate) and realized_range <= rr_gate),
                "detail": f"{realized_range*100:.2f}%" if realized_range is not None else "N/A"})
    out.append({"key": "c2_oi", "label": f"OI 축적(상위{(1-p.c2_oi_rise_quantile)*100:.0f}%)",
                "met": bool(oi_trend_now is not None and oi_gate is not None
                            and np.isfinite(oi_gate) and oi_trend_now >= oi_gate),
                "detail": f"{oi_trend_now*100:.2f}%" if oi_trend_now is not None else "N/A"})
    out.append({"key": "c2_vol", "label": f"거래량 수축(하위{p.c2_vol_contraction_quantile*100:.0f}%)",
                "met": bool(vol_med_now is not None and vol_gate is not None
                            and np.isfinite(vol_med_now) and np.isfinite(vol_gate) and vol_med_now <= vol_gate),
                "detail": f"{vol_med_now:.2f}" if vol_med_now is not None and np.isfinite(vol_med_now) else "N/A"})
    return out
