"""실거래소(Binance USDT-M 선물) 실시간 스트림을 GUI에 연결하는 브리지.

engine_bridge.EngineRunner(합성 데이터)와 동일한 인터페이스(start/stop/
is_alive)와 동일한 EventBus 토픽("status"/"candle"/"candle_tf"/
"candle_history"/"position"/"signal"/"trade_closed"/"summary")을 발행한다.
따라서 BotController가 어느 러너를 쓰든 GUI(views/widgets)는 수정할 필요가
없다.

스레드 모델:
- Tkinter 메인 스레드는 그대로 GUI 전용.
- 이 러너는 데몬 스레드 하나를 만들고, 그 안에서 자체 asyncio 이벤트 루프를
  돌린다 (websockets 라이브러리가 asyncio 기반이므로).
- 스레드 경계를 넘는 유일한 통로는 EventBus.publish() — 내부적으로
  queue.Queue 적재만 하므로 어느 스레드에서 불러도 안전하다. 실제 위젯
  갱신은 메인 스레드의 EventBus.dispatch()(root.after 폴링)에서만 일어난다.

실주문은 전혀 내지 않는다 — live_feed.LiveShadowRunner의 섀도(가상 체결)
로직을 그대로 재사용한다. 이 모듈은 그 위에 EventBus 발행만 얹는다.

주의: 이 코드는 fstream.binance.com 웹소켓과 fapi.binance.com REST
아웃바운드가 열린 환경(로컬 PC, VPS 등)에서만 동작한다.
"""

import asyncio
import sys
import threading
import time

from liquidation_strategy import binance_client as bc
from liquidation_strategy.live_feed import (
    LiveShadowRunner, stream_loop, oi_poll_loop, snapshot_loop, KLINE_INTERVALS,
)
from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams

HISTORY_LIMIT = 300           # 시작 시 interval별 REST 백필 봉 수 (차트 초기 표시용)
SUMMARY_EVERY_N_CANDLES = 5   # 1m 확정봉 N개마다 summary 발행


