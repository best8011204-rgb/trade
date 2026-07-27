#!/usr/bin/env python3
"""24시간 자동매매 봇 (헤드리스) — Binance 실시간 스트림 + 텔레그램.

⚠️ 실주문 경로는 현재 비활성화되어 있다 — 항상 페이퍼(가상 체결) 모드로
돌면서 신호/체결/현황을 텔레그램으로 알린다. config.json 의 live_trade 값은
무시된다 (추후 실거래 재활성화 시 사용).

Setup A/B/C 모두 구동하고(run_all.py의 GUI 경로와 기능 동등), 시작 시
REST로 과거 일봉/5분봉/1분봉/OI를 재생해 계층형 박스(일봉 레짐 게이트+
5분봉 박스)와 캐스케이드 감시 상태를 미리 채운 뒤 웹소켓 스트림에 붙는다.
웹소켓이 확정봉을 놓쳐도 1분마다 REST로 보정하는 폴백도 GUI 경로와 동일하게
동작한다.

[Setup A 예외] run_all.py와 달리 여기서는 Setup A가 setup_a_legacy.py의
forceOrder(청산 틱) 기반 원본 구현을 쓴다. (당초 "지역 차단" 때문에 forceOrder가
전혀 안 온다고 알고 있었으나, 실제 원인은 Binance가 2026-04-23부로 레거시
wss://fstream.binance.com/stream 을 public/market/private로 분리하면서
forceOrder/aggTrade/kline["market" 카테고리 전부]가 레거시 URL에서 조용히
끊긴 것이었다 — live_feed.py의 STREAM_URL을 /market/stream으로 이전해 해결.
이 fix는 run_all.py도 함께 쓰는 공용 코드라 지금은 두 경로 모두 forceOrder를
받을 수 있다. Setup A 구현이 여전히 갈라져 있는 건 순전히 "run_bot.py엔
청산틱 기반, run_all.py엔 캔들+OI 기반을 쓰겠다"는 선택 때문이며, 데이터
가용성 문제가 아니다.)

GUI까지 같이 보려면 이 파일 대신 `python3 run_all.py` 를 실행하면 된다 —
run_all 이 GUI + 전략 엔진 + 텔레그램 봇을 한 프로세스에서 모두 구동한다.
run_bot.py 는 화면 없는 서버/장시간 무인 운영용 대안이다.

주의: run_all 과 run_bot 을 동시에 실행하지 말 것 — 같은 텔레그램 토큰으로
두 프로세스가 getUpdates 를 폴링하면 충돌(409)한다.

    pip install -r requirements.txt
    cp config.example.json config.json   # 텔레그램 토큰 채우기
    python3 run_bot.py
"""

import argparse
import asyncio
import sys

from liquidation_strategy.bot_config import load_config
from liquidation_strategy.live_feed import (
    stream_loop, oi_poll_loop, snapshot_loop, replay_recent_history, rest_kline_poll_loop,
)
from liquidation_strategy.live_trade import LiveTradingRunner
from liquidation_strategy.telegram_bot import TelegramBot, BotState, CommandHandler
from liquidation_strategy.setup_c import ParamsC
from liquidation_strategy.live_setup_c import LiveSetupCRunner
from liquidation_strategy.setup_a_legacy import LegacyCascadeAParams, LegacyCascadeExhaustionLong


