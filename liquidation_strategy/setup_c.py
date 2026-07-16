"""Setup C — OU 평균회귀 (박스권 전용, 비유동성 전략).

StrategyEngine 무수정 원칙: 본 모듈은 klines DataFrame(5m)을 입력받아
신호/청산 이벤트를 반환하는 순수 함수 집합. 백테스트/라이브 공용.
포지션 라이프사이클(재진입/쿨다운) 상태는 여기서 들고 있지 않는다 —
그건 backtest_c.py의 원장(ledger)이 담당한다.

Pre-registration: uploads/strategy_c_ou_reversion_spec.md v1.0 (2026-07-17)
초기 파라미터는 '가설'이며 최적화 결과가 아니다. 30샘플 전 성과 해석 금지.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


# ---------------------------------------------------------------- parameters

@dataclass(frozen=True)
class ParamsC:
    # G1 — Variance Ratio gate
    vr_q: int = 4
    vr_window: int = 288          # 5m bars = 24h
    vr_threshold: float = 0.85    # VR < threshold → mean-reversion regime
    use_vr_gate: bool = True      # False면 G1을 건너뛴다 (명세 6장 규칙3: on/off 비교용)

    # G2 — half-life validity
    hl_min: int = 10              # bars
    hl_max: int = 120             # bars

    # G3 — z-score
    z_entry: float = 2.0
    z_exit: float = 0.5
    zwin_lo: int = 30
    zwin_hi: int = 300

    # G4 — box interior
    box_lookback: int = 288       # 24h
    box_quantile_lo: float = 0.05
    box_quantile_hi: float = 0.95

    # exits / risk
    sl_buffer_pct: float = 0.0015
    atr_window: int = 14
    atr_k: float = 1.5
    time_exit_hl_mult: float = 3.0
    max_reentries: int = 1

    # filters (on/off 비교 대상)
    use_funding_filter: bool = False
    funding_abs_limit: float = 0.0005   # per 8h
    use_event_blackout: bool = False
    blackout_pad_s: int = 900


# ---------------------------------------------------------------- statistics

def variance_ratio(logp: np.ndarray, q: int) -> float:
    """Lo–MacKinlay VR(q) on log prices. VR<1 → mean reversion."""
    r1 = np.diff(logp)
    if len(r1) < q * 2 or r1.var() == 0:
        return np.nan
    rq = logp[q:] - logp[:-q]
    # unbiased-ish simple estimator; sufficient as a gate statistic
    return rq.var() / (q * r1.var())


def ar1_half_life(logp: np.ndarray) -> float:
    """AR(1) 회귀로 OU 반감기(bars) 추정. 평균회귀 부재 시 inf."""
    x = logp[:-1]
    dx = np.diff(logp)
    x_c = x - x.mean()
    denom = float(np.dot(x_c, x_c))
    if denom == 0:
        return math.inf
    beta = float(np.dot(x_c, dx - dx.mean())) / denom
    if beta >= 0:                 # no mean reversion
        return math.inf
    theta = -math.log(1.0 + beta) if beta > -1 else math.inf
    if not math.isfinite(theta) or theta <= 0:
        return math.inf
    return math.log(2.0) / theta


def atr(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window).mean()


# ---------------------------------------------------------------- signal core

@dataclass
class SignalC:
    ts: pd.Timestamp
    side: str                     # "long" | "short"
    entry: float
    stop: float
    half_life_bars: float
    time_exit_bars: int
    z: float
    vr: float
    meta: dict = field(default_factory=dict)


def _rejection(reason: str, **kw) -> dict:
    return {"pass": False, "reason": reason, **kw}


def evaluate_bar(
    df: pd.DataFrame,
    i: int,
    p: ParamsC,
    funding_rate: float | None = None,
    in_blackout: bool = False,
) -> dict:
    """i번째 봉 종가 기준 게이트 평가. dict에 pass 여부 + 사유(rejection check 패널용)."""
    if i < max(p.vr_window, p.box_lookback) + 1:
        return _rejection("insufficient_history")

    logp_vr = np.log(df["close"].iloc[i - p.vr_window : i + 1].to_numpy())

    # G1 — regime gate (use_vr_gate=False면 건너뛰되, half-life 추정을 위해 vr 자체는 계산해둔다)
    vr = variance_ratio(logp_vr, p.vr_q)
    if p.use_vr_gate:
        if not np.isfinite(vr) or vr >= p.vr_threshold:
            return _rejection("G1_vr_gate", vr=vr)

    # G2 — half-life validity
    hl = ar1_half_life(logp_vr)
    if not (p.hl_min <= hl <= p.hl_max):
        return _rejection("G2_half_life", half_life=hl, vr=vr)

    # G3 — z-score displacement
    zwin = int(min(max(3 * hl, p.zwin_lo), p.zwin_hi))
    if i - zwin + 1 < 0:
        # zwin(최대 300봉)이 G1/G4가 요구하는 최소 히스토리(288봉)보다 길 수 있어
        # 초반 구간에서는 별도로 막아야 한다 (안 그러면 iloc 음수 인덱싱으로 미래
        # 데이터를 끌어오는 룩어헤드 버그가 생김).
        return _rejection("insufficient_history_zwin", half_life=hl, vr=vr)
    win = df["close"].iloc[i - zwin + 1 : i + 1]
    mu, sd = win.mean(), win.std(ddof=1)
    if sd == 0:
        return _rejection("G3_zero_vol")
    z = (df["close"].iloc[i] - mu) / sd
    if abs(z) < p.z_entry:
        return _rejection("G3_z_below_entry", z=z, vr=vr, half_life=hl)
    side = "long" if z <= -p.z_entry else "short"

    # G4 — box interior (돌파 진행 중 진입 금지)
    box = df["close"].iloc[i - p.box_lookback + 1 : i + 1]
    q_lo, q_hi = box.quantile(p.box_quantile_lo), box.quantile(p.box_quantile_hi)
    px = df["close"].iloc[i]
    if not (q_lo <= px <= q_hi):
        return _rejection("G4_outside_box", z=z)

    # F1 — funding alignment filter (on/off)
    if p.use_funding_filter and funding_rate is not None:
        against = (side == "long" and funding_rate < -p.funding_abs_limit) or (
            side == "short" and funding_rate > p.funding_abs_limit
        )
        # 주의: 부호 해석 — funding > 0 은 롱이 지불. 숏 진입 시 funding이 크게 양수면
        # 우호적(캐리 수취)이므로 차단하지 않음. 차단은 '역캐리' 방향만.
        if against:
            return _rejection("F1_funding_against", funding=funding_rate)

    # F2 — event blackout (on/off)
    if p.use_event_blackout and in_blackout:
        return _rejection("F2_event_blackout")

    # stops: hybrid max(structural, k*ATR)
    a = atr(df.iloc[: i + 1], p.atr_window).iloc[-1]
    if side == "long":
        struct = win.min() * (1 - p.sl_buffer_pct)
        stop = min(struct, px - p.atr_k * a)
    else:
        struct = win.max() * (1 + p.sl_buffer_pct)
        stop = max(struct, px + p.atr_k * a)

    sig = SignalC(
        ts=df.index[i],
        side=side,
        entry=float(px),
        stop=float(stop),
        half_life_bars=hl,
        time_exit_bars=int(round(p.time_exit_hl_mult * hl)),
        z=float(z),
        vr=float(vr),
        meta={"zscore_window": zwin, "mu": float(mu), "atr": float(a)},
    )
    return {"pass": True, "signal": sig}


def conditions(
    df: pd.DataFrame, i: int, p: ParamsC,
    funding_rate: float | None = None, in_blackout: bool = False,
) -> list[dict]:
    """G1~G4(+F1/F2)를 evaluate_bar처럼 첫 실패에서 멈추지 않고 항상 전부
    계산해 체크리스트로 반환한다 (GUI 실시간 표시용, Setup A/B의
    conditions()와 동일한 역할). 반환: [{"key","label","met","detail"}, ...]
    (G1~G4 고정 4개 + 활성화된 필터만 추가)."""
    out = []
    have_history = i >= max(p.vr_window, p.box_lookback) + 1
    if not have_history:
        for key, label in (
            ("g1_vr", f"G1: VR < {p.vr_threshold}"),
            ("g2_hl", f"G2: 반감기 {p.hl_min}~{p.hl_max}봉"),
            ("g3_z", f"G3: |z| ≥ {p.z_entry}"),
            ("g4_box", "G4: 박스 내부"),
        ):
            out.append({"key": key, "label": label, "met": False, "detail": "히스토리 부족"})
        return out

    logp_vr = np.log(df["close"].iloc[i - p.vr_window : i + 1].to_numpy())

    vr = variance_ratio(logp_vr, p.vr_q)
    vr_ok = np.isfinite(vr) and vr < p.vr_threshold
    out.append({
        "key": "g1_vr", "label": f"G1: VR < {p.vr_threshold}",
        "met": bool(vr_ok), "detail": f"{vr:.3f}" if np.isfinite(vr) else "계산불가",
    })

    hl = ar1_half_life(logp_vr)
    hl_ok = p.hl_min <= hl <= p.hl_max
    out.append({
        "key": "g2_hl", "label": f"G2: 반감기 {p.hl_min}~{p.hl_max}봉",
        "met": bool(hl_ok), "detail": f"{hl:.1f}봉" if math.isfinite(hl) else "회귀없음(inf)",
    })

    # zwin은 half-life에 의존 -> half-life가 무효(inf)면 최소값(zwin_lo)으로 대체 표시
    hl_for_win = hl if math.isfinite(hl) else p.zwin_lo / 3.0
    zwin = int(min(max(3 * hl_for_win, p.zwin_lo), p.zwin_hi))
    if i - zwin + 1 < 0:
        out.append({"key": "g3_z", "label": f"G3: |z| ≥ {p.z_entry}",
                     "met": False, "detail": "히스토리 부족"})
    else:
        win = df["close"].iloc[i - zwin + 1 : i + 1]
        sd = win.std(ddof=1)
        z = float((df["close"].iloc[i] - win.mean()) / sd) if sd > 0 else 0.0
        out.append({"key": "g3_z", "label": f"G3: |z| ≥ {p.z_entry}",
                     "met": bool(abs(z) >= p.z_entry), "detail": f"z={z:+.2f}"})

    box = df["close"].iloc[i - p.box_lookback + 1 : i + 1]
    q_lo, q_hi = box.quantile(p.box_quantile_lo), box.quantile(p.box_quantile_hi)
    px = float(df["close"].iloc[i])
    out.append({
        "key": "g4_box",
        "label": f"G4: 박스 내부[{p.box_quantile_lo*100:.0f}~{p.box_quantile_hi*100:.0f}%]",
        "met": bool(q_lo <= px <= q_hi), "detail": f"{px:.1f} ({q_lo:.1f}~{q_hi:.1f})",
    })

    if p.use_funding_filter:
        if funding_rate is None:
            out.append({"key": "f1_funding", "label": "F1: 펀딩 정렬", "met": False, "detail": "데이터 없음"})
        else:
            out.append({"key": "f1_funding", "label": "F1: 펀딩 정렬",
                         "met": abs(funding_rate) <= p.funding_abs_limit,
                         "detail": f"{funding_rate*100:.3f}%/8h"})
    if p.use_event_blackout:
        out.append({"key": "f2_blackout", "label": "F2: 이벤트 블랙아웃 아님",
                     "met": not in_blackout, "detail": "차단 구간" if in_blackout else "정상"})
    return out


def should_exit(
    df: pd.DataFrame, i: int, sig: SignalC, bars_held: int, p: ParamsC
) -> str | None:
    """청산 판정: 'tp_mean' | 'sl' | 'time' | None. 원장 기록용 사유 반환."""
    px = df["close"].iloc[i]
    lo, hi = df["low"].iloc[i], df["high"].iloc[i]

    # SL — intrabar touch
    if sig.side == "long" and lo <= sig.stop:
        return "sl"
    if sig.side == "short" and hi >= sig.stop:
        return "sl"

    # TP — z reversion (진입 시점과 동일 윈도우 정의로 재계산)
    zwin = sig.meta["zscore_window"]
    if i - zwin + 1 >= 0:
        win = df["close"].iloc[i - zwin + 1 : i + 1]
        sd = win.std(ddof=1)
        if sd > 0:
            z_now = (px - win.mean()) / sd
            if abs(z_now) <= p.z_exit:
                return "tp_mean"

    # time exit — OU 가정 위배
    if bars_held >= sig.time_exit_bars:
        return "time"
    return None
