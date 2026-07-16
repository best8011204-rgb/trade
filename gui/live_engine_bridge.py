"""실거래소(Binance USDT-M 선물) 실시간 스트림을 GUI에 연결하는 브리지.

engine_bridge.EngineRunner(합성 데이터)와 동일한 인터페이스(start/stop/
is_alive)와 동일한 EventBus 토픽("status"/"candle"/"candle_tf"/
"candle_history"/"position"/"signal"/"trade_closed"/"summary"/"intent"/
"conditions")을 발행한다. 따라서 BotController가 어느 러너를 쓰든
GUI(views/widgets)는 수정할 필요가 없다.

스레드 모델:
- Tkinter 메인 스레드는 그대로 GUI 전용.
- 이 러너는 데몬 스레드 하나를 만들고, 그 안에서 자체 asyncio 이벤트 루프를
  돌린다 (websockets 라이브러리가 asyncio 기반이므로).
- 스레드 경계를 넘는 유일한 통로는 EventBus.publish() — 내부적으로
  queue.Queue 적재만 하므로 어느 스레드에서 불러도 안전하다. 실제 위젯
  갱신은 메인 스레드의 EventBus.dispatch()(root.after 폴링)에서만 일어난다.

실주문은 전혀 내지 않는다 — LiveTradingRunner를 trader=None(페이퍼)으로
구동해 가상 체결만 한다. config.json 에 telegram_bot_token 이 있으면 같은
엔진을 공유하는 텔레그램 봇(/status, /set, /pause 등)도 이 러너의 asyncio
루프에서 함께 돈다 — run_all.py 하나로 GUI + 엔진 + 텔레그램이 모두 실행되는
이유. 이 모듈은 그 위에 EventBus 발행만 얹는다.

주의: 이 코드는 fstream.binance.com 웹소켓과 fapi.binance.com REST
아웃바운드가 열린 환경(로컬 PC, VPS 등)에서만 동작한다.
"""

import asyncio
import sys
import threading
import time

from liquidation_strategy import binance_client as bc
from liquidation_strategy.live_feed import (
    stream_loop, oi_poll_loop, snapshot_loop, KLINE_INTERVALS,
)
from liquidation_strategy.live_trade import LiveTradingRunner
from liquidation_strategy.bot_config import load_config
from liquidation_strategy.telegram_bot import TelegramBot, BotState, CommandHandler
from liquidation_strategy.setup_a import CascadeAParams
from liquidation_strategy.setup_b import CascadeBParams
from liquidation_strategy.setup_c import ParamsC
from liquidation_strategy.live_setup_c import LiveSetupCRunner

HISTORY_LIMIT = 300           # 시작 시 interval별 REST 백필 봉 수 (차트 초기 표시용)
SUMMARY_EVERY_N_CANDLES = 5   # 1m 확정봉 N개마다 summary 발행
REST_POLL_S = 60              # 웹소켓 폴백: 확정봉 REST 폴링 주기 (1분)
OI_DISPLAY_POLL_S = 60        # GUI 표시용 OI 폴링 주기 (엔진용 5분 폴링과 별개)
CONDITIONS_POLL_S = 1         # 트리거 하위조건 체크리스트 갱신 주기 (실시간 표시용)


