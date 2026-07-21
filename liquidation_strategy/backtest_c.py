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
import requests

from . import binance_client as bc
from . import oi_features as feat
from .setup_c import (
    ParamsC, compute_features, advance_c1, advance_c2, C1State, C2State, tranche_exit_step,
)

SYMBOL = "BTCUSDT"
COST_BPS = 10.0
OI_REST_MAX_DAYS = 29  # Binance 문서상 한도는 30일이지만 요청 시점 지연/시계
                       # 오차로 정확히 30일 폭 요청이 400을 반환하는 경우가 있어 여유를 둔다


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
    try:
        oi_hist = bc.get_open_interest_hist_range(symbol, "5m", oi_rest_start, end_ms)
    except requests.exceptions.HTTPError as e:
        # Binance의 openInterestHist 30일 한도 경계에서 400이 나는 경우가 있다 —
        # OI 없이는 gate가 전부 NaN(미충족) 처리되니, 전체 백테스트를 죽이지 않고
        # 로컬 누적 로그만으로 계속 진행한다.
        print(f"[fetch_5m_with_oi] openInterestHist 요청 실패({e}) — 로컬 누적 로그만 사용",
              file=sys.stderr)
        oi_hist = []
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
