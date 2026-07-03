"""전체 파이프라인 실행: 데이터 생성 -> 임계값 최적화(섀도 캘리브레이션) ->
최종 백테스트 -> 기각조건 판정 -> 시각화용 JSON 출력.

실행: python3 -m liquidation_strategy.run
"""

import json
import sys
from dataclasses import asdict

from .simulate_data import generate
from .backtest import run_backtest
from .optimize import grid_search_a, grid_search_b, A_WINRATE_RANGE, B_WINRATE_RANGE
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams
from .data_types import Side


def rejection_check_a(stats):
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    max_cl = stats.get("max_consec_losses", 0)

    checks.append({
        "rule": "30건 도달 시 비용후 평균 <= 0bps",
        "applicable": count >= 30,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps",
        "failed": count >= 30 and avg_bps <= 0,
    })
    checks.append({
        "rule": "50건 도달 시 평균 < +8bps 또는 승률 < 35%",
        "applicable": count >= 50,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps / 승률 {win_rate*100:.1f}%",
        "failed": count >= 50 and (avg_bps < 8 or win_rate < 0.35),
    })
    checks.append({
        "rule": "최대 연속 손실 10회 초과",
        "applicable": True,
        "value": f"{max_cl}회",
        "failed": max_cl > 10,
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def rejection_check_b(stats):
    checks = []
    count = stats.get("count", 0)
    avg_bps = stats.get("avg_bps", 0)
    win_rate = stats.get("win_rate", 0)
    avg_r = stats.get("avg_r", 0)

    checks.append({
        "rule": "20건 도달 시 비용후 평균 <= 0bps",
        "applicable": count >= 20,
        "value": f"{count}건 / 평균 {avg_bps:.1f}bps",
        "failed": count >= 20 and avg_bps <= 0,
    })
    checks.append({
        "rule": "40건 도달 시 승률 < 30% 또는 평균 R < 1.2",
        "applicable": count >= 40,
        "value": f"{count}건 / 승률 {win_rate*100:.1f}% / 평균 R {avg_r:.2f}",
        "failed": count >= 40 and (win_rate < 0.30 or avg_r < 1.2),
    })
    rejected = any(c["failed"] for c in checks)
    return checks, rejected


def trade_to_dict(t):
    return {
        "setup": t.setup,
        "side": t.side.value,
        "entry_ts": t.entry_ts,
        "entry_price": round(t.entry_price, 2),
        "exit_ts": t.exit_ts,
        "exit_price": round(t.exit_price, 2) if t.exit_price else None,
        "qty_fraction": round(t.qty_fraction, 3),
        "reason": t.reason,
        "r_multiple": round(t.r_multiple, 3) if t.r_multiple is not None else None,
        "bps": round(t.bps, 2) if t.bps is not None else None,
        "tag": t.tag,
    }


def build_equity_curve(trades):
    curve = []
    cum = 0.0
    for i, t in enumerate(sorted(trades, key=lambda x: x.exit_ts)):
        cum += t.bps * t.qty_fraction
        curve.append({"n": i + 1, "ts": t.exit_ts, "cum_bps": round(cum, 2), "setup": t.setup})
    return curve


def downsample_price(candles, step_minutes=60):
    return [
        {"ts": c.ts, "close": round(c.close, 1)}
        for i, c in enumerate(candles)
        if i % step_minutes == 0
    ]


def sample_window(candles, center_ts, before_min=90, after_min=180):
    idx_map = {c.ts: i for i, c in enumerate(candles)}
    # 가장 가까운 인덱스 찾기 (분봉 격자이므로 반올림)
    center_idx = min(range(len(candles)), key=lambda i: abs(candles[i].ts - center_ts))
    lo = max(0, center_idx - before_min)
    hi = min(len(candles), center_idx + after_min)
    return [
        {"ts": c.ts, "open": round(c.open, 1), "high": round(c.high, 1),
         "low": round(c.low, 1), "close": round(c.close, 1)}
        for c in candles[lo:hi]
    ]


def main(days=120, seed=7, out_path="liquidation_strategy_output.json"):
    print(f"[1/4] 합성 데이터 생성 (days={days}, seed={seed})...", file=sys.stderr)
    sim, baseline = generate(days=days, seed=seed)

    print("[2/4] 트리거 임계값 그리드서치 (캘리브레이션, 구조 고정)...", file=sys.stderr)
    results_a, best_a = grid_search_a(sim, baseline)
    results_b, best_b = grid_search_b(sim, baseline)

    a_params = CascadeAParams(**{**CascadeAParams().__dict__, **best_a["params"]})
    b_params = CascadeBParams(**{**CascadeBParams().__dict__, **best_b["params"]})

    print("[3/4] 최적 임계값으로 최종 통합 백테스트 (A+B 동시운용, 상호배제 규칙 적용)...", file=sys.stderr)
    engine = run_backtest(sim, baseline, a_params=a_params, b_params=b_params)
    summary = engine.summary()

    a_checks, a_rejected = rejection_check_a(summary.get("A", {}))
    b_checks, b_rejected = rejection_check_b(summary.get("B", {}))

    trades = sorted(engine.closed_trades, key=lambda t: t.exit_ts)
    equity_curve = build_equity_curve(trades)

    a_trades = [t for t in trades if t.setup == "A"]
    b_trades = [t for t in trades if t.setup == "B"]

    sample_a = sample_window(sim.candles, a_trades[0].entry_ts) if a_trades else []
    sample_b = sample_window(sim.candles, b_trades[0].entry_ts, before_min=240, after_min=300) if b_trades else []

    print("[4/4] JSON 출력...", file=sys.stderr)
    out = {
        "meta": {
            "days": days,
            "seed": seed,
            "n_candles": len(sim.candles),
            "n_injected_cascades": sum(1 for e in sim.events if e["type"] == "A"),
            "n_injected_traps": sum(1 for e in sim.events if e["type"] == "B"),
            "roundtrip_cost_bps": engine.cost_bps,
            "baseline_notional_per_hour": baseline,
        },
        "optimized_params": {
            "A": best_a["params"],
            "B": best_b["params"],
        },
        "grid_search": {
            "A": [{"params": r["params"], "stats": r["stats"]} for r in results_a],
            "B": [{"params": r["params"], "stats": r["stats"]} for r in results_b],
            "A_winrate_target": A_WINRATE_RANGE,
            "B_winrate_target": B_WINRATE_RANGE,
        },
        "summary": summary,
        "rejection": {
            "A": {"checks": a_checks, "rejected": a_rejected},
            "B": {"checks": b_checks, "rejected": b_rejected},
        },
        "trades": [trade_to_dict(t) for t in trades],
        "equity_curve": equity_curve,
        "price_overview": downsample_price(sim.candles, step_minutes=60),
        "sample_window_a": sample_a,
        "sample_window_b": sample_b,
        "sample_trade_a": trade_to_dict(a_trades[0]) if a_trades else None,
        "sample_trade_b": trade_to_dict(b_trades[0]) if b_trades else None,
    }

    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"완료 -> {out_path}", file=sys.stderr)
    return out


if __name__ == "__main__":
    main()
