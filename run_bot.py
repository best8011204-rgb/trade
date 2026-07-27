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

[Setup A 예외] run_all.py(로컬 GUI, 한국 리전 — forceOrder 지역 차단 추정)와
달리, 여기서는 Setup A가 setup_a_legacy.py의 forceOrder(청산 틱) 기반 원본
구현을 쓴다 — run_bot.py가 실제로 돌아가는 VPS에서는 forceOrder가 정상
수신될 수 있어서다. 캔들+거래량+OI만 쓰는 setup_a.py 버전은 run_all.py
전용이며, 두 경로의 Setup A 트리거 조건/파라미터가 서로 다르다는 뜻이다.

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

    # Setup A: run_all.py(로컬 GUI, 한국 리전 — forceOrder 지역 차단 추정)는
    # setup_a.py의 재설계된(캔들+거래량+OI, forceOrder 불필요) 버전을 그대로
    # 쓰지만, run_bot.py는 실제로 forceOrder가 정상 수신될 가능성이 있는
    # VPS에서 돌아가므로 원래(재설계 전) forceOrder 기반 구현을 되살려 쓴다 —
    # a_impl로 주입하면 engine.py가 이 구현을 그대로 Setup A 자리에 꽂는다.
    a_impl = LegacyCascadeExhaustionLong(LegacyCascadeAParams())

    runner = LiveTradingRunner(
        symbol=cfg["symbol"], out_json=cfg["out_json"], log_dir=cfg["log_dir"],
        trader=None, trade_usdt=float(cfg["trade_usdt"]), notify=notify,
        a_impl=a_impl,
    )
    state.apply_param_overrides(runner.engine)
    runner.paused = bool(state.data.get("paused"))

    # Setup C: StrategyEngine과 독립 — run_all.py(GUI)와 동일하게 실제 5분봉
    # 확정봉+OI를 그대로 받아 구동한다. 실주문 없음(A/B와 동일 원칙).
    # (예전엔 헤드리스 경로에 아예 없어서 /status에 Setup C가 안 나왔었다.)
    c_runner = LiveSetupCRunner(ParamsC())

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
