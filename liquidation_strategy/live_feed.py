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
from .data_types import Candle, OIPoint, ForceOrder
from .engine import StrategyEngine
from .setup_a import CascadeAParams
from .setup_b import CascadeBParams, HierarchicalBoxBuilder
from .report import build_report

STREAM_URL = "wss://fstream.binance.com/stream?streams={streams}"
SYMBOL = "btcusdt"
KLINE_INTERVALS = ("1m", "5m", "1h", "1d")   # 1m=엔진+차트, 나머지=차트 전용
OI_POLL_S = 300              # 5분
SNAPSHOT_EVERY_S = 60
CANDLE_HISTORY_MAX = 3000    # 대시보드용으로 보관할 최근 분봉 수
REST_POLL_S = 60              # 웹소켓 폴백: 확정봉 REST 폴링 주기 (1분)
REPLAY_LOOKBACK_S = 7200      # (재)시작 시 Setup A/B 캐스케이드 감시 상태를 따라잡기 위해
                              # 재생할 1분봉/OI 과거 구간
REPLAY_5M_BARS = 150          # 5분봉 박스(120개+최근 12개 제외=132개 필요) 시딩용 여유(12.5시간)
REPLAY_DAILY_DAYS = 150       # 일봉 레인지 게이트(90일 lookback+14일 ATR=최소 104일 필요) 시딩용 여유


class LiveShadowRunner:
    def __init__(self, symbol=SYMBOL, out_json="liquidation_strategy_output_live.json",
                 log_dir="logs", a_params=None, b_params=None, a_impl=None):
        self.symbol = symbol
        self.out_json = out_json
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self.engine = StrategyEngine(a_params or CascadeAParams(), b_params or CascadeBParams(), a_impl=a_impl)
        self.candles = deque(maxlen=CANDLE_HISTORY_MAX)
        # 계층형 박스: 5분봉 120개(최근 12개 제외)로 실제 box_low/high를 뽑고,
        # 일봉 90일 레인지 압축 게이트가 "돌파 구간"이라 판단하면 박스 자체를
        # 없앤다(clear_box) — Setup B 신규 트리거 탐지가 자동으로 멈춘다.
        # (backfill.py/simulate_data.py 등 백테스트 경로와 완전히 동일한 로직을
        # 공유하기 위해 HierarchicalBoxBuilder 하나로 계산한다.)
        self.box_builder = HierarchicalBoxBuilder(self.engine.b.p)
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
        """청산 원시 이벤트를 감사·기록용으로 남기고(logs/raw_events.jsonl),
        엔진에도 전달한다. 기본 Setup A(setup_a.py)는 forceOrder 없이 캔들+
        거래량+OI만으로 동작하므로 이 스트림이 안 들어와도(지역 차단 등)
        영향이 없다 — engine.on_force_order()가 self.a에 on_force_order가
        없으면 조용히 무시한다. run_bot.py가 setup_a_legacy(forceOrder 기반)를
        a_impl로 주입한 경우에만 실제로 쓰인다."""
        o = data["o"]
        if o["s"] != self.symbol.upper():
            return
        self._append_jsonl(self.event_log_path, {"type": "forceOrder", **data})
        fo = ForceOrder(ts=o["T"] / 1000.0, side=o["S"], price=float(o["ap"] or o["p"]), qty=float(o["q"]))
        self.engine.on_force_order(fo)

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
            self.box_builder.on_daily_candle(candle_dict["ts"], candle_dict["high"],
                                              candle_dict["low"], candle_dict["close"])

        if interval == "5m" and closed:
            self.box_builder.on_5m_candle(candle_dict["high"], candle_dict["low"])
            if self.box_builder.box_low is not None:
                self.engine.set_box(self.box_builder.box_low, self.box_builder.box_high)
            else:
                self.engine.clear_box()

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


