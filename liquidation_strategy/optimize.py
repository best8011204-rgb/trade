"""트리거 임계값 그리드서치.

명세서 4장 각주: "캘리브레이션은 트리거 임계값 조정만 허용하고 진입/청산
구조 변경은 불허." 이 원칙을 코드로 강제하기 위해, 최적화 대상은 감지
임계값(price_drop_atr_mult, oi_drop_quantile, rvol_quantile, rebound_pct,
oi_increase_pct)으로 한정하고, 손절/목표/트랜치 비율 등 실행 구조는
setup_a.py / setup_b.py / engine.py 에 고정된 채로 손대지 않는다.

목적함수: 사전 예측 범위(승률·손익비) 내에서 비용 후 기대값(avg_bps)을
최대화하는 조합을 찾는다. 표본이 기각 조건 최소 건수에 못 미치는 조합은
평가에서 제외한다.
"""

import itertools
from .backtest import run_backtest
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams

A_GRID = {
    "price_drop_atr_mult": [1.0, 1.5, 2.5],   # 구 vol_multiplier 대체(setup_a.py 재설계 — OI+거래량+ATR 프록시)
    "oi_drop_quantile": [0.02, 0.05, 0.10],
    "rvol_quantile": [0.85, 0.90, 0.95],
    "rebound_pct": [0.0005, 0.0008, 0.0015],
}

B_GRID = {
    "oi_increase_pct": [0.004, 0.006, 0.010],
    "retest_window_s": [15 * 60, 30 * 60, 45 * 60],
}

# 사전 예측 범위 (명세서 1장/2장) — 최적화 시 이 범위를 벗어나면 감점
A_WINRATE_RANGE = (0.45, 0.60)
B_WINRATE_RANGE = (0.40, 0.55)
A_MIN_COUNT = 30   # 섀도 최소 건수
B_MIN_COUNT = 20


def _score(stats, winrate_range, min_count):
    if not stats or stats.get("count", 0) < min_count:
        return None
    wr = stats["win_rate"]
    in_range = winrate_range[0] <= wr <= winrate_range[1]
    penalty = 0.0 if in_range else -abs(wr - min(max(wr, winrate_range[0]), winrate_range[1])) * 500
    return stats["avg_bps"] + penalty


def grid_search_a(sim, baseline, base_params: CascadeAParams = None):
    base_params = base_params or CascadeAParams()
    results = []
    keys = list(A_GRID.keys())
    for combo in itertools.product(*A_GRID.values()):
        overrides = dict(zip(keys, combo))
        p = CascadeAParams(**{**base_params.__dict__, **overrides})
        engine = run_backtest(sim, baseline, a_params=p, b_params=CascadeBParams())
        stats = engine.summary().get("A", {})
        score = _score(stats, A_WINRATE_RANGE, A_MIN_COUNT)
        results.append({"params": overrides, "stats": stats, "score": score})
    valid = [r for r in results if r["score"] is not None]
    best = max(valid, key=lambda r: r["score"]) if valid else max(results, key=lambda r: r["stats"].get("count", 0))
    return results, best


def grid_search_b(sim, baseline, base_params: CascadeBParams = None):
    base_params = base_params or CascadeBParams()
    results = []
    keys = list(B_GRID.keys())
    for combo in itertools.product(*B_GRID.values()):
        overrides = dict(zip(keys, combo))
        p = CascadeBParams(**{**base_params.__dict__, **overrides})
        engine = run_backtest(sim, baseline, a_params=CascadeAParams(), b_params=p)
        stats = engine.summary().get("B", {})
        score = _score(stats, B_WINRATE_RANGE, B_MIN_COUNT)
        results.append({"params": overrides, "stats": stats, "score": score})
    valid = [r for r in results if r["score"] is not None]
    best = max(valid, key=lambda r: r["score"]) if valid else max(results, key=lambda r: r["stats"].get("count", 0))
    return results, best
