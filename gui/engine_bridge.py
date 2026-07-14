"""합성 데이터(simulate_data.generate) 기반 백그라운드 엔진 러너.

GUI 위젯은 이 러너를 직접 참조하지 않고 EventBus만 구독한다 — 러너는
백그라운드 스레드에서 합성 스트림을 재생 속도(speed)에 맞춰 흘려보내며
StrategyEngine(원본 그대로)을 구동하고, 결과를 EventBus로 발행한다.

speed<=0 이면 지연 없이 최대 속도로 재생한다 (헤드리스 테스트용).
"""

import sys
import threading
import time

from liquidation_strategy.simulate_data import generate
from liquidation_strategy.engine import StrategyEngine
from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams

SUMMARY_EVERY_N_CANDLES = 60
MAX_SLEEP_S = 5.0  # 이벤트 간 간격이 큰 경우(청산 드문 구간) 단일 sleep 상한


class EngineRunner:
    """합성 데이터 시뮬레이션 러너 (오프라인, 네트워크 불필요, 실주문 없음)."""

    def __init__(self, bus, symbol="BTCUSDT", a_params=None, b_params=None,
                 speed=200.0, days=14, seed=7):
        self.bus = bus
        self.symbol = symbol
        self.speed = speed
        self.days = days
        self.seed = seed
        self.a_params = a_params or CascadeAParams()
        self.b_params = b_params or CascadeBParams()
        self._thread = None
        self._stopping = threading.Event()

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()

    # ------------------------------------------------------------------
    def _run(self):
        self.bus.publish("status", {
            "running": True, "connected": False,
            "message": f"합성 데이터 생성 중... ({self.symbol}, {self.days}일)",
        })
        try:
            sim, baseline = generate(days=self.days, seed=self.seed)
        except Exception as e:
            self.bus.publish("status", {
                "running": False, "connected": False,
                "message": f"합성 데이터 생성 실패: {e}",
            })
            return

        engine = StrategyEngine(self.a_params, self.b_params)
        events = self._build_timeline(sim)

        self.bus.publish("status", {
            "running": True, "connected": True,
            "message": f"합성 데이터 재생 중 (배속 {self.speed}x) — {self.symbol}",
        })

        latest_oi = None
        n_closed_seen = 0
        a_log_seen = 0
        b_log_seen = 0
        candle_count = 0
        prev_ts = events[0][0] if events else None

        for ts, kind, obj in events:
            if self._stopping.is_set():
                break
            if prev_ts is not None and self.speed and self.speed > 0:
                dt = (ts - prev_ts) / self.speed
                if dt > 0:
                    time.sleep(min(dt, MAX_SLEEP_S))
            prev_ts = ts

            if kind == 0:  # ForceOrder
                if obj.side == "SELL":
                    engine.a.set_hourly_baseline(baseline)
                engine.on_force_order(obj)
            elif kind == 1:  # OIPoint
                latest_oi = obj.oi
                engine.on_oi(obj)
            else:  # Candle
                c, (_box_ts, box_low, box_high) = obj
                engine.a.on_cvd_delta(c.ts, c.cvd_delta)
                engine.set_box(box_low, box_high)
                engine.on_candle(c, oi_now=latest_oi)
                candle_count += 1

                self.bus.publish("candle", {
                    "ts": c.ts, "open": c.open, "high": c.high, "low": c.low,
                    "close": c.close, "volume": c.volume,
                    "box_low": box_low, "box_high": box_high,
                })
                self.bus.publish("position", {"open_legs": _serialize_legs(engine.open_legs)})
                if candle_count % SUMMARY_EVERY_N_CANDLES == 0:
                    self.bus.publish("summary", engine.summary())

                n_closed_seen = self._flush_trades(engine, n_closed_seen)
                a_log_seen, b_log_seen = self._flush_signals(engine, a_log_seen, b_log_seen)

        self.bus.publish("summary", engine.summary())
        self.bus.publish("status", {
            "running": False, "connected": False,
            "message": "정지됨" if self._stopping.is_set() else "재생 완료",
        })

    def _build_timeline(self, sim):
        events = []
        for fo in sim.force_orders:
            events.append((fo.ts, 0, fo))
        for pt in sim.oi_points:
            events.append((pt.ts, 1, pt))
        for i, c in enumerate(sim.candles):
            events.append((c.ts, 2, (c, sim.box_series[i])))
        events.sort(key=lambda e: (e[0], e[1]))
        return events

    def _flush_trades(self, engine, n_closed_seen):
        trades = engine.closed_trades
        while n_closed_seen < len(trades):
            t = trades[n_closed_seen]
            self.bus.publish("trade_closed", {
                "setup": t.setup, "side": t.side.value, "entry_ts": t.entry_ts,
                "entry_price": t.entry_price, "exit_ts": t.exit_ts,
                "exit_price": t.exit_price, "reason": t.reason,
                "bps": t.bps, "r_multiple": t.r_multiple, "tag": t.tag,
            })
            n_closed_seen += 1
        return n_closed_seen

    def _flush_signals(self, engine, a_log_seen, b_log_seen):
        while a_log_seen < len(engine.a.log):
            ts, msg = engine.a.log[a_log_seen]
            self.bus.publish("signal", {"ts": ts, "msg": msg, "setup": "A"})
            a_log_seen += 1
        while b_log_seen < len(engine.b.log):
            ts, msg = engine.b.log[b_log_seen]
            self.bus.publish("signal", {"ts": ts, "msg": msg, "setup": "B"})
            b_log_seen += 1
        return a_log_seen, b_log_seen


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
