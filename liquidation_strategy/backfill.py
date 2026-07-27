"""Setup A+B 실데이터 과거 백테스트.

klines(가격) + openInterestHist(OI) 만으로 Setup A/B를 함께 실제 Binance
과거 데이터로 백테스트한다. [재설계] Setup A는 이제 forceOrder(청산 틱)
없이 캔들+거래량+OI만으로 동작하므로(setup_a.py 참고), 예전처럼 "forceOrder
과거 이력이 없어서 Setup A는 비활성"일 필요가 없다 — Setup B와 똑같은
REST 데이터로 Setup A도 재현된다.

openInterestHist는 Binance가 최근 30일까지만 제공하므로 조회 구간은
자동으로 그 안으로 제한된다.

실행 (인터넷이 열린 환경에서):
    python3 -m liquidation_strategy.backfill --days 30
"""

import argparse
import json
import sys
import time

from . import binance_client as bc
from .data_types import Candle, OIPoint
from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams
from .report import build_report

SYMBOL = "BTCUSDT"


def fetch(days: int, symbol: str = SYMBOL):
    days = min(days, 29)  # openInterestHist 30일 한도 여유
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000

    print(f"[1/3] {symbol} 1분봉 {days}일 수집...", file=sys.stderr)
    raw_klines = bc.get_klines_range(symbol, "1m", start_ms, end_ms)
    candles = [
        Candle(ts=k[0] / 1000.0, open=float(k[1]), high=float(k[2]),
               low=float(k[3]), close=float(k[4]), volume=float(k[5]))
        for k in raw_klines
    ]

    print(f"[2/3] {symbol} OI(5분) {days}일 수집...", file=sys.stderr)
    raw_oi = bc.get_open_interest_hist_range(symbol, "5m", start_ms, end_ms)
    oi_points = [OIPoint(ts=o["timestamp"] / 1000.0, oi=float(o["sumOpenInterest"])) for o in raw_oi]

    return candles, oi_points


def build_box_series(candles, window_min=240):
    """"이번 봉 이전까지"의 고가/저가로 박스를 계산한다(live_feed.py와 동일 원칙).
    이번 봉을 포함해서 계산하면 그 봉이 만든 새 극값이 곧 박스 경계가 되어버려
    그 봉 자신의 돌파를 절대 감지할 수 없다. close가 아니라 실제 윅(high/low)을
    써야 라이브 박스 계산과 일치한다."""
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    box_series = []
    for i in range(len(candles)):
        if i == 0:
            box_series.append((candles[i].ts, lows[i], highs[i]))
            continue
        lo_win = max(0, i - window_min)
        box_series.append((candles[i].ts, min(lows[lo_win:i]), max(highs[lo_win:i])))
    return box_series


def run(days: int, symbol: str = SYMBOL, a_params=None, b_params=None,
        out_path="liquidation_strategy_output_real_b.json"):
    candles, oi_points = fetch(days, symbol)
    if not candles:
        raise RuntimeError("klines 수집 실패 — 네트워크/레이트리밋 확인")

    print("[3/3] Setup A+B 백테스트 실행...", file=sys.stderr)
    engine = StrategyEngine(a_params or CascadeAParams(), b_params or CascadeBParams())

    box_series = build_box_series(candles)
    box_by_ts = {b[0]: (b[1], b[2]) for b in box_series}
    oi_by_ts = {o.ts: o.oi for o in oi_points}
    oi_sorted = sorted(oi_points, key=lambda o: o.ts)

    oi_idx = 0
    latest_oi = None
    for c in candles:
        while oi_idx < len(oi_sorted) and oi_sorted[oi_idx].ts <= c.ts:
            latest_oi = oi_sorted[oi_idx].oi
            engine.on_oi(oi_sorted[oi_idx])
            oi_idx += 1
        lo_hi = box_by_ts.get(c.ts)
        if lo_hi:
            engine.set_box(lo_hi[0], lo_hi[1])
        engine.on_candle(c, oi_now=latest_oi)

    meta = {
        "symbol": symbol,
        "days": days,
        "n_candles": len(candles),
        "roundtrip_cost_bps": engine.cost_bps,
        "data_source": "binance_rest_real (Setup A/B 모두)",
    }
    out = build_report(engine, candles, meta)
    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"완료 -> {out_path}", file=sys.stderr)
    return out


def main():
    ap = argparse.ArgumentParser(description="Setup A+B 실데이터(Binance REST) 백테스트")
    ap.add_argument("--days", type=int, default=29, help="조회 기간(일), openInterestHist 한도상 최대 29")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--out", default="liquidation_strategy_output_real_b.json")
    args = ap.parse_args()
    run(args.days, args.symbol, out_path=args.out)


if __name__ == "__main__":
    main()
