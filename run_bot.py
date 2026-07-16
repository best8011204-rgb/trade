#!/usr/bin/env python3
"""24시간 자동매매 봇 엔트리포인트 — Binance 실시간 스트림 + 실주문 + 텔레그램.

로컬 컴퓨터/노트북(인터넷 열린 환경)에서 실행:

    pip install -r requirements.txt
    cp config.example.json config.json   # 값 채우기
    python3 run_bot.py                   # 또는 python3 run_bot.py --config config.json

config.json 의 live_trade 가 false 면 주문 없이 신호·가상체결만 텔레그램으로
알리는 페이퍼 모드로 돈다. 실계좌에 붙이기 전 testnet=true 로 먼저 검증할 것.
"""

import argparse
import asyncio
import json
import os
import sys

from liquidation_strategy.live_feed import stream_loop, oi_poll_loop, snapshot_loop
from liquidation_strategy.live_trade import LiveTradingRunner
from liquidation_strategy.binance_trader import BinanceFuturesTrader
from liquidation_strategy.telegram_bot import TelegramBot, BotState, CommandHandler

DEFAULT_CONFIG = {
    "symbol": "btcusdt",
    "live_trade": False,          # true 면 실제 주문 실행
    "testnet": True,              # 실계좌 전 테스트넷으로 검증
    "binance_api_key": "",
    "binance_api_secret": "",
    "leverage": 3,
    "trade_usdt": 100.0,          # 포지션 1개당 명목가 (USDT)
    "telegram_bot_token": "",
    "telegram_chat_id": None,     # 비우면 최초 /start 사용자를 자동 바인딩
    "out_json": "liquidation_strategy_output_live.json",
    "log_dir": "logs",
    "state_file": "bot_state.json",
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    else:
        print(f"[config] {path} 없음 — 기본값 + 환경변수만 사용", file=sys.stderr)
    # 환경변수가 있으면 우선 (키를 파일에 남기고 싶지 않을 때)
    env_map = {
        "BINANCE_API_KEY": "binance_api_key",
        "BINANCE_API_SECRET": "binance_api_secret",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


async def main_async(cfg):
    state = BotState(cfg["state_file"])
    if cfg.get("telegram_chat_id"):
        state.data["chat_id"] = int(cfg["telegram_chat_id"])

    trader = None
    if cfg["live_trade"]:
        if not cfg["binance_api_key"] or not cfg["binance_api_secret"]:
            print("[config] live_trade=true 인데 API 키가 없음 — 페이퍼 모드로 강등", file=sys.stderr)
        else:
            trader = BinanceFuturesTrader(
                cfg["binance_api_key"], cfg["binance_api_secret"],
                symbol=cfg["symbol"], testnet=cfg["testnet"], leverage=int(cfg["leverage"]),
            )
            trader.prepare()
            bal = trader.get_usdt_balance()
            print(f"[trader] 준비 완료 ({'테스트넷' if cfg['testnet'] else '메인넷'}) "
                  f"가용잔고 {bal:,.2f} USDT", file=sys.stderr)

    bot = None
    notify = None
    if cfg["telegram_bot_token"]:
        bot = TelegramBot(cfg["telegram_bot_token"], state)
        notify = bot.send
    else:
        print("[telegram] 토큰 없음 — 텔레그램 기능 비활성", file=sys.stderr)

    runner = LiveTradingRunner(
        symbol=cfg["symbol"], out_json=cfg["out_json"], log_dir=cfg["log_dir"],
        trader=trader, trade_usdt=float(cfg["trade_usdt"]), notify=notify,
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
        mode = "실거래" if trader else "페이퍼"
        net = " · 테스트넷" if (trader and cfg["testnet"]) else ""
        bot.send(f"🤖 봇 시작 — {cfg['symbol'].upper()} · {mode}{net}\n"
                 f"포지션당 {cfg['trade_usdt']} USDT · 레버리지 x{cfg['leverage']}\n"
                 "/help 로 명령 확인")

    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(description="Binance 24h 자동매매 봇 (+텔레그램)")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    cfg = load_config(args.config)
    try:
        asyncio.run(main_async(cfg))
    except KeyboardInterrupt:
        print("\n종료.", file=sys.stderr)


if __name__ == "__main__":
    main()
