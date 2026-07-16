#!/usr/bin/env python3
"""24시간 자동매매 봇 (헤드리스) — Binance 실시간 스트림 + 텔레그램.

⚠️ 실주문 경로는 현재 비활성화되어 있다 — 항상 페이퍼(가상 체결) 모드로
돌면서 신호/체결/현황을 텔레그램으로 알린다. config.json 의 live_trade 값은
무시된다 (추후 실거래 재활성화 시 사용).

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
from liquidation_strategy.live_feed import stream_loop, oi_poll_loop, snapshot_loop
from liquidation_strategy.live_trade import LiveTradingRunner
from liquidation_strategy.telegram_bot import TelegramBot, BotState, CommandHandler


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

    runner = LiveTradingRunner(
        symbol=cfg["symbol"], out_json=cfg["out_json"], log_dir=cfg["log_dir"],
        trader=None, trade_usdt=float(cfg["trade_usdt"]), notify=notify,
    )
    state.apply_param_overrides(runner.engine)
    runner.paused = bool(state.data.get("paused"))

    tasks = [
        stream_loop(runner, cfg["symbol"]),
        oi_poll_loop(runner, cfg["symbol"]),
        snapshot_loop(runner),
    ]
    if bot:
        handler = CommandHandler(runner, state)
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
