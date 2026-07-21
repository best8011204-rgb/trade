# Setup C: Price×OI 상태 분류기로 재작성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** forceOrder(청산 틱) 없이, kline + openInterestHist만으로 완전히 백테스트 가능한 Setup C(OI-Flush Reversal 롱 + Coil-Break OI필터 양방향)로 기존 OU 평균회귀 Setup C를 완전히 교체한다.

**Architecture:** `setup_c.py`는 두 개의 독립 상태머신(`C1State`/`advance_c1` = 청산 캐스케이드 소진 롱, `C2State`/`advance_c2` = 코일 돌파 양방향)을 pandas DataFrame + 정수 인덱스 `i` 위에서 bar-by-bar로 구동하는 순수 함수 집합으로 남긴다(기존 setup_c.py 스타일 유지, StrategyEngine 무관). `oi_features.py`가 두 상태머신이 공유하는 피처(ΔOI%, RVOL, CLV, 윅비대칭, 바디/레인지, 레인지확장)와 롤링 분위수 계산을 담당한다. `backtest_c.py`가 OI 백필(REST 30일 + 라이브 누적 로그)과 트랜치(TP1/TP2 분할) 원장을 담당하고, `live_setup_c.py`가 동일 로직을 5분봉 스트리밍으로 구동해 GUI(A/B와 동일 인터페이스)에 연결한다.

**Tech Stack:** Python 3.12, pandas, numpy, 기존 `requests` 기반 `binance_client.py`. 신규 외부 의존성 없음.

## Global Constraints

- 이 저장소엔 테스트 프레임워크(pytest 등)가 없다 — 새로 추가하지 않는다. 각 Task의 검증은 `python -c "..."` 인라인 스모크 체크(assert 기반)로 하고, 실패 시 트레이스백이 곧 실패 신호다. 이는 기존 `backtest_c.py --days N` CLI 자체가 "테스트"인 프로젝트 관행을 따른 것이다.
- 모든 신규/수정 dataclass 필드는 `bool|int|float|str` 원시 타입만 쓴다 — `gui/views/settings_page.py`의 `ParamGroup`이 `dataclasses.fields()`로 자동 폼을 만들며 `_TYPE_NAME_MAP`에 없는 타입(tuple 등)은 조용히 깨진다(문자열로 오염). 튜플/리스트가 필요한 고정 상수는 dataclass 필드가 아니라 모듈 상수로 둔다.
- `ParamsC`는 **하나의 flat dataclass**로 유지한다(중첩 dataclass 금지 — 같은 이유로 GUI 폼이 깨진다). C1 전용 필드는 `c1_` 접두, C2 전용은 `c2_` 접두, 공유 필드는 접두 없음.
- 왕복 비용은 기존 관행대로 `COST_BPS = 10.0`, 매 청산(트랜치 포함) 시점마다 차감한다(engine.py `_close()`와 동일 관례).
- Setup 3(OI 다이버전스 소진)은 이번 계획의 범위 밖이다 — 사용자 스펙 본인이 "우선순위 최하위"로 명시했다. 구현하지 않는다.
- DSR/PBO/순열검정/IS-OOS 프레임워크(`replay.py` 등)는 현재 이 저장소에 존재하지 않는다 — 이번 계획은 그것을 새로 만들지 않는다. 기존 `report_c.py` 스타일(BE-WR/최소표본 기각조건)만 새 통계 구조에 맞춰 갱신한다. (사용자가 실제로 DSR/PBO 신규 구축을 원하면 별도 계획으로 분리해야 한다 — Task 10 완료 후 확인 필요.)
- 기존 Setup A/B(`setup_a.py`/`setup_b.py`/`engine.py`)는 무수정이다.

## File Structure

- `liquidation_strategy/oi_features.py` (신규) — ATR/RVOL/CLV/윅비대칭/바디레인지/레인지확장/OI변화율/롤링분위수/OI asof 병합. C1·C2 공용, 상태 없는 순수 함수만.
- `liquidation_strategy/setup_c.py` (전면 재작성) — `ParamsC`(flat), `C1State`/`advance_c1`/`conditions_c1`, `C2State`/`advance_c2`/`conditions_c2`, 공용 `tranche_exit_step()`, `compute_features()`. 기존 OU 코드(VR/half-life/z-score) 전부 삭제.
- `liquidation_strategy/backtest_c.py` (재작성) — `fetch_5m_with_oi()`(OI 병합 백필), `Ledger`(트랜치 지원), `run_backtest_c1()`/`run_backtest_c2()`. 기존 `compare_toggles`(OU 전용 F1/F2/VR on-off)는 제거.
- `liquidation_strategy/report_c.py` (수정) — 기각조건을 half-life 개념 대신 새 통계 필드(`avg_bars_held`/`time_exit_bars` 비율, OI게이트 기여도 on/off)로 갱신.
- `liquidation_strategy/live_feed.py` (소폭 수정) — `on_oi_poll`이 매 5분 폴링을 `logs/oi_history.jsonl`에 append(30일 REST 한계를 넘는 장기 백테스트용 누적, 사용자가 이미 있다고 착각했던 부분 — 실제로 지금 추가한다).
- `liquidation_strategy/live_setup_c.py` (재작성) — `LiveSetupCRunner`가 C1/C2 두 상태머신 + 두 원장을 관리. GUI 대상 공개 인터페이스(`on_confirmed_5m_candle`/`stop`/`restart`/`open_legs_view`/`describe`/`summary`/`combined_summary`/`last_conditions`)는 시그니처 그대로 유지(계약 불변), 내부만 교체. 신규 `on_oi(oi_value, ts)` 메서드 추가.
- `gui/live_engine_bridge.py` (소폭 수정) — `on_oi_poll` 후크에서 `self.c_runner.on_oi(...)` 호출 한 줄 추가.
- `gui/views/settings_page.py` (소폭 수정) — Setup C 그룹 라벨 텍스트만 갱신(`ParamsC` 필드는 자동 반영이라 폼 코드 변경 불필요).
- `gui/views/dashboard_page.py` (소폭 수정) — `trigger_c` `max_rows`를 늘어난 조건 개수에 맞게 조정.

---

### Task 1: `oi_features.py` — 공용 피처 프리미티브

**Files:**
- Create: `liquidation_strategy/oi_features.py`

**Interfaces:**
- Produces: `atr(df, window=14) -> pd.Series`, `oi_change_pct(oi: pd.Series, n: int) -> pd.Series`, `rvol(volume: pd.Series, window: int) -> pd.Series`, `clv(df) -> pd.Series`, `wick_asymmetry(df, cap=100.0) -> pd.Series`, `body_range_ratio(df) -> pd.Series`, `range_expansion(df, atr_window=14) -> pd.Series`, `rolling_quantile(series: pd.Series, window: int, q: float) -> pd.Series`, `merge_oi_asof(df_ts: pd.Series, oi_ts: np.ndarray, oi_val: np.ndarray) -> np.ndarray`.

- [ ] **Step 1: 파일 작성**

```python
"""Setup C(Price×OI 상태 분류기) 공용 피처 프리미티브.

전부 상태 없는 순수 함수. 하드코딩 상수 대신 rolling_quantile()로 임계값을
"최근 N봉 분포의 q-분위수"로 잡는 걸 표준으로 삼는다(레짐 강건성, 자유
파라미터 축소). C1(setup_c.OIFlushReversalLong)과 C2(CoilBreakOIFilter)가
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
```

- [ ] **Step 2: 스모크 체크 실행**

