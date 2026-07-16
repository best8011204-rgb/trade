"""Setup C(OU 평균회귀) 독립 백테스트 — StrategyEngine과 완전히 분리된 원장.

명세(uploads/strategy_c_ou_reversion_spec.md) 7장 검증 절차 중 1·2·4번:
  1. 5분봉 실데이터 백필 — 별도 fetch_binance_data.py를 새로 만들지 않고
     기존 binance_client.get_klines_range(이미 임의 interval 지원)를 재사용한다.
  2. 파라미터 조정 없이 스펙 초기값 그대로 1차 통과시켜 상태만 확인한다
     (run_backtest 자체가 이 용도 — 최적화 루프가 없다).
  4. StrategyEngine을 참조하지 않는 독립 원장(ledger_c_long / ledger_c_short).
     setup_c.py의 evaluate_bar/should_exit는 상태 없는 순수 함수이므로,
     재진입 횟수·쿨다운 같은 "지금 포지션이 어떤 상태인가"는 전부 이 파일의
     Ledger가 들고 있는다.

3번(F1/F2 on/off 4조합, VR 게이트 on/off)은 compare_toggles()가 담당한다.

실행:
    python3 -m liquidation_strategy.backtest_c --days 60
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import replace

import pandas as pd

from . import binance_client as bc
from .setup_c import ParamsC, evaluate_bar, should_exit

SYMBOL = "BTCUSDT"
COST_BPS = 10.0  # 명세서 0장과 동일 가정(A/B와 동일 왕복비용)


def fetch_5m(symbol: str = SYMBOL, days: int = 60) -> pd.DataFrame:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    raw = bc.get_klines_range(symbol, "5m", start_ms, end_ms)
    if not raw:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "n_trades", "taker_base", "taker_quote", "ignore",
    ])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["ts"] = df["open_time"] / 1000.0
    df = df[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    df.index = pd.to_datetime(df["ts"], unit="s")
    return df


class Ledger:
    """방향(롱/숏)별 독립 원장. 재진입/쿨다운 상태를 여기서 관리한다."""

    def __init__(self, side: str):
        self.side = side
        self.open_pos = None            # {"sig", "bars_held"}
        self.cooldown_until_bar = None
        self.reentry_count = 0
        self.trades: list[dict] = []

    def can_enter(self, i: int) -> bool:
        if self.open_pos is not None:
            return False
        if self.cooldown_until_bar is not None and i < self.cooldown_until_bar:
            return False
        return True

    def _on_exit(self, reason: str, i: int, half_life_bars: float, p: ParamsC):
        if reason == "sl" and self.reentry_count < p.max_reentries:
            self.reentry_count += 1
            self.cooldown_until_bar = i + max(1, int(round(half_life_bars * 1.0)))
        else:
            # 재진입 소진(또는 TP/시간청산) -> 다음은 완전히 새 신호 사이클로 취급
            self.reentry_count = 0
            self.cooldown_until_bar = None


def _bps(sig, exit_price: float, side: str) -> float:
    pnl = (exit_price - sig.entry) if side == "long" else (sig.entry - exit_price)
    return (pnl / sig.entry) * 10000.0


def _r_multiple(sig, exit_price: float, side: str) -> float:
    risk = abs(sig.entry - sig.stop)
    if risk == 0:
        return 0.0
    pnl = (exit_price - sig.entry) if side == "long" else (sig.entry - exit_price)
    return pnl / risk


def run_backtest(days: int = 60, symbol: str = SYMBOL, params: ParamsC = None,
                  df: pd.DataFrame = None) -> dict:
    """df를 직접 주면(테스트용) 네트워크 호출 없이 그 데이터로 돈다."""
    p = params or ParamsC()
    if df is None:
        df = fetch_5m(symbol, days)
    if df.empty:
        raise RuntimeError("5분봉 수집 실패 — 네트워크/레이트리밋 확인")
    df = df.reset_index(drop=True) if not isinstance(df.index, pd.DatetimeIndex) else df

    ledgers = {"long": Ledger("long"), "short": Ledger("short")}
    rejection_counts: Counter = Counter()

    for i in range(len(df)):
        for side, ledger in ledgers.items():
            pos = ledger.open_pos
            if pos is None:
                continue
            pos["bars_held"] += 1
            reason = should_exit(df, i, pos["sig"], pos["bars_held"], p)
            if reason is None:
                continue
            if reason == "sl":
                exit_price = float(pos["sig"].stop)
            else:
                exit_price = float(df["close"].iloc[i])
            ledger.trades.append({
                "side": side, "entry_ts": pos["sig"].ts, "entry_price": pos["sig"].entry,
                "exit_ts": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["ts"].iloc[i],
                "exit_price": exit_price, "reason": reason, "bars_held": pos["bars_held"],
                "half_life_bars": pos["sig"].half_life_bars,
                "bps": _bps(pos["sig"], exit_price, side) - COST_BPS,
                "r_multiple": _r_multiple(pos["sig"], exit_price, side),
            })
            ledger._on_exit(reason, i, pos["sig"].half_life_bars, p)
            ledger.open_pos = None

        res = evaluate_bar(df, i, p)
        if not res["pass"]:
            rejection_counts[res["reason"]] += 1
            continue
        sig = res["signal"]
        ledger = ledgers[sig.side]
        if ledger.can_enter(i):
            ledger.open_pos = {"sig": sig, "bars_held": 0}

    stats = {side: _summary(ledger.trades) for side, ledger in ledgers.items()}
    return {
        "meta": {"symbol": symbol, "days": days, "n_bars": len(df), "params": p.__dict__,
                 "roundtrip_cost_bps": COST_BPS},
        "stats": stats,
        "trades": {side: ledger.trades for side, ledger in ledgers.items()},
        "rejection_counts": dict(rejection_counts),
    }


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
        "avg_half_life_bars": sum(t["half_life_bars"] for t in trades) / len(trades),
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


def compare_toggles(days: int = 60, symbol: str = SYMBOL, df: pd.DataFrame = None) -> dict:
    """명세 7장 3번: F1×F2 2×2 + VR 게이트 on/off 비교용 러너 묶음.
    각 조합을 동일 데이터로 재실행해 stats를 나란히 반환한다."""
    if df is None:
        df = fetch_5m(symbol, days)
    base = ParamsC()
    combos = {
        "base(F1off,F2off)": base,
        "F1on": replace(base, use_funding_filter=True),
        "F2on": replace(base, use_event_blackout=True),
        "F1on_F2on": replace(base, use_funding_filter=True, use_event_blackout=True),
        "vr_gate_off": replace(base, use_vr_gate=False),
    }
    out = {}
    for name, params in combos.items():
        r = run_backtest(days=days, symbol=symbol, params=params, df=df)
        out[name] = r["stats"]
    return out


def main():
    ap = argparse.ArgumentParser(description="Setup C(OU 평균회귀) 독립 백테스트")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--compare-toggles", action="store_true",
                     help="F1/F2/VR게이트 on-off 비교표까지 함께 출력")
    args = ap.parse_args()

    print(f"[1/2] {args.symbol} 5분봉 {args.days}일 수집...", file=sys.stderr)
    df = fetch_5m(args.symbol, args.days)
    print(f"[2/2] Setup C 백테스트 실행 ({len(df)}봉)...", file=sys.stderr)
    result = run_backtest(days=args.days, symbol=args.symbol, df=df)
    for side, s in result["stats"].items():
        print(f"--- {side} ---")
        print(s)
    print("rejection_counts:", result["rejection_counts"])

    if args.compare_toggles:
        print("\n--- on/off 비교 ---")
        cmp = compare_toggles(args.days, args.symbol, df=df)
        for name, stats in cmp.items():
            print(name, stats)


if __name__ == "__main__":
    main()
