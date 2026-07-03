"""GUI와 liquidation_strategy.StrategyEngine 사이의 브리지.

엔진은 GUI를 전혀 모른다. 이 모듈이 백그라운드 스레드에서 엔진을 구동하고,
콜백 지점마다 EventBus에 이벤트를 발행한다. 스레드 경계를 넘는 유일한 통로는
EventBus.publish()이며, 내부적으로 queue.Queue에 적재하는 것이라 스레드
안전하다.

지금은 합성 데이터(simulate_data)로만 구동한다 — 실거래소 웹소켓
(liquidation_strategy/live_feed.py)은 네트워크가 열린 환경 전용이라 이
샌드박스에서 검증할 수 없기 때문이다. 실거래 연동 시에는 이 클래스와 동일한
이벤트 토픽("status"/"candle"/"position"/"signal"/"trade_closed"/"summary")을
발행하는 LiveEngineRunner를 추가하면 되며, GUI 쪽(views/widgets)은 수정할
필요가 없다 — live_feed.LiveShadowRunner의 콜백들(on_force_order_msg 등)에
bus.publish 호출만 끼워 넣는 방식이다.

이벤트 병합 순서는 liquidation_strategy/backtest.py의 검증된 규칙을 그대로
따른다: 같은 타임스탬프에서는 force_order/cvd를 candle보다 먼저 처리한다.
"""

import threading
import time

from liquidation_strategy.engine import StrategyEngine
from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams
from liquidation_strategy import simulate_data

SUMMARY_EVERY_N_CANDLES = 60
MAX_SLEEP_PER_STEP_S = 0.5


class EngineRunner:
    """합성 데이터로 StrategyEngine을 구동하는 백그라운드 러너."""

    def __init__(self, bus, symbol="BTCUSDT", a_params=None, b_params=None,
                 speed=200.0, days=14, seed=None):
        self.bus = bus
        self.symbol = symbol
        self.a_params = a_params or CascadeAParams()
        self.b_params = b_params or CascadeBParams()
        self.speed = speed          # 배속 (1.0 = 실시간, 0 = 최대속도)
        self.days = days
        self.seed = seed if seed is not None else int(time.time()) % 100000

        self.engine = StrategyEngine(self.a_params, self.b_params)
        self._stop_flag = threading.Event()
        self._thread = None

    def start(self):
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    def _run(self):
        self.bus.publish("status", {
            "running": True, "connected": True,
            "message": f"합성 데이터 시뮬레이션 시작 (symbol={self.symbol}, seed={self.seed})",
        })
        try:
            sim, baseline = simulate_data.generate(days=self.days, seed=self.seed)
        except Exception as e:
            self.bus.publish("status", {
                "running": False, "connected": False,
                "message": f"데이터 생성 실패: {e}",
            })
            return

        self.engine.a.set_hourly_baseline(baseline)
        box_by_ts = {b[0]: (b[1], b[2]) for b in sim.box_series}

        events = self._merge_streams(sim)
        n_closed_seen = 0
        n_a_log_seen = 0
        n_b_log_seen = 0
        candle_count = 0
        latest_oi = None
        prev_ts = None

        for ts, kind, payload in events:
            if self._stop_flag.is_set():
                break

            if prev_ts is not None and self.speed > 0:
                gap = (ts - prev_ts) / self.speed
                if gap > 0:
                    time.sleep(min(gap, MAX_SLEEP_PER_STEP_S))
            prev_ts = ts

            if kind == "force_order":
                self.engine.on_force_order(payload)
            elif kind == "oi":
                latest_oi = payload.oi
                self.engine.on_oi(payload)
            elif kind == "cvd":
                self.engine.a.on_cvd_delta(ts, payload)
            elif kind == "candle":
                lo_hi = box_by_ts.get(ts)
                if lo_hi:
                    self.engine.set_box(lo_hi[0], lo_hi[1])
                self.engine.on_candle(payload, oi_now=latest_oi)
                candle_count += 1

                self.bus.publish("candle", {
                    "ts": payload.ts, "close": payload.close,
                    "box_low": lo_hi[0] if lo_hi else None,
                    "box_high": lo_hi[1] if lo_hi else None,
                })
                self.bus.publish("position", {
                    "open_legs": _serialize_legs(self.engine.open_legs),
                })
                if candle_count % SUMMARY_EVERY_N_CANDLES == 0:
                    self.bus.publish("summary", self.engine.summary())

            n_closed_seen = self._flush_trades(n_closed_seen)
            n_a_log_seen = self._flush_signals(self.engine.a.log, n_a_log_seen, "A")
            n_b_log_seen = self._flush_signals(self.engine.b.log, n_b_log_seen, "B")

        self.bus.publish("summary", self.engine.summary())
        self.bus.publish("status", {
            "running": False, "connected": False,
            "message": "정지됨" if self._stop_flag.is_set() else "시뮬레이션 종료 (데이터 끝)",
        })

    @staticmethod
    def _merge_streams(sim):
        """backtest.py와 동일한 (ts, priority) 정렬 규칙: force_order/cvd가 candle보다 먼저."""
        events = []
        for c in sim.candles:
            events.append((c.ts, 0, "candle", c))
        for fo in sim.force_orders:
            events.append((fo.ts, -1, "force_order", fo))
        for pt in sim.oi_points:
            events.append((pt.ts, 1, "oi", pt))
        for ts_, delta in sim.cvd_series:
            events.append((ts_, -1, "cvd", delta))
        events.sort(key=lambda e: (e[0], e[1]))
        return [(ts_, kind, payload) for ts_, _, kind, payload in events]

    def _flush_trades(self, seen):
        trades = self.engine.closed_trades
        while seen < len(trades):
            t = trades[seen]
            self.bus.publish("trade_closed", {
                "setup": t.setup, "side": t.side.value, "entry_ts": t.entry_ts,
                "entry_price": t.entry_price, "exit_ts": t.exit_ts,
                "exit_price": t.exit_price, "reason": t.reason,
                "bps": t.bps, "r_multiple": t.r_multiple, "tag": t.tag,
            })
            seen += 1
        return seen

    def _flush_signals(self, log, seen, setup):
        while seen < len(log):
            ts, msg = log[seen]
            self.bus.publish("signal", {"ts": ts, "msg": msg, "setup": setup})
            seen += 1
        return seen


def _serialize_legs(open_legs):
    out = []
    for leg in open_legs:
        t = leg.trade
        out.append({
            "setup": t.setup, "side": t.side.value, "entry_price": t.entry_price,
            "qty_fraction": t.qty_fraction, "sl": leg.sl, "tp1": leg.tp1,
            "tp2": leg.tp2, "tp1_hit": leg.tp1_hit, "tag": t.tag,
        })
    return out
