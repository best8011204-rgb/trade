"""전체 파이프라인 실행(합성 데이터): 데이터 생성 -> 임계값 최적화(섀도 캘리브레이션) ->
최종 백테스트 -> 기각조건 판정 -> 시각화용 JSON 출력.

실데이터 연동은 run_live.py(실시간) / backfill.py(Setup B 과거 데이터) 참고.

실행: python3 -m liquidation_strategy.run
"""

import json
import sys

from .simulate_data import generate
from .backtest import run_backtest
from .optimize import grid_search_a, grid_search_b, A_WINRATE_RANGE, B_WINRATE_RANGE
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams
from .report import build_report


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

    print("[4/4] JSON 출력...", file=sys.stderr)
    meta = {
        "days": days,
        "seed": seed,
        "n_candles": len(sim.candles),
        "n_injected_cascades": sum(1 for e in sim.events if e["type"] == "A"),
        "n_injected_traps": sum(1 for e in sim.events if e["type"] == "B"),
        "roundtrip_cost_bps": engine.cost_bps,
        "baseline_notional_per_hour": baseline,
        "data_source": "synthetic",
    }
    grid_search = {
        "A": [{"params": r["params"], "stats": r["stats"]} for r in results_a],
        "B": [{"params": r["params"], "stats": r["stats"]} for r in results_b],
        "A_winrate_target": A_WINRATE_RANGE,
        "B_winrate_target": B_WINRATE_RANGE,
    }
    optimized_params = {"A": best_a["params"], "B": best_b["params"]}

    out = build_report(engine, sim.candles, meta, optimized_params, grid_search)

    with open(out_path, "w") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"완료 -> {out_path}", file=sys.stderr)
    return out


if __name__ == "__main__":
    main()
