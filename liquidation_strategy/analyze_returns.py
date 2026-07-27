"""진입 타점 선정 -> 매도(청산) 타점 선별 -> 수익률 확인 통합 파이프라인.

기존 모듈을 그대로 연결한다 (StrategyEngine은 무수정 원칙 유지):
  - 진입 타점: setup_a.CascadeExhaustionLong / setup_b.TrappedLongFlushShort 트리거
  - 청산 타점: engine.StrategyEngine 의 SL / TP1 / TP2 / TIME 관리
  - 수익률:   본 모듈이 체결 내역을 트레이드별 표(CSV) + 요약 + 에쿼티로 변환
              (bps는 왕복비용 차감 후, R은 손절 거리 기준 위험단위)

[재설계] Setup A가 forceOrder 없이 캔들+거래량+OI만으로 동작하도록 바뀌면서
(setup_a.py 참고), klines+OI만 있으면 Setup A/B 둘 다 재현 가능해졌다.
--source rest가 더 이상 "Setup B만"이 아니다.

데이터 소스 2종:
  1) --source synthetic          합성 데이터 (파이프라인 검증용, 어디서나 동작)
  2) --source rest --days 29     Binance 실데이터 klines+OI -> Setup A/B 모두 (인터넷 필요)
  3) --source replay             klines CSV(선택: cvd 컬럼) + OI CSV -> Setup A/B 모두
                                 (--source rest와 동일 로직, 로컬 CSV로 오프라인 재현할 때 사용)

수익률 정의 (두 가지를 함께 보고, 혼동하지 않는다):
  - net_bps        포지션 명목가 기준 수익률(bp), 왕복비용 차감 후. 트랜치 비중 가중.
  - equity_curve   고정 리스크 모델: 매 트레이드 위험(1R) = 계좌의 risk_pct%.
                   트레이드 수익 = R × qty_fraction × risk_pct. 복리 누적.
                   레버리지·명목 크기와 무관하게 "위험 단위당 성과"를 복리로 환산한 값.

실행 예:
    python3 -m liquidation_strategy.analyze_returns --source synthetic
    python3 -m liquidation_strategy.analyze_returns --source rest --days 29
    python3 -m liquidation_strategy.analyze_returns --source replay \
        --klines data/klines_1m.csv --oi data/oi_5m.csv

출력:
    trades_report.csv                 트레이드별 진입/청산/수익률 표
    liquidation_strategy_output.json  대시보드(gui.py) 자동 리로드용 (--out 으로 변경 가능)
    콘솔 요약: 셋업별 건수/승률/평균bps/평균R/누적수익률/기각조건 판정
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone

from .data_types import Candle, OIPoint
from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams
from .report import build_report, rejection_check_a, rejection_check_b

DEFAULT_RISK_PCT = 0.5  # 트레이드당 계좌 위험(1R) = 0.5%


# ----------------------------------------------------------------------
# 데이터 로더
# ----------------------------------------------------------------------
def _parse_ts(v):
    """epoch(s), epoch(ms), ISO8601 문자열 모두 허용 -> epoch seconds."""
    try:
        f = float(v)
        return f / 1000.0 if f > 1e11 else f
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


def load_klines_csv(path):
    """klines CSV -> [Candle]. 헤더 필요: ts/open/high/low/close/volume[,cvd].

    fetch_binance_data.py 산출물이나 Binance kline 원본을 CSV로 저장한 형태 모두
    호환되도록 컬럼명을 느슨하게 매칭한다.
    """
    candles = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in reader.fieldnames}

        def col(*names):
            for n in names:
                if n in cols:
                    return cols[n]
            return None

        c_ts = col("ts", "timestamp", "open_time", "time")
        c_o, c_h = col("open", "o"), col("high", "h")
        c_l, c_c = col("low", "l"), col("close", "c")
        c_v = col("volume", "v", "vol")
        c_cvd = col("cvd", "cvd_delta", "delta")
        if not all((c_ts, c_o, c_h, c_l, c_c)):
            raise ValueError(f"klines CSV 헤더 인식 실패: {reader.fieldnames}")

        for row in reader:
            candles.append(Candle(
                ts=_parse_ts(row[c_ts]),
                open=float(row[c_o]), high=float(row[c_h]),
                low=float(row[c_l]), close=float(row[c_c]),
                volume=float(row[c_v]) if c_v and row.get(c_v) else 0.0,
                cvd_delta=float(row[c_cvd]) if c_cvd and row.get(c_cvd) else 0.0,
            ))
    candles.sort(key=lambda c: c.ts)
    return candles


def load_oi_csv(path):
    """OI CSV/JSON -> [OIPoint]. CSV 헤더: ts,oi (또는 timestamp,sumOpenInterest)."""
    pts = []
    if path.endswith(".json"):
        with open(path) as f:
            raw = json.load(f)
        for o in raw:
            pts.append(OIPoint(ts=_parse_ts(o["timestamp"]), oi=float(o["sumOpenInterest"])))
    else:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            cols = {c.lower().strip(): c for c in reader.fieldnames}
            c_ts = cols.get("ts") or cols.get("timestamp") or cols.get("time")
            c_oi = cols.get("oi") or cols.get("sumopeninterest") or cols.get("open_interest")
            if not (c_ts and c_oi):
                raise ValueError(f"OI CSV 헤더 인식 실패: {reader.fieldnames}")
            for row in reader:
                pts.append(OIPoint(ts=_parse_ts(row[c_ts]), oi=float(row[c_oi])))
    pts.sort(key=lambda p: p.ts)
    return pts


def build_box_series(candles, window_min=240):
    """backfill.py 와 동일한 4시간 롤링 박스 — "이번 봉 이전까지"의 고가/저가로
    계산한다(live_feed.py와 동일 원칙, close가 아니라 실제 윅 사용)."""
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    out = []
    for i in range(len(candles)):
        if i == 0:
            out.append((candles[i].ts, lows[i], highs[i]))
            continue
        lo = max(0, i - window_min)
        out.append((candles[i].ts, min(lows[lo:i]), max(highs[lo:i])))
    return out


# ----------------------------------------------------------------------
# 리플레이: 진입(셋업 트리거) -> 청산(엔진) 실행
# ----------------------------------------------------------------------
def replay(candles, oi_points, a_params=None, b_params=None,
           macro_blackouts=None, cost_bps=10.0):
    """candles+oi_points만으로 Setup A/B를 함께 구동한다 — Setup A가 forceOrder
    없이 캔들+거래량+OI로 재설계된 이후 이 함수엔 forceOrder가 전혀 필요 없다."""
    engine = StrategyEngine(a_params or CascadeAParams(), b_params or CascadeBParams(),
                            macro_blackouts, cost_bps=cost_bps)

    box_by_ts = {b[0]: (b[1], b[2]) for b in build_box_series(candles)}

    events = [(c.ts, 1, "candle", c) for c in candles]
    events += [(p.ts, 0, "oi", p) for p in oi_points]
    events.sort(key=lambda e: (e[0], e[1]))

    latest_oi = None
    for ts_, _, kind, payload in events:
        if kind == "oi":
            latest_oi = payload.oi
            engine.on_oi(payload)
        else:
            if payload.cvd_delta:
                engine.a.on_cvd_delta(ts_, payload.cvd_delta)
            lo_hi = box_by_ts.get(payload.ts)
            if lo_hi:
                engine.set_box(lo_hi[0], lo_hi[1])
            engine.on_candle(payload, oi_now=latest_oi)
    return engine


# ----------------------------------------------------------------------
# 수익률 리포트
# ----------------------------------------------------------------------
def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""


def write_trades_csv(trades, path, risk_pct=DEFAULT_RISK_PCT):
    trades = sorted(trades, key=lambda t: t.exit_ts)
    equity = 1.0
    cum_bps = 0.0
    rows = []
    for t in trades:
        w_bps = t.bps * t.qty_fraction
        cum_bps += w_bps
        equity *= (1.0 + t.r_multiple * t.qty_fraction * risk_pct / 100.0)
        rows.append({
            "setup": t.setup, "side": t.side.value, "tag": t.tag,
            "entry_time_utc": _iso(t.entry_ts), "entry_price": round(t.entry_price, 2),
            "exit_time_utc": _iso(t.exit_ts), "exit_price": round(t.exit_price, 2),
            "exit_reason": t.reason, "qty_fraction": round(t.qty_fraction, 3),
            "r_multiple": round(t.r_multiple, 3),
            "net_bps": round(t.bps, 2),
            "weighted_net_bps": round(w_bps, 2),
            "cum_net_bps": round(cum_bps, 2),
            f"equity_x (risk {risk_pct}%/R)": round(equity, 5),
        })
    with open(path, "w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    return cum_bps, equity


def print_summary(engine, cum_bps, equity, risk_pct=DEFAULT_RISK_PCT):
    summary = engine.summary()
    print("\n=== 수익률 요약 (왕복비용 %.1fbps 차감 후) ===" % engine.cost_bps)
    for setup, label in (("A", "A 캐스케이드 소진 롱"), ("B", "B 트랩드롱 플러시 숏")):
        s = summary.get(setup, {})
        if not s or not s.get("count"):
            print(f"[Setup {setup}] {label}: 체결 0건")
            continue
        print(f"[Setup {setup}] {label}: {s['count']}건 | 승률 {s['win_rate']*100:.1f}% | "
              f"평균 {s['avg_bps']:.1f}bps | 평균 {s['avg_r']:.2f}R | 최대연속손실 {s['max_consec_losses']}회")
        checks, rejected = (rejection_check_a if setup == "A" else rejection_check_b)(s)
        for c in checks:
            state = "위반" if c["failed"] else ("통과" if c["applicable"] else "표본부족(미적용)")
            print(f"    - {c['rule']} -> {c['value']} [{state}]")
        print(f"    => 사전등록 기각 판정: {'기각' if rejected else '유지'}")
    n = len(engine.closed_trades)
    print(f"\n전체 {n}건 | 누적 {cum_bps:.1f}bps (비중가중 합산)")
    print(f"고정리스크 복리 수익률 (트레이드당 위험 {risk_pct}%/R 가정): {(equity-1)*100:+.2f}%")
    if any(t.setup == "A" for t in engine.closed_trades) is False:
        print("* Setup A 체결 없음 — 이 구간엔 트리거 조건(가격급락+OI급감+RVOL)이 "
              "충족되지 않은 것으로 보입니다.")


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="진입->청산->수익률 통합 파이프라인")
    ap.add_argument("--source", choices=["synthetic", "rest", "replay"], default="synthetic")
    ap.add_argument("--days", type=int, default=29, help="rest: 조회 일수 / synthetic: 생성 일수")
    ap.add_argument("--seed", type=int, default=7, help="synthetic 시드")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--klines", help="replay: klines CSV (ts,open,high,low,close,volume[,cvd])")
    ap.add_argument("--oi", help="replay: OI CSV/JSON (ts,oi)")
    ap.add_argument("--risk-pct", type=float, default=DEFAULT_RISK_PCT, help="트레이드당 계좌 위험 %% (1R)")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--trades-csv", default="trades_report.csv")
    ap.add_argument("--out", default="liquidation_strategy_output.json",
                    help="대시보드 JSON 경로 (gui.py 자동 리로드 대상)")
    args = ap.parse_args()

    if args.source == "synthetic":
        from .simulate_data import generate
        from .backtest import run_backtest
        sim, baseline = generate(days=args.days if args.days != 29 else 120, seed=args.seed)
        engine = run_backtest(sim, baseline, cost_bps=args.cost_bps)
        candles = sim.candles
        meta_src = "synthetic"
    elif args.source == "rest":
        from .backfill import fetch
        candles, oi_points = fetch(args.days, args.symbol)
        engine = replay(candles, oi_points, cost_bps=args.cost_bps)
        meta_src = "binance_rest_real (Setup A/B 모두)"
    else:  # replay
        if not args.klines:
            ap.error("--source replay 에는 --klines 가 필요합니다")
        candles = load_klines_csv(args.klines)
        oi_points = load_oi_csv(args.oi) if args.oi else []
        engine = replay(candles, oi_points, cost_bps=args.cost_bps)
        meta_src = f"replay (klines={args.klines}, oi={args.oi})"

    cum_bps, equity = write_trades_csv(engine.closed_trades, args.trades_csv, args.risk_pct)
    print_summary(engine, cum_bps, equity, args.risk_pct)

    meta = {
        "symbol": args.symbol, "n_candles": len(candles),
        "roundtrip_cost_bps": engine.cost_bps, "data_source": meta_src,
        "risk_pct_per_R": args.risk_pct,
        "equity_multiple": round(equity, 5), "cum_net_bps": round(cum_bps, 2),
    }
    out = build_report(engine, candles, meta)
    with open(args.out, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"\n트레이드 표 -> {args.trades_csv}")
    print(f"대시보드 JSON -> {args.out}  (gui.py 실행 중이면 자동 리로드)")


if __name__ == "__main__":
    main()