```bash
python -c "
import numpy as np, pandas as pd
from liquidation_strategy import oi_features as f

df = pd.DataFrame({
    'open':  [100, 101, 99, 95, 96],
    'high':  [102, 103, 100, 97, 98],
    'low':   [99, 98, 95, 93, 95],
    'close': [101, 99, 95, 96, 97],
    'volume':[10, 12, 50, 60, 15],
})

a = f.atr(df, window=3)
assert a.isna().sum() == 2, 'ATR 워밍업 NaN 개수'
assert a.iloc[-1] > 0

rv = f.rvol(df['volume'], window=3)
assert rv.iloc[-1] > 0

c = f.clv(df)
assert (c.between(0, 1)).all(), 'CLV는 0~1 사이여야 함'
assert abs(c.iloc[0] - (101-99)/(102-99)) < 1e-9

w = f.wick_asymmetry(df)
assert (w >= 0).all() and (w <= 100.0).all()

br = f.body_range_ratio(df)
assert (br.between(0, 1)).all()

re = f.range_expansion(df, atr_window=3)
assert re.iloc[-1] > 0

oi = pd.Series([1000, 1010, 1005, 990, 1020])
chg = f.oi_change_pct(oi, 2)
assert abs(chg.iloc[2] - (1005-1000)/1000) < 1e-9

rq = f.rolling_quantile(df['close'], window=3, q=0.5)
assert not rq.isna().all()

ts = pd.Series([0, 60, 120, 180, 400])
oi_ts = np.array([0, 100, 200, 300])
oi_val = np.array([1.0, 2.0, 3.0, 4.0])
merged = f.merge_oi_asof(ts, oi_ts, oi_val)
assert list(merged) == [1.0, 1.0, 2.0, 2.0, 4.0], merged

print('oi_features.py OK')
"
```

Expected: `oi_features.py OK` (AssertionError나 트레이스백이 뜨면 해당 함수 로직을 고칠 것 — 이 단계에선 아직 실패할 대상이 없다, 순수 유틸이라 구현과 검증을 같은 스텝에 둔다).

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/oi_features.py
git commit -m "feat: add shared Price x OI feature primitives for Setup C"
```

---

### Task 2: `setup_c.py` — `ParamsC`(flat) + 피처 병합 + 트랜치 청산 공용 함수

**Files:**
- Modify: `liquidation_strategy/setup_c.py` (전체 교체 — 기존 OU 코드/`ParamsC`/`SignalC`/`evaluate_bar`/`conditions`/`should_exit`/`variance_ratio`/`ar1_half_life`/`atr` 전부 제거)

**Interfaces:**
- Consumes: `liquidation_strategy.oi_features`의 전체 함수 (Task 1).
- Produces: `ParamsC`(dataclass, flat, 원시타입만), `compute_features(df: pd.DataFrame, p: ParamsC) -> pd.DataFrame`(입력 df는 `ts,open,high,low,close,volume,oi` 컬럼 + `DatetimeIndex`), `tranche_exit_step(side: str, pos: dict, high: float, low: float, close: float, bars_held: int, time_exit_bars: int) -> tuple[str|None, float|None, float|None]`. Task 3/4가 이 세 가지를 가져다 쓴다.

- [ ] **Step 1: 파일 헤더 + `ParamsC` 작성 (기존 파일 내용을 이걸로 전부 교체)**

```python
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

# ΔOI% 다중 윈도우(5/15/60분 @ 5분봉) — 튜닝 대상이 아닌 고정 피처 정의라
# dataclass 필드가 아니라 모듈 상수로 둔다(ParamGroup 폼이 tuple을 못 다룸).
OI_CHANGE_WINDOWS_BARS = (1, 3, 12)  # 5m, 15m, 60m


@dataclass
class ParamsC:
    # --- 공유 ---
    atr_window: int = 14
    quantile_lookback_bars: int = 2016   # 5m x 2016 = 7일. 분위수 게이트의 롤링 창.

    # --- C1: OI-Flush Reversal Long (탐지 knob 3개: price_drop/oi_drop/rvol) ---
    c1_price_drop_atr_mult: float = 2.0      # N봉 하락폭이 ATR의 몇 배 이상이어야 캐스케이드 후보인가
    c1_oi_drop_quantile: float = 0.05        # DeltaOI%(15m)가 최근 분포 하위 몇 %여야 디레버리징으로 보는가
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
```

- [ ] **Step 2: `compute_features()` 추가 (같은 파일, `ParamsC` 아래)**

```python
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

    # C1 게이트: 15분(w2) 창을 캐스케이드 확인의 기본 OI 변화율로 쓴다.
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
```

- [ ] **Step 3: 스모크 체크**

```bash
python -c "
import numpy as np, pandas as pd
from liquidation_strategy.setup_c import ParamsC, compute_features, tranche_exit_step

n = 3000
rng = np.random.default_rng(0)
ts = pd.Series(np.arange(n) * 300.0)
close = 100 + np.cumsum(rng.normal(0, 0.3, n))
df = pd.DataFrame({
    'ts': ts,
    'open': close, 'high': close + 0.5, 'low': close - 0.5, 'close': close,
    'volume': rng.uniform(1, 10, n),
    'oi': 1000 + np.cumsum(rng.normal(0, 2, n)),
})
df.index = pd.to_datetime(df['ts'], unit='s')

p = ParamsC()
out = compute_features(df, p)
for col in ('atr','rvol','clv','wick_asym','oi_chg_c1','oi_chg_c1_qlo','rvol_qhi',
            'realized_range_c2','realized_range_c2_qlo','oi_trend_c2_qhi'):
    assert col in out.columns, col
assert out['atr'].iloc[-1] > 0

pos = {'stop': 95.0, 'tp1': 105.0, 'tp2': 110.0, 'tp1_fraction': 0.5, 'tp1_hit': False}
r, px, frac = tranche_exit_step('long', pos, high=106, low=99, close=104, bars_held=1, time_exit_bars=999)
assert r == 'TP1' and frac == 0.5 and pos['tp1_hit'] is True
r, px, frac = tranche_exit_step('long', pos, high=111, low=100, close=110, bars_held=2, time_exit_bars=999)
assert r == 'TP2' and abs(frac - 0.5) < 1e-9

print('setup_c.py Task2 OK')
"
```

Expected: `setup_c.py Task2 OK`.

- [ ] **Step 4: Commit**

```bash
git add liquidation_strategy/setup_c.py
git commit -m "refactor: replace OU Setup C params with flat Price x OI ParamsC + shared feature/exit helpers"
```

---

### Task 3: `setup_c.py` — C1 상태머신 (OI-Flush Reversal Long)

**Files:**
- Modify: `liquidation_strategy/setup_c.py` (append)

**Interfaces:**
- Consumes: Task 2의 `ParamsC`, `compute_features()` 출력 컬럼들.
- Produces: `SignalC1`(dataclass), `C1State`(dataclass), `advance_c1(feat_df, i, state, p) -> tuple[C1State, SignalC1|None]`, `conditions_c1(feat_df, i, state, p) -> list[dict]`.

- [ ] **Step 1: 코드 추가**

```python
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
```

- [ ] **Step 2: 스모크 체크 — 인위적 캐스케이드+소진 시퀀스로 신호가 나오는지 확인**

```bash
python -c "
import numpy as np, pandas as pd
from liquidation_strategy.setup_c import ParamsC, compute_features, advance_c1, C1State, conditions_c1

p = ParamsC(quantile_lookback_bars=200, atr_window=14, c1_cascade_window_bars=6,
            c1_exhaustion_confirm_bars=2)
n = 400
rng = np.random.default_rng(1)
close = 100 + np.cumsum(rng.normal(0, 0.05, n))
oi = 1000 + np.cumsum(rng.normal(0, 0.5, n))
high = close + 0.2
low = close - 0.2
vol = rng.uniform(1, 3, n)

