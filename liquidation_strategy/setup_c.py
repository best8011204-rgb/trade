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
