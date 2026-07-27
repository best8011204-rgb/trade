"""실시간 Binance 선물 스트림 -> StrategyEngine -> 섀도(가상 체결) 로깅.

명세서 5장 1단계("섀도 단계: 라이브 스트림에서 시그널만 기록, 가상 체결")를
그대로 구현한다. 실제 주문을 내지 않는다 — aggTrade/kline 웹소켓과
openInterest REST 폴링을 엔진에 연결해 트리거·체결을 계산만 하고 JSONL로
기록한다. forceOrder(청산 틱)는 감사용으로만 기록한다 — Setup A/B/C 전부
캔들+거래량+OI만으로 동작하므로 이 스트림이 안 들어와도 트레이딩 로직엔
영향이 없다(setup_a.py 재설계 참고).

[v2 변경점]
- kline 구독을 1m 단일에서 KLINE_INTERVALS = (1m, 5m, 1h, 1d) 멀티로 확장.
  * 엔진(StrategyEngine)은 여전히 1m 확정봉만 소비한다 (전략 로직 불변).
  * 5m/1h/1d 확정봉은 GUI 차트 표시 전용이며, on_display_candle 훅이
    설정된 경우에만 전달된다 (훅 미설정 시 기존과 100% 동일하게 동작).
- on_display_candle(interval: str, candle: dict) 훅 추가. GUI 브리지
  (gui/live_engine_bridge.py)가 이 훅에 EventBus.publish를 꽂는다.

이 코드는 인터넷이 열린 로컬 머신/서버에서 실행해야 한다:

    pip install -r requirements.txt
    python3 -m liquidation_strategy.live_feed

Ctrl-C로 종료. 종료 전까지 주기적으로 liquidation_strategy_output_live.json
(대시보드 스키마)과 logs/*.jsonl(원시 이벤트 로그)을 갱신한다.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque

import websockets

from . import binance_client as bc
from .data_types import Candle, OIPoint
from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams, DailyBoxRegime
from .report import build_report

STREAM_URL = "wss://fstream.binance.com/stream?streams={streams}"
SYMBOL = "btcusdt"
KLINE_INTERVALS = ("1m", "5m", "1h", "1d")   # 1m=엔진+차트, 나머지=차트 전용
OI_POLL_S = 300              # 5분
SNAPSHOT_EVERY_S = 60
CANDLE_HISTORY_MAX = 3000    # 대시보드용으로 보관할 최근 분봉 수


class LiveShadowRunner:
    def __init__(self, symbol=SYMBOL, out_json="liquidation_strategy_output_live.json",
                 log_dir="logs", a_params=None, b_params=None):
        self.symbol = symbol
        self.out_json = out_json
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self.engine = StrategyEngine(a_params or CascadeAParams(), b_params or CascadeBParams())
        self.candles = deque(maxlen=CANDLE_HISTORY_MAX)
        # 계층형 박스: 5분봉 120개(최근 12개 제외)로 실제 box_low/high를 뽑고,
        # 일봉 90일 레인지 압축 게이트가 "돌파 구간"이라 판단하면 박스 자체를
        # 없앤다(clear_box) — Setup B 신규 트리거 탐지가 자동으로 멈춘다.
        self.candles_5m = deque(maxlen=200)
        self.daily_regime = DailyBoxRegime()
        self.cvd_accum = 0.0
        self._n_closed_seen = 0
        self._a_log_seen = 0
        self._b_log_seen = 0

        # GUI 표시 전용 훅. 시그니처: fn(interval: str, candle: dict)
        # candle dict: {ts, open, high, low, close, volume, closed: True}
        self.on_display_candle = None

        self.event_log_path = os.path.join(log_dir, "raw_events.jsonl")
        self.trade_log_path = os.path.join(log_dir, "shadow_trades.jsonl")
        self.signal_log_path = os.path.join(log_dir, "signals.jsonl")
        self.oi_log_path = os.path.join(log_dir, "oi_history.jsonl")

    def _append_jsonl(self, path, obj):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- inbound events ----------------------------------------------
    def on_force_order_msg(self, data):
        """청산 원시 이벤트를 감사·기록용으로만 남긴다(logs/raw_events.jsonl) —
        Setup A/B/C 전부 forceOrder 없이 캔들+거래량+OI만으로 동작하므로,
        이 스트림이 아예 안 들어와도(지역 차단 등) 트레이딩 로직엔 영향이 없다."""
        o = data["o"]
        if o["s"] != self.symbol.upper():
            return
        self._append_jsonl(self.event_log_path, {"type": "forceOrder", **data})

    def on_agg_trade_msg(self, data):
        if data["s"] != self.symbol.upper():
            return
        qty = float(data["q"])
        delta = -qty if data["m"] else qty  # m=True: 매수자가 메이커 -> 공격적 매도
        self.cvd_accum += delta

    def on_kline_msg(self, data):
        """모든 kline 스트림(1m/5m/1h/1d)의 공용 진입점.

        - 엔진(전략 로직)은 여전히 확정(닫힌) 1m봉만 소비한다 (k["x"] == True,
          interval == "1m") — 트리거 구조를 바꾸지 않기 위해 절대 건드리지
          않는다.
        - 반면 GUI 표시용 훅(on_display_candle)에는 확정 여부와 무관하게
          모든 kline 업데이트를 전달한다. Binance는 진행 중인 봉도 초당 한 번
          꼴로 갱신을 보내주므로, 이 경로만으로 "1분마다"가 아니라 실시간에
          가깝게(초 단위) 가격/거래량이 갱신된다 — 추가 REST 폴링 없이 이미
          열려 있는 웹소켓만 활용한다.
        """
        k = data["k"]
        if k["s"] != self.symbol.upper():
            return
        interval = k["i"]
        closed = bool(k["x"])
        candle_dict = {
            "ts": k["t"] / 1000.0, "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
            "closed": closed,
        }
        if interval == "1d" and closed:
            self.daily_regime.on_daily_candle(candle_dict["ts"], candle_dict["high"],
                                               candle_dict["low"], candle_dict["close"])

        if interval == "5m" and closed:
            self.candles_5m.append(candle_dict)
            self._update_5m_box()

        if interval == "1m" and closed:
            c = Candle(ts=candle_dict["ts"], open=candle_dict["open"], high=candle_dict["high"],
                       low=candle_dict["low"], close=candle_dict["close"],
                       volume=candle_dict["volume"], cvd_delta=self.cvd_accum)
            self.engine.a.on_cvd_delta(c.ts, self.cvd_accum)
            self.cvd_accum = 0.0

            self.engine.on_candle(c, oi_now=self._latest_oi)

            self.candles.append(c)
            self._flush_new_logs()

        if self.on_display_candle is not None:
            try:
                self.on_display_candle(interval, candle_dict)
            except Exception as e:  # 표시 훅 오류가 엔진을 죽이면 안 된다
                print(f"[display_hook] error: {e}", file=sys.stderr)

    def _update_5m_box(self):
        """실제 박스(box_low/high)는 5분봉 120개 중 최근 12개를 뺀
        120~13번째 구간(과거 확정 구간)으로만 계산한다. 일봉 90일 레인지
        압축 게이트(daily_regime)가 "돌파 구간"이라 판단하면 박스 자체가
        존재하지 않는 것으로 취급한다(clear_box) — 새 구간이 형성되지 않은
        상태이므로 Setup B의 신규 트리거 탐지를 자동으로 멈춘다."""
        p = self.engine.b.p
        n = len(self.candles_5m)
        end = n - p.box_5m_exclude_recent
        start = end - p.box_5m_bars
        if start < 0 or end <= start:
            self.engine.clear_box()
            return
        window = list(self.candles_5m)[start:end]
        box_low = min(x["low"] for x in window)
        box_high = max(x["high"] for x in window)
        if self.daily_regime.is_range_regime(p):
            self.engine.set_box(box_low, box_high)
        else:
            self.engine.clear_box()

    _latest_oi = None

    def on_oi_poll(self, oi_value, ts):
        self._latest_oi = oi_value
        self._append_jsonl(self.oi_log_path, {"ts": ts, "oi": oi_value})
        self.engine.on_oi(OIPoint(ts=ts, oi=oi_value))

    def _flush_new_logs(self):
        # 새로 닫힌 트레이드 기록
        while self._n_closed_seen < len(self.engine.closed_trades):
            t = self.engine.closed_trades[self._n_closed_seen]
            self._append_jsonl(self.trade_log_path, {
                "setup": t.setup, "side": t.side.value, "entry_ts": t.entry_ts,
                "entry_price": t.entry_price, "exit_ts": t.exit_ts, "exit_price": t.exit_price,
                "reason": t.reason, "bps": t.bps, "r_multiple": t.r_multiple, "tag": t.tag,
            })
            self._n_closed_seen += 1
        for log, seen_attr in ((self.engine.a.log, "_a_log_seen"), (self.engine.b.log, "_b_log_seen")):
            seen = getattr(self, seen_attr)
            while seen < len(log):
                ts, msg = log[seen]
                self._append_jsonl(self.signal_log_path, {"ts": ts, "msg": msg})
                seen += 1
            setattr(self, seen_attr, seen)

    # ---- snapshot ------------------------------------------------------
    def dump_snapshot(self):
        meta = {
            "symbol": self.symbol.upper(),
            "mode": "live_shadow",
            "generated_at": time.time(),
            "n_candles_buffered": len(self.candles),
            "roundtrip_cost_bps": self.engine.cost_bps,
            "data_source": "binance_live_websocket_real",
        }
        out = build_report(self.engine, list(self.candles), meta)
        tmp = self.out_json + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
        os.replace(tmp, self.out_json)


async def oi_poll_loop(runner: LiveShadowRunner, symbol: str):
    while True:
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, bc.get_open_interest, symbol.upper())
            runner.on_oi_poll(float(data["openInterest"]), time.time())
        except Exception as e:
            print(f"[oi_poll] error: {e}", file=sys.stderr)
        await asyncio.sleep(OI_POLL_S)


async def snapshot_loop(runner: LiveShadowRunner):
    while True:
        await asyncio.sleep(SNAPSHOT_EVERY_S)
        try:
            runner.dump_snapshot()
        except Exception as e:
            print(f"[snapshot] error: {e}", file=sys.stderr)


def build_stream_names(symbol: str):
    """구독할 combined stream 이름 목록. kline은 KLINE_INTERVALS 전체."""
    names = [f"{symbol}@forceOrder", f"{symbol}@aggTrade"]
    names += [f"{symbol}@kline_{iv}" for iv in KLINE_INTERVALS]
    return names


async def stream_loop(runner: LiveShadowRunner, symbol: str, proxy=True):
    """proxy: websockets.connect()에 그대로 전달.
    True(기본) = HTTPS_PROXY/ALL_PROXY 환경변수 자동 감지(기존 동작과 동일).
    문자열(예: "socks5://127.0.0.1:1080") = 그 프록시로 이 웹소켓 연결만 강제 우회.
    지역 차단 등으로 fstream이 핸드셰이크는 되는데 데이터가 안 오는 경우 사용."""
    streams = "/".join(build_stream_names(symbol))
    url = STREAM_URL.format(streams=streams)
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, proxy=proxy, ping_interval=180, ping_timeout=60) as ws:
                print(f"[stream] connected: {url}", file=sys.stderr)
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    stream, data = msg.get("stream", ""), msg.get("data", {})
                    if stream.endswith("@forceOrder"):
                        runner.on_force_order_msg(data)
                    elif stream.endswith("@aggTrade"):
                        runner.on_agg_trade_msg(data)
                    elif "@kline_" in stream:
                        runner.on_kline_msg(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[stream] disconnected ({e}), retry in {backoff}s", file=sys.stderr)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main_async(symbol: str, out_json: str, log_dir: str, proxy=True):
    runner = LiveShadowRunner(symbol=symbol, out_json=out_json, log_dir=log_dir)
    await asyncio.gather(
        stream_loop(runner, symbol, proxy=proxy),
        oi_poll_loop(runner, symbol),
        snapshot_loop(runner),
    )


def main():
    from .bot_config import load_config

    cfg = load_config(quiet=True)
    ap = argparse.ArgumentParser(description="실시간 섀도 검증 러너 (실주문 없음)")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--out", default="liquidation_strategy_output_live.json")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--ws-proxy", default=cfg.get("ws_proxy") or None,
                     help='웹소켓 전용 프록시 (예: socks5://127.0.0.1:1080). '
                          '미지정 시 config.json의 ws_proxy 또는 HTTPS_PROXY 환경변수 자동 감지.')
    args = ap.parse_args()
    proxy = args.ws_proxy if args.ws_proxy else True
    try:
        asyncio.run(main_async(args.symbol, args.out, args.log_dir, proxy=proxy))
    except KeyboardInterrupt:
        print("\n종료.", file=sys.stderr)


if __name__ == "__main__":
    main()