class LiveEngineRunner:
    """LiveShadowRunner(asyncio)를 EventBus 세계로 감싸는 러너."""

    def __init__(self, bus, symbol="BTCUSDT", a_params=None, b_params=None,
                 out_json="liquidation_strategy_output_live.json", log_dir="logs"):
        self.bus = bus
        self.symbol = symbol.upper()
        self.runner = LiveShadowRunner(
            symbol=symbol.lower(),
            out_json=out_json, log_dir=log_dir,
            a_params=a_params or CascadeAParams(),
            b_params=b_params or CascadeBParams(),
        )
        self._loop = None
        self._thread = None
        self._stopping = threading.Event()

        # 로그 커서 (EngineRunner._flush_trades/_flush_signals와 동일 패턴)
        self._n_closed_seen = 0
        self._a_log_seen = 0
        self._b_log_seen = 0
        self._candle_count = 0

        self._install_hooks()

    # ------------------------------------------------------------------
    # LiveShadowRunner 콜백 지점에 EventBus 발행을 끼워 넣는다.
    # 엔진/섀도 로직 자체는 원본 그대로 실행된다.
    # ------------------------------------------------------------------
    def _install_hooks(self):
        # 1) 모든 타임프레임 확정봉 -> "candle_tf"
        def on_display_candle(interval, candle):
            self.bus.publish("candle_tf", {"interval": interval, "candle": candle})
        self.runner.on_display_candle = on_display_candle

        # 2) 1m 확정봉 처리 후 -> 기존 토픽("candle"/"position"/"summary") 발행
        orig_on_kline = self.runner.on_kline_msg

        def on_kline_msg(data):
            orig_on_kline(data)
            k = data.get("k", {})
            if k.get("i") != "1m" or not k.get("x"):
                return
            c = self.runner.candles[-1] if self.runner.candles else None
            if c is None:
                return
            self._candle_count += 1
            eng = self.runner.engine
            self.bus.publish("candle", {
                "ts": c.ts, "open": c.open, "high": c.high, "low": c.low,
                "close": c.close, "volume": c.volume,
                "box_low": eng.box_low, "box_high": eng.box_high,
            })
            self.bus.publish("position", {"open_legs": _serialize_legs(eng.open_legs)})
            if self._candle_count % SUMMARY_EVERY_N_CANDLES == 0:
                self.bus.publish("summary", eng.summary())
            self._flush_trades()
            self._flush_signals()
        self.runner.on_kline_msg = on_kline_msg

        # 3) forceOrder 처리 후에도 트레이드/시그널이 닫힐 수 있으므로 flush
        orig_on_fo = self.runner.on_force_order_msg

        def on_force_order_msg(data):
            orig_on_fo(data)
            self._flush_trades()
            self._flush_signals()
        self.runner.on_force_order_msg = on_force_order_msg

    def _flush_trades(self):
        trades = self.runner.engine.closed_trades
        while self._n_closed_seen < len(trades):
            t = trades[self._n_closed_seen]
            self.bus.publish("trade_closed", {
                "setup": t.setup, "side": t.side.value, "entry_ts": t.entry_ts,
                "entry_price": t.entry_price, "exit_ts": t.exit_ts,
                "exit_price": t.exit_price, "reason": t.reason,
                "bps": t.bps, "r_multiple": t.r_multiple, "tag": t.tag,
            })
            self._n_closed_seen += 1

    def _flush_signals(self):
        for log, seen_attr, setup in (
            (self.runner.engine.a.log, "_a_log_seen", "A"),
            (self.runner.engine.b.log, "_b_log_seen", "B"),
        ):
            seen = getattr(self, seen_attr)
            while seen < len(log):
                ts, msg = log[seen]
                self.bus.publish("signal", {"ts": ts, "msg": msg, "setup": setup})
                seen += 1
            setattr(self, seen_attr, seen)

    # ------------------------------------------------------------------
    # 생명주기
    # ------------------------------------------------------------------
    def start(self):
        self._stopping.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopping.set()
        if self._loop is not None:
            # asyncio 루프 스레드에 정지 요청 (스레드세이프)
            self._loop.call_soon_threadsafe(self._cancel_all_tasks)

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _cancel_all_tasks(self):
        for task in asyncio.all_tasks(self._loop):
            task.cancel()

    # ------------------------------------------------------------------
    def _thread_main(self):
        self.bus.publish("status", {
            "running": True, "connected": False,
            "message": f"실거래소 연동 준비 중... ({self.symbol}, REST 백필)",
        })

        # 1) REST 백필: 각 타임프레임 최근 HISTORY_LIMIT개 확정봉 -> 차트 초기화
        try:
            self._backfill_history()
        except Exception as e:
            self.bus.publish("status", {
                "running": False, "connected": False,
                "message": f"REST 백필 실패: {e} — 네트워크/지역 차단 여부를 확인하세요.",
            })
            return

        if self._stopping.is_set():
            self.bus.publish("status", {"running": False, "connected": False, "message": "정지됨"})
            return

        # 2) 웹소켓 스트림: 자체 asyncio 루프
        self.bus.publish("status", {
            "running": True, "connected": True,
            "message": f"실시간 스트림 연결 (섀도 모드, 실주문 없음) — {self.symbol}",
        })
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(asyncio.gather(
                stream_loop(self.runner, self.runner.symbol),
                oi_poll_loop(self.runner, self.runner.symbol),
                snapshot_loop(self.runner),
            ))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[live_runner] loop error: {e}", file=sys.stderr)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            self.bus.publish("status", {
                "running": False, "connected": False,
                "message": "정지됨" if self._stopping.is_set() else "스트림 종료",
            })

    def _backfill_history(self):
        """interval별 최근 확정봉을 REST로 받아 'candle_history'로 일괄 발행.

        Binance klines 응답의 마지막 원소는 미확정(진행 중) 봉이므로 제외한다.
        """
        for interval in KLINE_INTERVALS:
            if self._stopping.is_set():
                return
            raw = bc.get_klines(self.symbol, interval=interval, limit=HISTORY_LIMIT)
            now_ms = time.time() * 1000
            candles = [
                {"ts": k[0] / 1000.0, "open": float(k[1]), "high": float(k[2]),
                 "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                 "closed": True}
                for k in raw if float(k[6]) <= now_ms  # closeTime 지난 봉만 = 확정봉
            ]
            self.bus.publish("candle_history", {"interval": interval, "candles": candles})


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