# 300~306봉: 급락 + OI 급감 + 거래량 폭증(캐스케이드)
crash_start = 300
for k in range(7):
    close[crash_start+k] = close[crash_start-1] - (k+1) * 2.0
    oi[crash_start+k] = oi[crash_start-1] - (k+1) * 15.0
    vol[crash_start+k] = 40.0
    high[crash_start+k] = close[crash_start+k] + 0.3
    low[crash_start+k] = close[crash_start+k] - 3.0  # 긴 하단 윅 준비

# 307~309봉: 소진(반등, 하단윅 긴 반전캔들, OI 감속)
for k in range(3):
    idx = crash_start + 7 + k
    close[idx] = close[crash_start+6] + 1.0
    oi[idx] = oi[crash_start+6] + 2.0
    vol[idx] = 5.0
    low[idx] = close[crash_start+6] - 5.0   # 긴 하단 윅
    high[idx] = close[idx] + 0.2

df = pd.DataFrame({'ts': np.arange(n)*300.0, 'open': close, 'high': high, 'low': low,
                    'close': close, 'volume': vol, 'oi': oi})
df.index = pd.to_datetime(df['ts'], unit='s')
f = compute_features(df, p)

state = C1State()
signal = None
for i in range(len(f)):
    state, sig = advance_c1(f, i, state, p)
    if sig is not None:
        signal = sig
        break

assert signal is not None, 'C1 신호가 발생해야 한다(인위적 캐스케이드+소진 시퀀스)'
assert signal.stop < signal.entry < signal.tp1 < signal.tp2
print('C1 signal:', signal)
print('setup_c.py Task3 (C1) OK')
"
```

Expected: `setup_c.py Task3 (C1) OK` — 신호가 안 나오면 합성 시퀀스의 낙폭/OI낙폭/RVOL이 `quantile_lookback_bars=200` 창의 분위수 게이트를 못 넘은 것이니, 합성 데이터의 crash 강도(현재 −2.0/봉, vol=40)를 키워서 재확인. 로직 자체(상태 전이 조건)는 바꾸지 말 것.

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/setup_c.py
git commit -m "feat: implement C1 OI-Flush Reversal Long state machine"
```

---

### Task 4: `setup_c.py` — C2 상태머신 (Coil-Break with OI Filter)

**Files:**
- Modify: `liquidation_strategy/setup_c.py` (append)

**Interfaces:**
- Produces: `SignalC2`(dataclass, `side`/`kind` 필드 포함), `C2State`(dataclass), `advance_c2(feat_df, i, state, p) -> tuple[C2State, SignalC2|None]`, `conditions_c2(feat_df, i, state, p) -> list[dict]`.

- [ ] **Step 1: 코드 추가**

```python
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
    elif state.state == "BREAKOUT_PENDING":
        out.append({"key": "c2_dir", "label": "돌파 방향", "met": True, "detail": state.breakout_side})
    return out
```

- [ ] **Step 2: 스모크 체크**

```bash
python -c "
import numpy as np, pandas as pd
from liquidation_strategy.setup_c import ParamsC, compute_features, advance_c2, C2State

p = ParamsC(quantile_lookback_bars=200, atr_window=14, c2_coil_window_bars=20)
n = 400
rng = np.random.default_rng(2)

# 사전 히스토리는 평범한 변동성의 랜덤워크(코일이 '압축'으로 보이려면
# 기준 분포에 변동성이 있어야 한다 — 완전히 평평하면 코일 쪽이 오히려
# '더 넓은' 레인지가 되어버려 압축 게이트가 절대 통과하지 않는다).
close = 100 + np.cumsum(rng.normal(0, 0.1, n))
oi = 1000 + np.cumsum(rng.normal(0, 1.0, n))
vol = rng.uniform(1, 5, n)
high = close + rng.uniform(0.05, 0.3, n)
low = close - rng.uniform(0.05, 0.3, n)

coil_start = 250
coil_len = 20
base = close[coil_start-1]
for k in range(coil_len):
    close[coil_start+k] = base + rng.normal(0, 0.01)   # 사전 히스토리보다 훨씬 좁은 변동
    oi[coil_start+k] = oi[coil_start-1] + k * 3.0       # 코일 동안 OI 꾸준히 증가
    vol[coil_start+k] = rng.uniform(0.2, 0.4)           # 거래량 수축
    high[coil_start+k] = close[coil_start+k] + 0.02
    low[coil_start+k] = close[coil_start+k] - 0.02

breakout = coil_start + coil_len
box_high_val = max(high[coil_start:breakout])
for k in range(3):   # 돌파봉 + 후속 2봉, 전부 돌파 방향으로 이어짐(판정 보류 구간 대비)
    idx = breakout + k
    close[idx] = box_high_val + 3.0 + k * 0.5
    high[idx] = close[idx] + 0.2
    low[idx] = box_high_val + 0.1
    oi[idx] = oi[breakout - 1] + 20.0 + k * 5.0         # 돌파 직후 OI 계속 증가
    vol[idx] = 40.0                                      # 거래량 팽창 -> 모멘텀 조건

df = pd.DataFrame({'ts': np.arange(n)*300.0, 'open': close, 'high': high, 'low': low,
                    'close': close, 'volume': vol, 'oi': oi})
df.index = pd.to_datetime(df['ts'], unit='s')
f = compute_features(df, p)

state = C2State()
signal = None
for i in range(len(f)):
    state, sig = advance_c2(f, i, state, p)
    if sig is not None:
        signal = sig
        break

assert signal is not None, 'C2 모멘텀 신호가 발생해야 한다'
assert signal.side == 'long' and signal.kind == 'momentum'
assert signal.stop < signal.entry < signal.tp1 < signal.tp2
print('C2 signal:', signal)
print('setup_c.py Task4 (C2) OK')
"
```

