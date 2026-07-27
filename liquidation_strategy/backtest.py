"""합성/실 데이터 스트림 위에서 StrategyEngine을 실행하는 러너.

명세서 5장의 '섀도 단계'(가상 체결, 라이브 전 검증)에 해당한다.
"""

from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams


def run_backtest(sim, baseline_notional_per_hour, a_params: CascadeAParams = None,
                  b_params: CascadeBParams = None, macro_blackouts=None, cost_bps=10.0):
    # baseline_notional_per_hour: Setup A는 더 이상 forceOrder 기반이 아니라 트리거에
    # 안 쓰인다(setup_a.py 재설계). 호출부(그리드서치/리포트 meta) 시그니처 호환을 위해
    # 인자는 그대로 받되, 여기서는 사용하지 않는다.
    engine = StrategyEngine(a_params, b_params, macro_blackouts, cost_bps=cost_bps)

    # 이벤트를 시간순으로 머지: candle(1분), oi(5분), cvd(1분)
    events = []
    for c in sim.candles:
        events.append((c.ts, 0, "candle", c))
    for pt in sim.oi_points:
        events.append((pt.ts, 1, "oi", pt))
    for ts_, delta in sim.cvd_series:
        events.append((ts_, -1, "cvd", delta))

    events.sort(key=lambda e: (e[0], e[1]))

    box_by_ts = {b[0]: (b[1], b[2]) for b in sim.box_series}
    latest_oi = None

    for ts_, _, kind, payload in events:
        if kind == "oi":
            latest_oi = payload.oi
            engine.on_oi(payload)
        elif kind == "cvd":
            engine.a.on_cvd_delta(ts_, payload)
        elif kind == "candle":
            box_low, box_high = box_by_ts.get(ts_, (None, None))
            if box_low is not None:
                engine.set_box(box_low, box_high)
            else:
                engine.clear_box()
            engine.on_candle(payload, oi_now=latest_oi)

    return engine
