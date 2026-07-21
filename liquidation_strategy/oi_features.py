"""Setup C(Price x OI 상태 분류기) 공용 피처 프리미티브.

전부 상태 없는 순수 함수. 하드코딩 상수 대신 rolling_quantile()로 임계값을
"최근 N봉 분포의 q-분위수"로 잡는 걸 표준으로 삼는다(레짐 강건성, 자유
파라미터 축소). C1(setup_c.py의 OI-Flush Reversal)과 C2(Coil-Break)가
공유한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ATR_WINDOW_DEFAULT = 14


def atr(df: pd.DataFrame, window: int = ATR_WINDOW_DEFAULT) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def oi_change_pct(oi: pd.Series, n: int) -> pd.Series:
    """DeltaOI%_n = (OI_t - OI_{t-n}) / OI_{t-n}."""
    return oi.pct_change(n)


def rvol(volume: pd.Series, window: int) -> pd.Series:
    """RVOL = vol_t / rolling median(vol) — 절대치 대신 중앙값 대비 상대거래량."""
    med = volume.rolling(window).median().replace(0, np.nan)
    return volume / med


def clv(df: pd.DataFrame) -> pd.Series:
    """Close Location Value: (close-low)/(high-low). 범위 0이면 중립(0.5)."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


def wick_asymmetry(df: pd.DataFrame, cap: float = 100.0) -> pd.Series:
    """lower_wick / upper_wick. 둘 다 0(완전 바디 캔들)이면 중립(1.0),
    upper_wick만 0이면 저점매수거부가 극단적으로 강하다는 뜻이라 cap으로 클리핑."""
    body_hi = df[["open", "close"]].max(axis=1)
    body_lo = df[["open", "close"]].min(axis=1)
    lower_wick = (body_lo - df["low"]).clip(lower=0)
    upper_wick = (df["high"] - body_hi).clip(lower=0)
    ratio = lower_wick / upper_wick.replace(0, np.nan)
    both_zero = upper_wick.eq(0) & lower_wick.eq(0)
    ratio = ratio.where(~both_zero, 1.0)
    return ratio.fillna(cap).clip(upper=cap)


def body_range_ratio(df: pd.DataFrame) -> pd.Series:
    """|close-open| / (high-low) — 결정력(추세) vs 인디시전(도지류)."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["open"]).abs() / rng).fillna(0.0)


def range_expansion(df: pd.DataFrame, atr_window: int = ATR_WINDOW_DEFAULT) -> pd.Series:
    """(high-low)/ATR_n — 클라이맥스(레인지 확장) 판정."""
    a = atr(df, atr_window).replace(0, np.nan)
    return (df["high"] - df["low"]) / a


def rolling_quantile(series: pd.Series, window: int, q: float) -> pd.Series:
    """series의 최근 window봉 분포에서 q-분위수 '값'을 반환한다(비교용 임계값).
    q=0.05 -> 하위 5% 값, q=0.95 -> 상위 5%(=95번째 백분위) 값."""
    return series.rolling(window).quantile(q)


def merge_oi_asof(df_ts: pd.Series, oi_ts: np.ndarray, oi_val: np.ndarray) -> np.ndarray:
    """df_ts(오름차순 epoch seconds) 각 시각에 '그 시각 이하의 가장 최근' OI 값을
    붙인다(asof/backward join). oi_ts/oi_val은 정렬 여부 무관(내부에서 정렬)."""
    order = np.argsort(np.asarray(oi_ts))
    oi_ts_sorted = np.asarray(oi_ts)[order]
    oi_val_sorted = np.asarray(oi_val)[order]
    idx = np.searchsorted(oi_ts_sorted, df_ts.to_numpy(), side="right") - 1
    out = np.where(idx >= 0, oi_val_sorted[np.clip(idx, 0, len(oi_val_sorted) - 1)], np.nan)
    return out