Expected: `setup_c.py Task4 (C2) OK`. 신호가 안 나오면(코일 인식 실패 등) 합성 시퀀스의 코일 응축 강도/OI 증가폭을 조정할 것 — 상태전이 조건 자체는 스펙대로 유지.

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/setup_c.py
git commit -m "feat: implement C2 Coil-Break with OI Filter bidirectional state machine"
```

---

### Task 5: `backtest_c.py` — OI 병합 백필 + 트랜치 원장 + C1/C2 백테스트

**Files:**
- Modify: `liquidation_strategy/backtest_c.py` (전체 교체)

**Interfaces:**
- Consumes: `setup_c.ParamsC`, `compute_features`, `advance_c1`, `advance_c2`, `C1State`, `C2State`, `tranche_exit_step`, `binance_client.get_klines_range`, `binance_client.get_open_interest_hist_range`, `oi_features.merge_oi_asof`.
- Produces: `fetch_5m_with_oi(symbol, days, oi_log_path=None) -> pd.DataFrame`, `run_backtest_c1(days, symbol, params, df) -> dict`, `run_backtest_c2(days, symbol, params, df) -> dict`.

- [ ] **Step 1: 파일 전체 교체**

```python
"""Setup C(Price x OI 상태 분류기) 독립 백테스트 — StrategyEngine과 분리된 원장.

OI 병목: openInterestHist는 5분 granularity로 최근 30일만 공개 제공한다.
그 이상은 live_feed.py가 실시간으로 logs/oi_history.jsonl에 누적한 기록으로
보충한다(Task 7) — 이 파일의 fetch_5m_with_oi()가 REST(최근 30일)와 로컬
누적 로그를 병합해 가능한 만큼 확장한다. 캔들 로직 자체는 klines만으로
수년치 검증 가능하니 문제 없고, OI 의존 부분만 이 한계를 받는다.

실행:
    python3 -m liquidation_strategy.backtest_c --setup c1 --days 30
    python3 -m liquidation_strategy.backtest_c --setup c2 --days 30
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

import numpy as np
import pandas as pd

from . import binance_client as bc
from . import oi_features as feat
from .setup_c import (
    ParamsC, compute_features, advance_c1, advance_c2, C1State, C2State, tranche_exit_step,
)

SYMBOL = "BTCUSDT"
COST_BPS = 10.0
OI_REST_MAX_DAYS = 30


def _load_local_oi_log(path: str) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return pd.DataFrame(columns=["ts", "oi"])
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                rows.append({"ts": float(d["ts"]), "oi": float(d["oi"])})
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return pd.DataFrame(rows)


def fetch_5m_with_oi(symbol: str = SYMBOL, days: int = 30,
                      oi_log_path: str = "logs/oi_history.jsonl") -> pd.DataFrame:
    """klines(5m, days일치)에 OI를 asof 병합해 반환. columns=[ts,open,high,low,close,volume,oi]."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    raw = bc.get_klines_range(symbol, "5m", start_ms, end_ms)
    if not raw:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi"])
    kdf = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "n_trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        kdf[col] = kdf[col].astype(float)
    kdf["ts"] = kdf["open_time"] / 1000.0
    kdf = kdf[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)

    oi_rest_start = max(start_ms, end_ms - OI_REST_MAX_DAYS * 86_400_000)
    oi_hist = bc.get_open_interest_hist_range(symbol, "5m", oi_rest_start, end_ms)
    oi_rows = [{"ts": float(o["timestamp"]) / 1000.0, "oi": float(o["sumOpenInterest"])} for o in oi_hist]
    oi_df = pd.DataFrame(oi_rows, columns=["ts", "oi"])

    local_oi = _load_local_oi_log(oi_log_path)
    oi_all = pd.concat([local_oi, oi_df], ignore_index=True)
    if oi_all.empty:
        kdf["oi"] = np.nan
    else:
        oi_all = oi_all.drop_duplicates(subset="ts").sort_values("ts")
        kdf["oi"] = feat.merge_oi_asof(kdf["ts"], oi_all["ts"].to_numpy(), oi_all["oi"].to_numpy())

    kdf.index = pd.to_datetime(kdf["ts"], unit="s")
    return kdf


def _bps(entry: float, exit_price: float, side: str, cost_bps: float = COST_BPS) -> float:
    pnl = (exit_price - entry) if side == "long" else (entry - exit_price)
    return (pnl / entry) * 10000.0 - cost_bps


def _r_multiple(entry: float, stop: float, exit_price: float, side: str) -> float:
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    pnl = (exit_price - entry) if side == "long" else (entry - exit_price)
    return pnl / risk


class Ledger:
    """트랜치(TP1 부분청산) 지원 원장. C1/C2 공용 — side는 트레이드마다 다를 수 있다(C2)."""

    def __init__(self, name: str):
        self.name = name
        self.pos = None
        self.cooldown_until_bar = None
        self.reentry_count = 0
        self.trades: list[dict] = []

    def can_enter(self, i: int) -> bool:
        if self.pos is not None:
            return False
        if self.cooldown_until_bar is not None and i < self.cooldown_until_bar:
            return False
        return True

    def open(self, side: str, sig, i: int, extra: dict = None):
        self.pos = {
            "side": side, "entry": sig.entry, "stop": sig.stop, "tp1": sig.tp1, "tp2": sig.tp2,
            "tp1_fraction": sig.tp1_fraction, "tp1_hit": False, "bars_held": 0,
            "entry_ts": sig.ts, "entry_i": i, "time_exit_bars": sig.time_exit_bars,
            "extra": extra or {},
        }

    def step(self, i: int, high: float, low: float, close: float, ts, max_reentries: int):
        if self.pos is None:
            return None
        self.pos["bars_held"] += 1
        reason, exit_price, frac = tranche_exit_step(
            self.pos["side"], self.pos, high, low, close, self.pos["bars_held"], self.pos["time_exit_bars"])
        if reason is None:
            return None
        trade = {
            "side": self.pos["side"], "entry_ts": self.pos["entry_ts"], "entry_price": self.pos["entry"],
            "exit_ts": ts, "exit_price": exit_price, "reason": reason,
            "bars_held": self.pos["bars_held"], "qty_fraction": frac,
            "bps": _bps(self.pos["entry"], exit_price, self.pos["side"]),
            "r_multiple": _r_multiple(self.pos["entry"], self.pos["stop"], exit_price, self.pos["side"]),
            **self.pos["extra"],
        }
        self.trades.append(trade)
        if reason == "TP1":
            return trade  # 잔여 트랜치 보유, pos 유지
        if reason == "SL" and self.reentry_count < max_reentries:
            self.reentry_count += 1
            self.cooldown_until_bar = i + 1
        else:
            self.reentry_count = 0
            self.cooldown_until_bar = None
        self.pos = None
        return trade


def _summary(trades: list[dict]) -> dict:
    if not trades:
        return {"count": 0}
    wins = [t for t in trades if t["bps"] > 0]
    losses = [t for t in trades if t["bps"] <= 0]
    be_wr = None
    if wins and losses:
        avg_win = sum(t["bps"] for t in wins) / len(wins)
        avg_loss = sum(-t["bps"] for t in losses) / len(losses)
        if avg_win + avg_loss > 0:
            be_wr = avg_loss / (avg_win + avg_loss)
    return {
        "count": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_bps": sum(t["bps"] for t in trades) / len(trades),
        "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
        "avg_bars_held": sum(t["bars_held"] for t in trades) / len(trades),
        "breakeven_win_rate": be_wr,
        "max_consec_losses": _max_consec_losses(trades),
    }


def _max_consec_losses(trades: list[dict]) -> int:
    m = cur = 0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        if t["bps"] <= 0:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m


def run_backtest_c1(days: int = 30, symbol: str = SYMBOL, params: ParamsC = None,
                     df: pd.DataFrame = None) -> dict:
    p = params or ParamsC()
    if df is None:
        df = fetch_5m_with_oi(symbol, days)
    if df.empty:
        raise RuntimeError("5분봉+OI 수집 실패 — 네트워크/레이트리밋 확인")
    f = compute_features(df, p)

    ledger = Ledger("C1")
    state = C1State()
    rejection_counts: Counter = Counter()

    for i in range(len(f)):
        row = f.iloc[i]
        ledger.step(i, row["high"], row["low"], row["close"], f.index[i], p.c1_max_reentries)
        state, sig = advance_c1(f, i, state, p)
        if sig is None:
            continue
        if ledger.can_enter(i):
            ledger.open("long", sig, i)
        else:
            rejection_counts["position_busy"] += 1

    stats = _summary(ledger.trades)
    return {
        "meta": {"symbol": symbol, "days": days, "n_bars": len(f), "params": p.__dict__,
                 "roundtrip_cost_bps": COST_BPS, "setup": "C1"},
        "stats": stats, "trades": ledger.trades, "rejection_counts": dict(rejection_counts),
    }


