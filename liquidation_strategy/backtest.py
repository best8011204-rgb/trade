"""합성/실 데이터 스트림 위에서 StrategyEngine을 실행하는 러너.

명세서 5장의 '섀도 단계'(가상 체결, 라이브 전 검증)에 해당한다.
"""

from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams


def run_backtest(sim, baseline_notional_per_hour, a_params: CascadeAParams = None,
                  b_params: CascadeBParams = None, macro_blackouts=None, cost_bps=10.0):
    engine = StrategyEngine(a_params, b_params, macro_blackouts, cost_bps=cost_bps)
    engine.a.set_hourly_baseline(baseline_notional_per_hour)

    # 이벤트를 시간순으로 머지: candle(1분), force_order(가변), oi(5분), cvd(1분)
    events = []
    for c in sim.candles:
        events.append((c.ts, 0, "candle", c))
    for fo in sim.force_orders:
        events.append((fo.ts, -1, "force_order", fo))  # 캔들보다 먼저 처리(같은 분 안)
    for pt in sim.oi_points:
        events.append((pt.ts, 1, "oi", pt))
    for ts_, delta in sim.cvd_series:
        events.append((ts_, -1, "cvd", delta))

    events.sort(key=lambda e: (e[0], e[1]))

    box_by_ts = {b[0]: (b[1], b[2]) for b in sim.box_series}
    oi_by_ts = {o.ts: o.oi for o in sim.oi_points}
    latest_oi = None

    for ts_, _, kind, payload in events:
        if kind == "force_order":
            engine.on_force_order(payload)
        elif kind == "oi":
            latest_oi = payload.oi
            engine.on_oi(payload)
        elif kind == "cvd":
            engine.a.on_cvd_delta(ts_, payload)
        elif kind == "candle":
            lo_hi = box_by_ts.get(ts_)
            if lo_hi:
                engine.set_box(lo_hi[0], lo_hi[1])
            engine.on_candle(payload, oi_now=latest_oi)

    return engine