def replay_recent_history(runner: "LiveShadowRunner", symbol: str, stopping_event=None):
    """(재)시작 시점 이전 과거 데이터를 실제 박스 계산 + Setup A/B 엔진 경로로
    재생한다 — 웹소켓 라이브 스트림이 붙기 전에 호출해, 시작 직후부터 이미
    진행 중이던 캐스케이드/박스 상태를 놓치지 않는다.

    gui/live_engine_bridge.py(GUI)와 run_bot.py(헤드리스) 둘 다 이 함수 하나를
    공유한다 — 예전엔 GUI 쪽에만 있어서 헤드리스 경로는 매번 완전 콜드
    스타트로 시작했다(일봉 90일 레인지 게이트가 처음 채워지기까지 최소
    45일, 5분봉 박스도 11시간 걸리는 문제 — run_bot.py가 크래시로 재시작될
    때마다 이 워밍업이 처음부터 다시 반복됐었다).

    runner.on_kline_msg/on_oi_poll(라이브 웹소켓 메시지가 쓰는 것과 동일한
    진입점)을 그대로 재사용해 박스/포지션/로그가 실제 라이브 흐름과 똑같이
    갱신되게 한다. stopping_event가 set되면 중간에 멈춘다.

    세 단계로 재생한다:
    1) 일봉(REPLAY_DAILY_DAYS개) -> 일봉 레인지 압축 게이트 시딩
    2) 5분봉(REPLAY_5M_BARS개) -> 5분봉 박스(120개 중 최근 12개 제외) 시딩.
       반드시 1)보다 나중에 재생해야 마지막 5분봉의 박스 계산이 완전히
       채워진 일봉 게이트를 보고 박스를 확정한다.
    3) 1분봉+OI(REPLAY_LOOKBACK_S) -> Setup A/B 캐스케이드 감시 상태 재생.
    """
    symbol_u = symbol.upper()
    now_ms = time.time() * 1000

    def _to_kline_msg(interval, k):
        return {"k": {
            "s": symbol_u, "i": interval, "x": True, "t": k[0],
            "o": k[1], "h": k[2], "l": k[3], "c": k[4], "v": k[5],
        }}

    try:
        raw_daily = bc.get_klines(symbol_u, interval="1d", limit=REPLAY_DAILY_DAYS)
        for k in raw_daily:
            if float(k[6]) > now_ms:
                continue  # 미확정(진행 중) 봉 제외
            runner.on_kline_msg(_to_kline_msg("1d", k))
    except Exception as e:
        print(f"[replay] 일봉 재생 실패({e}) — 일봉 레인지 게이트 없이 시작합니다.", file=sys.stderr)

    try:
        raw_5m = bc.get_klines(symbol_u, interval="5m", limit=REPLAY_5M_BARS)
        for k in raw_5m:
            if float(k[6]) > now_ms:
                continue
            runner.on_kline_msg(_to_kline_msg("5m", k))
    except Exception as e:
        print(f"[replay] 5분봉 재생 실패({e}) — 5분봉 박스 없이 시작합니다.", file=sys.stderr)

    start_ms = now_ms - REPLAY_LOOKBACK_S * 1000
    raw_klines = bc.get_klines(symbol_u, interval="1m", start_ms=int(start_ms), end_ms=int(now_ms), limit=1500)
    try:
        raw_oi = bc.get_open_interest_hist(symbol_u, period="5m",
                                            start_ms=int(start_ms), end_ms=int(now_ms), limit=500)
    except Exception as e:
        print(f"[replay] OI 이력 조회 실패({e}) — OI 없이 재생합니다.", file=sys.stderr)
        raw_oi = []

    events = []
    for k in raw_klines:
        if float(k[6]) > now_ms:
            continue  # 미확정(진행 중) 봉 제외
        events.append((k[0], 0, k))
    for o in raw_oi:
        events.append((float(o["timestamp"]), 1, o))
    events.sort(key=lambda e: (e[0], e[1]))

    if not events:
        return
    print(f"[replay] 최근 {REPLAY_LOOKBACK_S // 60}분 캔들/OI {len(events)}건 재생 중...", file=sys.stderr)
    for _, kind, payload in events:
        if stopping_event is not None and stopping_event.is_set():
            return
        if kind == 1:
            runner.on_oi_poll(float(payload["sumOpenInterest"]), float(payload["timestamp"]) / 1000.0)
        else:
            runner.on_kline_msg(_to_kline_msg("1m", payload))
    print(f"[replay] 완료 — Setup A/B가 최근 {REPLAY_LOOKBACK_S // 60}분의 박스/캐스케이드 상태를 반영합니다.",
          file=sys.stderr)


async def rest_kline_poll_loop(runner: "LiveShadowRunner", symbol: str, poll_s: float = REST_POLL_S):
    """웹소켓이 막힌 환경 폴백: poll_s(기본 1분)마다 확정봉을 REST로 받아 동일
    경로에 주입한다. gui/live_engine_bridge.py(GUI)와 run_bot.py(헤드리스)
    둘 다 공유 — 예전엔 GUI 쪽에만 있어서 헤드리스 경로엔 이 안전망이 없었다.

    - 각 타임프레임 최근 2봉을 조회해 '아직 처리 안 된 확정봉'만
      runner.on_kline_msg(웹소켓과 동일한 메시지 형태)로 흘린다.
    - 1m 중복 방지: runner.candles[-1].ts 이하는 스킵 (웹소켓이 정상이면
      이 폴링은 사실상 아무 것도 주입하지 않는다).
    """
    symbol_u = symbol.upper()
    loop = asyncio.get_running_loop()
    last_ts = {iv: None for iv in KLINE_INTERVALS}
    while True:
        await asyncio.sleep(poll_s)
        for iv in KLINE_INTERVALS:
            try:
                raw = await loop.run_in_executor(
                    None, lambda iv=iv: bc.get_klines(symbol_u, interval=iv, limit=2))
            except Exception as e:
                print(f"[rest_poll] {iv} error: {e}", file=sys.stderr)
                break  # 네트워크 문제면 이번 라운드 전체 스킵
            now_ms = time.time() * 1000
            for k in raw:
                if float(k[6]) > now_ms:
                    continue  # 미확정(진행 중) 봉 제외
                ts = k[0] / 1000.0
                if iv == "1m":
                    if runner.candles and ts <= runner.candles[-1].ts:
                        continue  # 웹소켓/이전 폴링이 이미 처리한 봉
                elif last_ts[iv] is not None and ts <= last_ts[iv]:
                    continue
                last_ts[iv] = ts
                runner.on_kline_msg({"k": {
                    "s": symbol_u, "i": iv, "x": True, "t": k[0],
                    "o": k[1], "h": k[2], "l": k[3], "c": k[4], "v": k[5],
                }})


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