def run_backtest_c2(days: int = 30, symbol: str = SYMBOL, params: ParamsC = None,
                     df: pd.DataFrame = None) -> dict:
    p = params or ParamsC()
    if df is None:
        df = fetch_5m_with_oi(symbol, days)
    if df.empty:
        raise RuntimeError("5분봉+OI 수집 실패 — 네트워크/레이트리밋 확인")
    f = compute_features(df, p)

    ledger = Ledger("C2")
    state = C2State()
    rejection_counts: Counter = Counter()

    for i in range(len(f)):
        row = f.iloc[i]
        ledger.step(i, row["high"], row["low"], row["close"], f.index[i], p.c2_max_reentries)
        state, sig = advance_c2(f, i, state, p)
        if sig is None:
            continue
        if ledger.can_enter(i):
            ledger.open(sig.side, sig, i, extra={"kind": sig.kind})
        else:
            rejection_counts["position_busy"] += 1

    stats = _summary(ledger.trades)
    return {
        "meta": {"symbol": symbol, "days": days, "n_bars": len(f), "params": p.__dict__,
                 "roundtrip_cost_bps": COST_BPS, "setup": "C2"},
        "stats": stats, "trades": ledger.trades, "rejection_counts": dict(rejection_counts),
    }


def main():
    ap = argparse.ArgumentParser(description="Setup C(Price x OI) 독립 백테스트")
    ap.add_argument("--setup", choices=["c1", "c2"], required=True)
    ap.add_argument("--days", type=int, default=30, help="OI는 REST 30일 한도 + 로컬 누적 로그")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--oi-log", default="logs/oi_history.jsonl")
    args = ap.parse_args()

    print(f"[1/2] {args.symbol} 5분봉+OI {args.days}일 수집...", file=sys.stderr)
    df = fetch_5m_with_oi(args.symbol, args.days, args.oi_log)
    print(f"[2/2] Setup {args.setup.upper()} 백테스트 실행 ({len(df)}봉)...", file=sys.stderr)
    runner = run_backtest_c1 if args.setup == "c1" else run_backtest_c2
    result = runner(days=args.days, symbol=args.symbol, df=df)
    print(result["stats"])
    print("rejection_counts:", result["rejection_counts"])


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 스모크 체크(네트워크 없이 합성 df로 — 이 저장소 개발 샌드박스는 바이낸스 아웃바운드가 막혀 있으므로 fetch 함수 자체는 로컬 CLI 실행 시 실기(사용자 PC)에서만 검증 가능. 여기서는 `run_backtest_c1/c2`가 미리 만든 df로 정상 완주하는지만 확인)**

```bash
python -c "
import numpy as np, pandas as pd
from liquidation_strategy.setup_c import ParamsC
from liquidation_strategy.backtest_c import run_backtest_c1, run_backtest_c2

n = 3000
rng = np.random.default_rng(3)
close = 100 + np.cumsum(rng.normal(0, 0.05, n))
df = pd.DataFrame({
    'ts': np.arange(n) * 300.0,
    'open': close, 'high': close + rng.uniform(0.05, 0.3, n),
    'low': close - rng.uniform(0.05, 0.3, n), 'close': close,
    'volume': rng.uniform(1, 10, n),
    'oi': 1000 + np.cumsum(rng.normal(0, 1.0, n)),
})
df.index = pd.to_datetime(df['ts'], unit='s')

p = ParamsC(quantile_lookback_bars=200)
r1 = run_backtest_c1(df=df, params=p)
r2 = run_backtest_c2(df=df, params=p)
assert r1['meta']['setup'] == 'C1'
assert r2['meta']['setup'] == 'C2'
assert 'stats' in r1 and 'stats' in r2
print('C1 stats:', r1['stats'])
print('C2 stats:', r2['stats'])
print('backtest_c.py Task5 OK')
"
```

Expected: `backtest_c.py Task5 OK` (표본 0건이어도 정상 — 무작위 데이터라 신호가 거의 안 나오는 게 자연스럽다. 여기서 확인하는 건 "완주해서 올바른 모양의 dict를 반환하는가"이지 신호 발생 자체가 아니다).

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/backtest_c.py
git commit -m "refactor: rewrite backtest_c.py for OI-merged data + tranche ledger + C1/C2 runners"
```

---

### Task 6: `report_c.py` — 새 통계 구조에 맞춘 기각조건 갱신

**Files:**
- Modify: `liquidation_strategy/report_c.py` (전체 교체)

**Interfaces:**
- Consumes: Task 5의 `_summary()` 출력 shape(`count/win_rate/avg_bps/breakeven_win_rate/avg_bars_held/max_consec_losses` — `avg_half_life_bars` 필드는 더 이상 없음).
- Produces: `rejection_check_c(stats: dict) -> tuple[list, bool]`(규칙1만 유지), `rejection_check_oi_gate_contribution(stats_with_oi: dict, stats_without_oi: dict) -> dict`(OI 게이트 on/off 기여도 판정 — 기존 `rejection_check_vr_gate`를 개념 재사용), `correlation_with_ab` 그대로 유지.

- [ ] **Step 1: 파일 교체**

```python
"""Setup C(Price x OI) 기각조건 판정 — report.py(A/B)와 별도 모듈.

OU 전용이던 half-life 기반 규칙(2번)은 제거한다 — 새 Setup C1/C2엔 OU 가정이
없다. 대신 "OI 게이트가 실제로 기여하는가"를 OI-필터 on/off 어블레이션으로
검증한다(옛 rejection_check_vr_gate와 동일한 패턴 — VR게이트 대신 OI게이트).
"""

import pandas as pd

MIN_SAMPLES = 30


