"""실시간 Binance 선물 스트림 -> StrategyEngine -> 섀도(가상 체결) 로깅.

명세서 5장 1단계("섀도 단계: 라이브 스트림에서 시그널만 기록, 가상 체결")를
그대로 구현한다. 실제 주문을 내지 않는다 — forceOrder/aggTrade/kline_1m
웹소켓과 openInterest REST 폴링을 엔진에 연결해 트리거·체결을 계산만 하고
JSONL로 기록한다.

이 세션의 샌드박스는 WebSocket 업그레이드와 fapi.binance.com 아웃바운드가
막혀 있어 여기서 직접 실행할 수 없다. 인터넷이 열린 로컬 머신/서버에서:

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
from .data_types import ForceOrder, Candle, OIPoint
from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams
from .report import build_report

STREAM_URL = "wss://fstream.binance.com/stream?streams={streams}"
SYMBOL = "btcusdt"
BOX_WINDOW_MIN = 240        # 4h 박스
BASELINE_WINDOW_S = 24 * 3600
OI_POLL_S = 300              # 5분
SNAPSHOT_EVERY_S = 60
CANDLE_HISTORY_MAX = 3000    # 대시보드용으로 보관할 최근 분봉 수


class RollingBaseline:
    """T1 임계값 비교용 '최근 24h 시간당 평균 청산 금액'을 라이브로 추정."""

    def __init__(self, window_s=BASELINE_WINDOW_S):
        self.window_s = window_s
        self.buf = deque()  # (ts, notional)

    def add(self, ts, notional):
        self.buf.append((ts, notional))
        self._prune(ts)

    def _prune(self, ts):
        while self.buf and ts - self.buf[0][0] > self.window_s:
            self.buf.popleft()

    def per_hour(self, ts):
        self._prune(ts)
        if not self.buf:
            return None
        span_h = max(1.0, (ts - self.buf[0][0]) / 3600.0)
        total = sum(n for _, n in self.buf)
        return total / span_h


class LiveShadowRunner:
    def __init__(self, symbol=SYMBOL, out_json="liquidation_strategy_output_live.json",
                 log_dir="logs", a_params=None, b_params=None):
        self.symbol = symbol
        self.out_json = out_json
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self.engine = StrategyEngine(a_params or CascadeAParams(), b_params or CascadeBParams())
        self.baseline = RollingBaseline()
        self.candles = deque(maxlen=CANDLE_HISTORY_MAX)
        self.cvd_accum = 0.0
        self._n_closed_seen = 0
        self._a_log_seen = 0
        self._b_log_seen = 0

        self.event_log_path = os.path.join(log_dir, "raw_events.jsonl")
        self.trade_log_path = os.path.join(log_dir, "shadow_trades.jsonl")
        self.signal_log_path = os.path.join(log_dir, "signals.jsonl")

    def _append_jsonl(self, path, obj):
        with open(path, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ---- inbound events ----------------------------------------------
    def on_force_order_msg(self, data):
        o = data["o"]
        if o["s"] != self.symbol.upper():
            return
        fo = ForceOrder(ts=o["T"] / 1000.0, side=o["S"], price=float(o["ap"] or o["p"]), qty=float(o["q"]))
        self._append_jsonl(self.event_log_path, {"type": "forceOrder", **data})
        if fo.side == "SELL":
            self.baseline.add(fo.ts, fo.notional)
            self.engine.a.set_hourly_baseline(self.baseline.per_hour(fo.ts))
        self.engine.on_force_order(fo)
        self._flush_new_logs()

    def on_agg_trade_msg(self, data):
        if data["s"] != self.symbol.upper():
            return
        qty = float(data["q"])
        delta = -qty if data["m"] else qty  # m=True: 매수자가 메이커 -> 공격적 매도
        self.cvd_accum += delta

    def on_kline_msg(self, data):
        k = data["k"]
        if k["s"] != self.symbol.upper() or not k["x"]:
            return  # 확정(닫힌) 1분봉만 사용
        c = Candle(ts=k["t"] / 1000.0, open=float(k["o"]), high=float(k["h"]),
                   low=float(k["l"]), close=float(k["c"]), volume=float(k["v"]),
                   cvd_delta=self.cvd_accum)
        self.engine.a.on_cvd_delta(c.ts, self.cvd_accum)
        self.cvd_accum = 0.0

        self.candles.append(c)
        lo = max(0, len(self.candles) - BOX_WINDOW_MIN)
        window = list(self.candles)[lo:]
        box_low = min(x.low for x in window)
        box_high = max(x.high for x in window)
        self.engine.set_box(box_low, box_high)
        self.engine.on_candle(c, oi_now=self._latest_oi)
        self._flush_new_logs()

    _latest_oi = None

    def on_oi_poll(self, oi_value, ts):
        self._latest_oi = oi_value
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
        with open(tmp, "w") as f:
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


async def stream_loop(runner: LiveShadowRunner, symbol: str):
    streams = f"{symbol}@forceOrder/{symbol}@aggTrade/{symbol}@kline_1m"
    url = STREAM_URL.format(streams=streams)
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=180, ping_timeout=60) as ws:
                print(f"[stream] connected: {url}", file=sys.stderr)
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    stream, data = msg.get("stream", ""), msg.get("data", {})
                    if stream.endswith("@forceOrder"):
                        runner.on_force_order_msg(data)
                    elif stream.endswith("@aggTrade"):
                        runner.on_agg_trade_msg(data)
                    elif stream.endswith("@kline_1m"):
                        runner.on_kline_msg(data)
        except Exception as e:
            print(f"[stream] disconnected ({e}), retry in {backoff}s", file=sys.stderr)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main_async(symbol: str, out_json: str, log_dir: str):
    runner = LiveShadowRunner(symbol=symbol, out_json=out_json, log_dir=log_dir)
    await asyncio.gather(
        stream_loop(runner, symbol),
        oi_poll_loop(runner, symbol),
        snapshot_loop(runner),
    )


def main():
    ap = argparse.ArgumentParser(description="실시간 섀도 검증 러너 (실주문 없음)")
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--out", default="liquidation_strategy_output_live.json")
    ap.add_argument("--log-dir", default="logs")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args.symbol, args.out, args.log_dir))
    except KeyboardInterrupt:
        print("\n종료.", file=sys.stderr)


if __name__ == "__main__":
    main()