async def main_async(cfg):
    state = BotState(cfg["state_file"])
    if cfg.get("telegram_chat_id"):
        state.data["chat_id"] = int(cfg["telegram_chat_id"])

    # 실주문 비활성화: live_trade 설정과 무관하게 트레이더를 만들지 않는다.
    if cfg.get("live_trade"):
        print("[config] live_trade=true 이지만 실주문 경로는 비활성화 상태 — "
              "페이퍼 모드로 실행합니다.", file=sys.stderr)

    bot = None
    notify = None
    if cfg["telegram_bot_token"]:
        bot = TelegramBot(cfg["telegram_bot_token"], state)
        notify = bot.send
    else:
        print("[telegram] 토큰 없음 — 텔레그램 기능 비활성", file=sys.stderr)

    # Setup A: run_bot.py는 setup_a_legacy.py의 forceOrder(청산 틱) 기반 원본
    # 구현을 쓴다(run_all.py는 여전히 setup_a.py의 캔들+거래량+OI 버전).
    # a_impl로 주입하면 engine.py가 이 구현을 그대로 Setup A 자리에 꽂는다.
    # forceOrder 자체는 live_feed.py의 STREAM_URL을 /market 엔드포인트로
    # 이전한 뒤로 두 경로 모두 정상 수신된다(예전엔 레거시 URL 마이그레이션
    # 문제로 지역 무관하게 안 들어왔었다).
    a_impl = LegacyCascadeExhaustionLong(LegacyCascadeAParams())

    runner = LiveTradingRunner(
        symbol=cfg["symbol"], out_json=cfg["out_json"], log_dir=cfg["log_dir"],
        trader=None, trade_usdt=float(cfg["trade_usdt"]), notify=notify,
        a_impl=a_impl,
    )

    # Setup C: StrategyEngine과 독립 — run_all.py(GUI)와 동일하게 실제 5분봉
    # 확정봉+OI를 그대로 받아 구동한다. 실주문 없음(A/B와 동일 원칙).
    # (예전엔 헤드리스 경로에 아예 없어서 /status에 Setup C가 안 나왔었다.)
    # apply_param_overrides()가 Setup C의 저장된 /set 값도 복원해야 하므로
    # c_runner는 반드시 그 호출보다 먼저 만든다.
    c_runner = LiveSetupCRunner(ParamsC())

    state.apply_param_overrides(runner.engine, c_runner)
    runner.paused = bool(state.data.get("paused"))

    def on_display_candle(interval, candle):
        if interval == "5m" and candle.get("closed"):
            c_runner.on_confirmed_5m_candle(candle)
    runner.on_display_candle = on_display_candle

    orig_on_oi_poll = runner.on_oi_poll

    def on_oi_poll(oi_value, ts):
        orig_on_oi_poll(oi_value, ts)
        c_runner.on_oi(oi_value, ts)
    runner.on_oi_poll = on_oi_poll

    # run_all.py(GUI)와 동일하게, 라이브 스트림을 붙이기 전에 REST로 과거
    # 일봉/5분봉/1분봉/OI를 재생해 계층형 박스(일봉 레짐 게이트+5분봉 박스)와
    # Setup A/B 캐스케이드 감시 상태를 미리 채운다 — 이게 없으면 재시작할
    # 때마다(PM2 크래시 재시작 포함) 완전 콜드 스타트로 시작해 일봉 게이트가
    # 최소 45일치 쌓이기 전까진 박스가 아예 형성되지 않는다.
    try:
        replay_recent_history(runner, cfg["symbol"])
    except Exception as e:
        print(f"[replay] 실패({e}) — 재생 없이 라이브 스트림부터 시작합니다.", file=sys.stderr)

    tasks = [
        stream_loop(runner, cfg["symbol"]),
        oi_poll_loop(runner, cfg["symbol"]),
        snapshot_loop(runner),
        rest_kline_poll_loop(runner, cfg["symbol"]),
    ]
    if bot:
        handler = CommandHandler(runner, state, c_runner=c_runner)
        tasks.append(bot.poll_loop(handler.handle))
        bot.send(f"🤖 봇 시작 — {cfg['symbol'].upper()} · 페이퍼(실주문 비활성)\n"
                 "/help 로 명령 확인")

    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(description="Binance 24h 페이퍼 트레이딩 봇 (+텔레그램, 실주문 비활성)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = load_config(args.config)
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        print("\n종료.", file=sys.stderr)


if __name__ == "__main__":
    main()