def rejection_check_c(stats: dict) -> tuple[list, bool]:
    """count>=30 도달 시 실측 승률 <= 손익분기승률(BE-WR) 이고 기댓값 <= 0이면 기각."""
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    be_wr = stats.get("breakeven_win_rate")

    applicable = count >= MIN_SAMPLES
    wr_flat_or_below = be_wr is not None and win_rate <= be_wr
    wr_value = (f"승률 {win_rate*100:.1f}% / BE-WR {be_wr*100:.1f}%"
                if be_wr is not None else "BE-WR 계산불가(승/패 표본 편중)")
    checks.append({
        "rule": f"{MIN_SAMPLES}건 도달 시 실측 승률 <= 손익분기승률(BE-WR) 이고 기댓값 <= 0",
        "applicable": applicable,
        "value": f"{count}건 / {wr_value} / 평균 {avg_bps:.1f}bps",
        "failed": bool(applicable and wr_flat_or_below and avg_bps <= 0),
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def rejection_check_oi_gate_contribution(stats_with_oi: dict, stats_without_oi: dict) -> dict:
    """OI 게이트 on(정상 실행) vs off(가격/거래량 조건만, OI 게이트 무시하고
    돌린 어블레이션) 비교에서, on 쪽 평균bps 기여(on-off)가 0 이하면 기각.
    호출측(백테스트 스크립트)이 두 실행 결과의 stats를 미리 만들어 넘긴다."""
    on_count, off_count = stats_with_oi.get("count", 0), stats_without_oi.get("count", 0)
    applicable = on_count >= MIN_SAMPLES and off_count >= MIN_SAMPLES
    on_bps = stats_with_oi.get("avg_bps", 0.0)
    off_bps = stats_without_oi.get("avg_bps", 0.0)
    contribution = on_bps - off_bps
    return {
        "rule": "OI 게이트 on/off 비교에서 게이트 기여(on 평균bps - off 평균bps) <= 0",
        "applicable": applicable,
        "value": (f"on {on_bps:+.1f}bps({on_count}건) / off {off_bps:+.1f}bps({off_count}건) "
                  f"/ 기여 {contribution:+.1f}bps"),
        "failed": bool(applicable and contribution <= 0),
    }


def correlation_with_ab(c_trades: list, ab_bps_by_day: pd.Series, threshold: float = 0.6) -> dict:
    """Setup C(C1+C2 합산 권장)를 일별 bps 합으로 리샘플링해 Setup A/B 일별 bps와 상관계수 계산."""
    if not c_trades:
        return {"rule": "Setup A/B와 트레이드 손익 상관 > 0.6", "applicable": False,
                "value": "Setup C 표본 없음", "failed": False}
    df = pd.DataFrame(c_trades)
    df["day"] = pd.to_datetime(df["exit_ts"]).dt.floor("D")
    c_by_day = df.groupby("day")["bps"].sum()
    aligned = pd.concat([c_by_day, ab_bps_by_day.rename("ab")], axis=1).fillna(0.0)
    aligned.columns = ["c", "ab"]
    applicable = len(aligned) >= 10
    corr = float(aligned["c"].corr(aligned["ab"])) if applicable else None
    return {
        "rule": "Setup A/B와 트레이드 손익 상관 > 0.6",
        "applicable": applicable,
        "value": (f"상관계수 {corr:.2f} (겹치는 날 {len(aligned)}일)" if applicable
                  else f"겹치는 날 {len(aligned)}일 (10일 미만 — 판정 보류)"),
        "failed": bool(applicable and corr is not None and corr > threshold),
    }
```

- [ ] **Step 2: 스모크 체크**

```bash
python -c "
from liquidation_strategy.report_c import rejection_check_c, rejection_check_oi_gate_contribution

stats_bad = {'count': 40, 'win_rate': 0.3, 'breakeven_win_rate': 0.5, 'avg_bps': -2.0}
checks, rejected = rejection_check_c(stats_bad)
assert rejected is True

stats_good = {'count': 40, 'win_rate': 0.55, 'breakeven_win_rate': 0.45, 'avg_bps': 3.0}
checks, rejected = rejection_check_c(stats_good)
assert rejected is False

on = {'count': 35, 'avg_bps': 5.0}
off = {'count': 35, 'avg_bps': -1.0}
r = rejection_check_oi_gate_contribution(on, off)
assert r['failed'] is False and r['applicable'] is True

print('report_c.py Task6 OK')
"
```

Expected: `report_c.py Task6 OK`.

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/report_c.py
git commit -m "refactor: adapt report_c.py rejection checks to Setup C1/C2 stats shape"
```

---

### Task 7: `live_feed.py` — OI 히스토리 누적 로깅

**Files:**
- Modify: `liquidation_strategy/live_feed.py:170-172` (`on_oi_poll` 메서드)

**Interfaces:**
- Consumes: 기존 `self._append_jsonl(path, obj)` 헬퍼(`live_feed.py:100`), 기존 `self.log_dir`.
- Produces: `logs/oi_history.jsonl`에 `{"ts": ..., "oi": ...}` 라인이 5분마다(기존 `OI_POLL_S=300` 주기, `oi_poll_loop`가 이미 호출) 누적. `backtest_c.fetch_5m_with_oi()`(Task 5)가 이 파일을 읽는다.

- [ ] **Step 1: `LiveShadowRunner.__init__`에 경로 필드 추가**

`liquidation_strategy/live_feed.py`의 기존 `self.signal_log_path = os.path.join(log_dir, "signals.jsonl")` 바로 아래에 추가:

```python
        self.oi_log_path = os.path.join(log_dir, "oi_history.jsonl")
```

- [ ] **Step 2: `on_oi_poll`을 로깅하도록 수정**

기존:
```python
    def on_oi_poll(self, oi_value, ts):
        self._latest_oi = oi_value
        self.engine.on_oi(OIPoint(ts=ts, oi=oi_value))
```

교체:
```python
    def on_oi_poll(self, oi_value, ts):
        self._latest_oi = oi_value
        self._append_jsonl(self.oi_log_path, {"ts": ts, "oi": oi_value})
        self.engine.on_oi(OIPoint(ts=ts, oi=oi_value))
```

- [ ] **Step 3: 스모크 체크 — 네트워크 없이 `on_oi_poll` 호출만으로 파일이 append되는지 확인**

```bash
python -c "
import os, json, tempfile
from liquidation_strategy.live_feed import LiveShadowRunner

with tempfile.TemporaryDirectory() as d:
    r = LiveShadowRunner(log_dir=d, out_json=os.path.join(d, 'out.json'))
    r.on_oi_poll(12345.6, 1700000000.0)
    r.on_oi_poll(12350.1, 1700000300.0)
    path = os.path.join(d, 'oi_history.jsonl')
    assert os.path.exists(path)
    lines = open(path, encoding='utf-8').read().strip().splitlines()
    assert len(lines) == 2
    row = json.loads(lines[0])
    assert row['ts'] == 1700000000.0 and abs(row['oi'] - 12345.6) < 1e-9
print('live_feed.py Task7 OK')
"
```

Expected: `live_feed.py Task7 OK`.

- [ ] **Step 4: Commit**

```bash
git add liquidation_strategy/live_feed.py
git commit -m "feat: persist live OI polls to logs/oi_history.jsonl for long-horizon backtesting"
```

---

### Task 8: `live_setup_c.py` — C1/C2 듀얼 상태머신 실시간 러너

**Files:**
- Modify: `liquidation_strategy/live_setup_c.py` (전체 교체)

**Interfaces:**
- Consumes: Task 2-4의 `ParamsC`, `compute_features`, `advance_c1/advance_c2`, `C1State/C2State`, `tranche_exit_step`; Task 5의 `Ledger`, `_bps`, `_r_multiple`, `_max_consec_losses`.
- Produces (계약 불변 — `gui/live_engine_bridge.py`가 그대로 호출): `LiveSetupCRunner(params: ParamsC = None)`, `.on_confirmed_5m_candle(candle: dict)`, `.on_oi(oi_value: float, ts: float)`(신규), `.stop()`, `.restart(params)`, `.open_legs_view() -> list[dict]`, `.describe() -> str`, `.summary() -> dict`, `.combined_summary() -> dict`, `.last_conditions -> list[dict]`, `.closed_trades: list[dict]`.

- [ ] **Step 1: 파일 전체 교체**

```python
"""Setup C 실시간 러너 — 확정 5분봉 스트림 위에서 C1(OI-Flush Reversal Long)과
C2(Coil-Break OI Filter)를 동시에 구동한다. StrategyEngine과는 완전히
독립적이지만, GUI(Dashboard/Settings/PnL/Log)에는 A/B와 동일한 수준으로
연동된다 — 공개 메서드 시그니처는 이전 버전(OU 평균회귀)과 동일하게 유지.

라이브 모드: live_feed.py가 이미 구독 중인 btcusdt@kline_5m 확정봉 +
oi_poll_loop(5분 REST)의 OI 값을 받는다(신규 on_oi 훅).
합성 모드: MinuteAggregator로 1분봉을 5분봉으로 집계해 같은 인터페이스로 공급.
"""

from __future__ import annotations

from collections import deque

import pandas as pd

from .setup_c import (
    ParamsC, compute_features, advance_c1, advance_c2, C1State, C2State,
    conditions_c1, conditions_c2,
)
from .backtest_c import Ledger

MAX_BARS_KEPT = 3000   # quantile_lookback_bars(기본 2016) + 여유


class MinuteAggregator:
    """1분봉 -> N초 버킷 집계기. 합성 모드에서 Setup C(5분봉 전용)를 구동하기
    위해서만 쓰인다."""

    def __init__(self, bucket_s: int, on_bar_closed):
        self.bucket_s = bucket_s
        self.on_bar_closed = on_bar_closed
        self._forming = None

    def add_1m(self, ts: float, o: float, h: float, l: float, c: float, v: float):
        bucket_ts = (int(ts) // self.bucket_s) * self.bucket_s
        f = self._forming
        if f is None or f["ts"] != bucket_ts:
            if f is not None:
                self.on_bar_closed(dict(f, closed=True))
            self._forming = {"ts": bucket_ts, "open": o, "high": h, "low": l, "close": c, "volume": v}
        else:
            f["high"] = max(f["high"], h)
            f["low"] = min(f["low"], l)
            f["close"] = c
            f["volume"] += v


class LiveSetupCRunner:
    """확정 5분봉 하나씩 받아 C1/C2를 동시 구동. 실주문 없음(섀도/페이퍼)."""

    def __init__(self, params: ParamsC = None):
        self.p = params or ParamsC()
        self.enabled = True
        self._rows = deque(maxlen=MAX_BARS_KEPT)
        self._latest_oi = None

        self.c1_state = C1State()
        self.c2_state = C2State()
        self.ledger_c1 = Ledger("C1")
        self.ledger_c2 = Ledger("C2")

        self.closed_trades: list[dict] = []
        self.log: list[tuple] = []
        self._c1_conditions: list[dict] = []
        self._c2_conditions: list[dict] = []

    # ------------------------------------------------------------------
    def on_oi(self, oi_value: float, ts: float):
        """live_engine_bridge.py의 oi_poll(5분 REST) 후크에서 호출."""
        self._latest_oi = oi_value

    def on_confirmed_5m_candle(self, candle: dict):
        """candle: {ts, open, high, low, close, volume, closed(무시)}."""
        row = dict(candle)
        row["oi"] = self._latest_oi if self._latest_oi is not None else float("nan")
        self._rows.append(row)
        if len(self._rows) < 3:
            return  # rolling()/iloc[i-1] 접근이 안전하려면 최소 몇 행은 필요.
                    # 히스토리 부족(quantile_lookback_bars 미만)은 advance_c1/advance_c2
                    # 내부의 _min_history_c1/_min_history_c2 가드가 이미 처리한다.
        f = compute_features(self._to_df(), self.p)
        i = len(f) - 1

        self._step_c1(f, i)
        self._step_c2(f, i)

        self._c1_conditions = conditions_c1(f, i, self.c1_state, self.p)
        self._c2_conditions = conditions_c2(f, i, self.c2_state, self.p)

        if not self.enabled:
            return

        self.c1_state, sig1 = advance_c1(f, i, self.c1_state, self.p)
        if sig1 is not None and self.ledger_c1.can_enter(i):
            self.ledger_c1.open("long", sig1, i)
            self.log.append((sig1.ts.timestamp(),
                              f"Setup C1 LONG 진입 신호 @ {sig1.entry:,.1f} (캐스케이드저점 {sig1.cascade_low:,.1f})"))

        self.c2_state, sig2 = advance_c2(f, i, self.c2_state, self.p)
        if sig2 is not None and self.ledger_c2.can_enter(i):
            self.ledger_c2.open(sig2.side, sig2, i, extra={"kind": sig2.kind})
            self.log.append((sig2.ts.timestamp(),
                              f"Setup C2 {sig2.side.upper()} 진입 신호 @ {sig2.entry:,.1f} ({sig2.kind})"))

    def _step_c1(self, f, i):
        row = f.iloc[i]
        trade = self.ledger_c1.step(i, row["high"], row["low"], row["close"], f.index[i], self.p.c1_max_reentries)
        if trade is not None:
            self._record_closed("C1", trade)

    def _step_c2(self, f, i):
        row = f.iloc[i]
        trade = self.ledger_c2.step(i, row["high"], row["low"], row["close"], f.index[i], self.p.c2_max_reentries)
        if trade is not None:
            self._record_closed("C2", trade)

    def _record_closed(self, setup: str, trade: dict):
        rec = {
            "setup": setup, "side": trade["side"].upper(), "entry_ts": trade["entry_ts"].timestamp(),
            "entry_price": trade["entry_price"], "exit_ts": trade["exit_ts"].timestamp(),
            "exit_price": trade["exit_price"], "reason": trade["reason"],
            "bars_held": trade["bars_held"], "bps": trade["bps"], "r_multiple": trade["r_multiple"],
            "tag": f"{setup}-{trade['side']}",
        }
        self.closed_trades.append(rec)
        self.log.append((rec["exit_ts"],
                          f"Setup {setup} {rec['side']} {rec['reason']} 청산 @ {rec['exit_price']:,.1f} ({rec['bps']:+.1f}bps)"))

    def _to_df(self) -> pd.DataFrame:
        df = pd.DataFrame(list(self._rows))
        df.index = pd.to_datetime(df["ts"], unit="s")
        return df

    # ------------------------------------------------------------------
    # GUI/Settings 연동용 — 공개 인터페이스는 이전 버전과 동일하게 유지
    # ------------------------------------------------------------------
    def stop(self):
        self.ledger_c1.pos = None
        self.ledger_c2.pos = None
        self.enabled = False

    def restart(self, params: ParamsC):
        self.p = params
        self.c1_state = C1State()
        self.c2_state = C2State()
        self.ledger_c1 = Ledger("C1")
        self.ledger_c2 = Ledger("C2")
        self.enabled = True

    def open_legs_view(self) -> list[dict]:
        out = []
        for setup, ledger in (("C1", self.ledger_c1), ("C2", self.ledger_c2)):
            pos = ledger.pos
            if pos is None:
                continue
            out.append({
                "setup": setup, "side": pos["side"].upper(), "entry_price": pos["entry"],
                "qty_fraction": 1.0 if not pos["tp1_hit"] else 1 - pos["tp1_fraction"],
                "sl": pos["stop"], "tp1": pos["tp1"], "tp2": pos["tp2"],
                "tp1_hit": pos["tp1_hit"], "tag": f"{setup}-{pos['side']}",
            })
        return out

    def describe(self) -> str:
        if not self._c1_conditions and not self._c2_conditions:
            return "히스토리 축적 중 (5분봉+OI 데이터 대기)"
        if not self.enabled:
            return "중지됨 — \"적용\"으로 재시작 대기"
        parts = []
        if self._c1_conditions:
            met = sum(1 for c in self._c1_conditions if c["met"])
            parts.append(f"C1 {met}/{len(self._c1_conditions)}")
        if self._c2_conditions:
            met = sum(1 for c in self._c2_conditions if c["met"])
            parts.append(f"C2 {met}/{len(self._c2_conditions)}")
        return " · ".join(parts)

    def summary(self) -> dict:
        out = {}
        for setup in ("C1", "C2"):
            trades = [t for t in self.closed_trades if t["setup"] == setup]
            if not trades:
                out[setup] = {"count": 0}
                continue
            wins = [t for t in trades if t["bps"] > 0]
            out[setup] = {
                "count": len(trades),
                "win_rate": len(wins) / len(trades),
                "avg_bps": sum(t["bps"] for t in trades) / len(trades),
                "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
            }
        return out

    def combined_summary(self) -> dict:
        """C1+C2 합산 — PnL 위젯의 단일 "C" 행에 표시(기존 GUI 스키마 불변)."""
        trades = self.closed_trades
        if not trades:
            return {"count": 0}
        wins = [t for t in trades if t["bps"] > 0]
        return {
            "count": len(trades),
            "win_rate": len(wins) / len(trades),
            "avg_bps": sum(t["bps"] for t in trades) / len(trades),
            "avg_r": sum(t["r_multiple"] for t in trades) / len(trades),
            "max_consec_losses": _max_consec_losses(trades),
        }

    @property
    def last_conditions(self) -> list[dict]:
        def _prefix(conds, tag):
            return [{**c, "label": f"{tag} {c['label']}"} for c in conds]
        return _prefix(self._c1_conditions, "[C1]") + _prefix(self._c2_conditions, "[C2]")


def _max_consec_losses(trades: list) -> int:
    m = cur = 0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        if t["bps"] <= 0:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return m
```

- [ ] **Step 2: 스모크 체크 — 합성 5분봉 스트림으로 러너가 죽지 않고 도는지 확인**

```bash
python -c "
import numpy as np
from liquidation_strategy.setup_c import ParamsC
from liquidation_strategy.live_setup_c import LiveSetupCRunner

p = ParamsC(quantile_lookback_bars=50, atr_window=5, c2_coil_window_bars=10)
runner = LiveSetupCRunner(p)
rng = np.random.default_rng(4)
close = 100.0
for i in range(300):
    close += rng.normal(0, 0.05)
    runner.on_oi(1000 + rng.normal(0, 1), ts=i * 300.0)
    runner.on_confirmed_5m_candle({
        'ts': i * 300.0, 'open': close, 'high': close + 0.1, 'low': close - 0.1,
        'close': close, 'volume': rng.uniform(1, 5), 'closed': True,
    })

assert isinstance(runner.describe(), str)
assert isinstance(runner.open_legs_view(), list)
assert isinstance(runner.summary(), dict)
assert isinstance(runner.combined_summary(), dict)
assert isinstance(runner.last_conditions, list)
runner.stop()
runner.restart(ParamsC(quantile_lookback_bars=50, atr_window=5, c2_coil_window_bars=10))
print('live_setup_c.py Task8 OK')
"
```

Expected: `live_setup_c.py Task8 OK` (예외 없이 300봉 스트리밍 + stop/restart 완주).

- [ ] **Step 3: Commit**

```bash
git add liquidation_strategy/live_setup_c.py
git commit -m "refactor: rewrite LiveSetupCRunner to drive C1+C2 dual state machines"
```

---

### Task 9: GUI 배선 — OI 훅 연결 + 라벨/조건행수 갱신

**Files:**
- Modify: `gui/live_engine_bridge.py:104-116` (`_install_hooks`의 `on_display_candle`)
- Modify: `gui/live_engine_bridge.py:154-161` (`on_oi_poll` 후크)
- Modify: `gui/views/settings_page.py:133` (Setup C 라벨)
- Modify: `gui/views/dashboard_page.py:137` (`trigger_c` `max_rows`)

**Interfaces:**
- Consumes: Task 8의 `LiveSetupCRunner.on_oi(oi_value, ts)`.

- [ ] **Step 1: `gui/live_engine_bridge.py` — OI를 c_runner에도 전달**

기존(`_install_hooks` 내부, "# 4) OI 5분 폴링" 부분):
```python
        orig_on_oi = self.runner.on_oi_poll

        def on_oi_poll(oi_value, ts):
            orig_on_oi(oi_value, ts)
            self.bus.publish("oi", {"ts": ts, "oi": oi_value})
        self.runner.on_oi_poll = on_oi_poll
```

교체:
```python
        orig_on_oi = self.runner.on_oi_poll

        def on_oi_poll(oi_value, ts):
            orig_on_oi(oi_value, ts)
            self.c_runner.on_oi(oi_value, ts)
            self.bus.publish("oi", {"ts": ts, "oi": oi_value})
        self.runner.on_oi_poll = on_oi_poll
```

- [ ] **Step 2: `gui/views/settings_page.py` — 라벨 갱신**

기존:
```python
        self.group_c = ParamGroup(groups, "Setup C — OU 평균회귀 (검증 전, 가설)", ParamsC)
```
교체:
```python
        self.group_c = ParamGroup(groups, "Setup C — Price×OI 상태 분류기 (C1+C2, 검증 전)", ParamsC)
```

- [ ] **Step 3: `gui/views/dashboard_page.py` — 조건 행수 확장**

기존:
```python
        self.trigger_c = TriggerSection(scroller.interior, "Setup C", theme.ACCENT_YELLOW, max_rows=6)
```
교체 (C1 최대 4행 + C2 최대 3행 + 라벨 프리픽스 고려 여유 있게):
```python
        self.trigger_c = TriggerSection(scroller.interior, "Setup C", theme.ACCENT_YELLOW, max_rows=8)
```

- [ ] **Step 4: 컴파일 체크**

```bash
python -m py_compile gui/live_engine_bridge.py gui/views/settings_page.py gui/views/dashboard_page.py liquidation_strategy/live_setup_c.py liquidation_strategy/setup_c.py liquidation_strategy/backtest_c.py liquidation_strategy/report_c.py liquidation_strategy/live_feed.py liquidation_strategy/oi_features.py
echo COMPILE_OK
```

Expected: `COMPILE_OK` (문법 에러 없음).

- [ ] **Step 5: Commit**

```bash
git add gui/live_engine_bridge.py gui/views/settings_page.py gui/views/dashboard_page.py
git commit -m "wire: connect OI polls to Setup C runner and update GUI labels/row counts"
```

---

### Task 10: 엔드투엔드 검증 (합성 스트림 + 실사용자 PC 백테스트 CLI)

**Files:** 없음(검증만).

- [ ] **Step 1: 전체 임포트 그래프가 깨지지 않는지 GUI 진입점으로 확인**

```bash
python -c "
import gui.app
import gui.live_engine_bridge
from liquidation_strategy.setup_c import ParamsC
import dataclasses
fields = [f.name for f in dataclasses.fields(ParamsC)]
assert all(f.startswith(('c1_', 'c2_')) or f in ('atr_window', 'quantile_lookback_bars') for f in fields)
print('필드 개수:', len(fields))
print('Task10 import graph OK')
"
```

Expected: `Task10 import graph OK` (tkinter가 없는 headless 환경이면 `gui.app` import 시 `_tkinter` 관련 에러가 날 수 있음 — 그 경우 `gui.live_engine_bridge`만 단독 임포트해서 확인. 사용자 PC에는 tkinter가 있으므로 실제 실행 시엔 문제 없음).

- [ ] **Step 2: (사용자 PC에서, 인터넷 되는 환경) 실데이터로 스모크 백테스트**

```bash
python -m liquidation_strategy.backtest_c --setup c1 --days 14
python -m liquidation_strategy.backtest_c --setup c2 --days 14
```

Expected: 각각 `{"count": N, ...}` 형태의 stats가 출력되고 예외 없이 종료. `count`가 0이어도 정상(임계값이 보수적이면 14일치엔 신호가 드물 수 있음) — 여기서 확인하는 건 "OI 병합 백필 + 상태머신이 실데이터로 죽지 않고 도는가".

- [ ] **Step 3: (사용자 PC에서) GUI 기동 확인**

```bash
python run_all.py --synthetic
```

Dashboard 탭에서 "Setup C" 트리거 섹션에 `[C1] ...`/`[C2] ...` 프리픽스가 붙은 조건 리스트가 뜨는지, PnL 위젯의 "C" 행이 여전히 하나로 표시되는지, Settings 탭의 "Setup C — Price×OI 상태 분류기" 폼에 새 필드들이 뜨는지 육안 확인.

- [ ] **Step 4: 범위 확인 질문 (사용자에게)**

이 계획은 전략 재작성(Task 1-9)까지만 다룬다. 아래는 이번 계획에 포함되지 않았다 — 완료 후 필요 여부를 사용자에게 확인할 것:
- DSR/PBO/순열검정/IS-OOS 검증 프레임워크(`replay.py` 등)는 저장소에 없다 — 신규 구축이 필요하면 별도 계획으로 분리.
- `optimize.py`의 그리드서치 대상에 C1/C2 knob 추가(현재 A/B만 지원).
- Setup 3(OI 다이버전스 소진) — 사용자가 최우선순위 아니라고 명시, 미구현.