class LiveEngineRunner:
    """LiveShadowRunner(asyncio)를 EventBus 세계로 감싸는 러너."""

    def __init__(self, bus, symbol="BTCUSDT", a_params=None, b_params=None, c_params=None,
                 out_json="liquidation_strategy_output_live.json", log_dir="logs"):
        self.bus = bus
        self.symbol = symbol.upper()

        # config.json 로드 (없으면 기본값) — 텔레그램 토큰/상태파일 공유
        self._cfg = load_config(quiet=True)
        self.runner = LiveTradingRunner(
            symbol=symbol.lower(),
            out_json=out_json, log_dir=log_dir,
            a_params=a_params or CascadeAParams(),
            b_params=b_params or CascadeBParams(),
            trader=None,  # 실주문 비활성 — 페이퍼(가상 체결) 전용
            trade_usdt=float(self._cfg.get("trade_usdt", 100.0)),
        )
        # Setup C: StrategyEngine과 독립 — 실제 5분봉(btcusdt@kline_5m) 확정봉을
        # 그대로 받아 구동한다. 실주문 없음(A/B와 동일 원칙).
        self.c_runner = LiveSetupCRunner(c_params or ParamsC())

        # 텔레그램: 토큰이 있으면 같은 엔진을 공유하는 봇을 함께 구동
        self._tg_bot = None
        self._tg_handler = None
        if self._cfg.get("telegram_bot_token"):
            state = BotState(self._cfg.get("state_file", "bot_state.json"))
            if self._cfg.get("telegram_chat_id"):
                state.data["chat_id"] = int(self._cfg["telegram_chat_id"])
            state.apply_param_overrides(self.runner.engine)   # /set 값 복원
            self.runner.paused = bool(state.data.get("paused"))
            self._tg_bot = TelegramBot(self._cfg["telegram_bot_token"], state)
            self._tg_handler = CommandHandler(self.runner, state, bus=self.bus)
            self.runner.notify = self._tg_bot.send

        self._loop = None
        self._thread = None
        self._stopping = threading.Event()

        # 로그 커서 (EngineRunner._flush_trades/_flush_signals와 동일 패턴)
        self._n_closed_seen = 0
        self._a_log_seen = 0
        self._b_log_seen = 0
        self._c_log_seen = 0
        self._c_closed_seen = 0
        self._candle_count = 0

        self._install_hooks()

    # ------------------------------------------------------------------
    # LiveShadowRunner 콜백 지점에 EventBus 발행을 끼워 넣는다.
    # 엔진/섀도 로직 자체는 원본 그대로 실행된다.
    # ------------------------------------------------------------------
    def _install_hooks(self):
        # 1) 모든 타임프레임 확정봉 -> "candle_tf". 5분 확정봉은 Setup C도 구동한다
        #    (StrategyEngine과 무관 — live_setup_c.LiveSetupCRunner가 독립적으로 처리).
        def on_display_candle(interval, candle):
            self.bus.publish("candle_tf", {"interval": interval, "candle": candle})
            if interval == "5m" and candle.get("closed"):
                self.c_runner.on_confirmed_5m_candle(candle)
                self._flush_trades()
                self._flush_signals()
                self._publish_position()
                self._publish_intent()
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
            self._publish_position()
            self._publish_intent()
            if self._candle_count % SUMMARY_EVERY_N_CANDLES == 0:
                s = eng.summary()
                s["C"] = self.c_runner.combined_summary()
                self.bus.publish("summary", s)
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

        # 4) OI 5분 폴링 -> "oi" (대시보드 OI 서브차트/라벨)
        orig_on_oi = self.runner.on_oi_poll

        def on_oi_poll(oi_value, ts):
            orig_on_oi(oi_value, ts)
            self.bus.publish("oi", {"ts": ts, "oi": oi_value})
        self.runner.on_oi_poll = on_oi_poll

    def _publish_position(self):
        eng = self.runner.engine
        self.bus.publish("position", {
            "open_legs": _serialize_legs(eng.open_legs) + self.c_runner.open_legs_view(),
        })

    def _publish_intent(self):
        eng = self.runner.engine
        a_text, a_level = eng.a.describe()
        b_text, b_level = eng.b.describe()
        self.bus.publish("intent", {
            "a_text": a_text, "a_level": a_level,
            "b_text": b_text, "b_level": b_level,
            "c_text": self.c_runner.describe(), "c_level": None,
        })

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

        c_trades = self.c_runner.closed_trades
        while self._c_closed_seen < len(c_trades):
            t = c_trades[self._c_closed_seen]
            self.bus.publish("trade_closed", dict(t))
            self._c_closed_seen += 1

    def _flush_signals(self):
        for log, seen_attr, setup in (
            (self.runner.engine.a.log, "_a_log_seen", "A"),
            (self.runner.engine.b.log, "_b_log_seen", "B"),
            (self.c_runner.log, "_c_log_seen", "C"),
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

    @property
    def engine(self):
        """EngineRunner(합성)와 동일한 위치에서 엔진에 접근할 수 있게 하는
        얇은 위임 — Settings 페이지가 모드와 무관하게 controller.runner.engine
        으로 통일해서 읽을 수 있다."""
        return self.runner.engine

    def stop_setup(self, setup: str):
        """Settings 페이지 "중지" 버튼. GUI(메인) 스레드에서 호출되므로,
        엔진을 실제로 만지는 작업은 asyncio 루프 스레드에 스레드세이프하게
        예약한다 (call_soon_threadsafe)."""
        if setup == "C":
            self._call_threadsafe(self.c_runner.stop)
        else:
            self._call_threadsafe(lambda: self.runner.engine.stop_setup(setup))

    def restart_setup(self, setup: str, params):
        """Settings 페이지 "적용" — 실행 중인 러너에 새 파라미터를 즉시 반영."""
        if setup == "C":
            self._call_threadsafe(lambda: self.c_runner.restart(params))
        else:
            self._call_threadsafe(lambda: self.runner.engine.restart_setup(setup, params))

    def _call_threadsafe(self, fn):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(fn)
        else:
            fn()  # 루프가 아직 안 떴으면(REST 백필 중 등) 직접 호출해도 안전

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

        # 2) 웹소켓 스트림: 자체 asyncio 루프 (+ 텔레그램 봇, 토큰 있을 때)
        tg_note = " · 텔레그램 ON" if self._tg_bot else ""
        self.bus.publish("status", {
            "running": True, "connected": True,
            "message": f"실시간 연결 (웹소켓 + 1분 REST 폴백, 섀도 모드, 실주문 없음{tg_note}) — {self.symbol}",
        })
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        tasks = [
            stream_loop(self.runner, self.runner.symbol),
            oi_poll_loop(self.runner, self.runner.symbol),
            snapshot_loop(self.runner),
            self._rest_kline_poll_loop(),
            self._oi_display_poll_loop(),
            self._conditions_poll_loop(),
        ]
        if self._tg_bot:
            tasks.append(self._tg_bot.poll_loop(self._tg_handler.handle))
            self._tg_bot.send(f"🤖 봇 시작 (GUI) — {self.symbol} · 페이퍼(실주문 비활성)\n"
                              "/help 로 명령 확인")
        try:
            self._loop.run_until_complete(asyncio.gather(*tasks))
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

    async def _rest_kline_poll_loop(self):
        """웹소켓이 막힌 환경 폴백: 1분마다 확정봉을 REST로 받아 동일 경로에 주입.

        - 각 타임프레임 최근 2봉을 조회해 '아직 처리 안 된 확정봉'만
          runner.on_kline_msg(웹소켓과 동일한 메시지 형태)로 흘린다.
        - 1m 중복 방지: runner.candles[-1].ts 이하는 스킵 (웹소켓이 정상이면
          이 폴링은 사실상 아무 것도 주입하지 않는다).
        - 5m/1h/1d는 표시 전용이며 차트가 같은 ts 봉을 교체 처리하므로
          웹소켓과 겹쳐도 무해하다.
        """
        loop = asyncio.get_running_loop()
        last_ts = {iv: None for iv in KLINE_INTERVALS}
        while True:
            await asyncio.sleep(REST_POLL_S)
            for iv in KLINE_INTERVALS:
                try:
                    raw = await loop.run_in_executor(
                        None, lambda iv=iv: bc.get_klines(self.symbol, interval=iv, limit=2))
                except Exception as e:
                    print(f"[rest_poll] {iv} error: {e}", file=sys.stderr)
                    break  # 네트워크 문제면 이번 라운드 전체 스킵
                now_ms = time.time() * 1000
                for k in raw:
                    if float(k[6]) > now_ms:
                        continue  # 미확정(진행 중) 봉 제외
                    ts = k[0] / 1000.0
                    if iv == "1m":
                        if self.runner.candles and ts <= self.runner.candles[-1].ts:
                            continue  # 웹소켓/이전 폴링이 이미 처리한 봉
                    elif last_ts[iv] is not None and ts <= last_ts[iv]:
                        continue
                    last_ts[iv] = ts
                    self.runner.on_kline_msg({"k": {
                        "s": self.symbol, "i": iv, "x": True, "t": k[0],
                        "o": k[1], "h": k[2], "l": k[3], "c": k[4], "v": k[5],
                    }})

    async def _conditions_poll_loop(self):
        """T1~T4(A)/T1~T3(B) 하위 조건 체크리스트를 실시간(1초 주기)으로
        갱신한다. describe()의 상태 텍스트와 달리 conditions()는 시간 경과
        자체가 조건(무청산 경과, 반등 유지 등)이라 캔들 틱(1분)만으로는 GUI가
        갱신 시점 사이에 뒤처져 보인다 — 실제 벽시계 시간(time.time())으로
        매초 재계산해 정확한 실시간 상태를 보여준다."""
        eng = self.runner.engine
        while True:
            await asyncio.sleep(CONDITIONS_POLL_S)
            try:
                now = time.time()
                self.bus.publish("conditions", {
                    "a_conditions": eng.a.conditions(now),
                    "b_conditions": eng.b.conditions(now),
                    "c_conditions": self.c_runner.last_conditions,
                })
            except Exception as e:
                print(f"[conditions_poll] error: {e}", file=sys.stderr)

    async def _oi_display_poll_loop(self):
        """GUI 표시용 OI 1분 폴링. 엔진에는 넣지 않는다 —
        엔진용 OI는 기존 oi_poll_loop(5분)가 그대로 담당한다 (전략 로직 불변).
        """
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(OI_DISPLAY_POLL_S)
            try:
                data = await loop.run_in_executor(
                    None, bc.get_open_interest, self.symbol)
                self.bus.publish("oi", {"ts": time.time(), "oi": float(data["openInterest"])})
            except Exception as e:
                print(f"[oi_display_poll] error: {e}", file=sys.stderr)

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

        # OI 히스토리 (5분 주기): 실패해도 라이브 폴링으로 채워지므로 치명적이지 않다
        try:
            hist = bc.get_open_interest_hist(self.symbol, period="5m", limit=HISTORY_LIMIT)
            points = [(float(o["timestamp"]) / 1000.0, float(o["sumOpenInterest"]))
                      for o in hist]
            if points:
                self.bus.publish("oi_history", {"points": points})
        except Exception as e:
            print(f"[live_runner] OI 히스토리 백필 실패(비치명): {e}", file=sys.stderr)


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
