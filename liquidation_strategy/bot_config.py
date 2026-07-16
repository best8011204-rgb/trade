"""config.json 로더 — run_bot.py(헤드리스)와 gui/run_all(GUI)이 공유한다.

환경변수(BINANCE_API_KEY 등)가 있으면 파일 값보다 우선한다.
"""

import json
import os
import sys

DEFAULT_CONFIG = {
    "symbol": "btcusdt",
    "live_trade": False,          # 현재 실주문 경로는 비활성화됨 (항상 페이퍼)
    "testnet": True,
    "binance_api_key": "",
    "binance_api_secret": "",
    "leverage": 3,
    "trade_usdt": 100.0,          # 포지션 1개당 명목가 (USDT) — 페이퍼 표기용
    "telegram_bot_token": "",
    "telegram_chat_id": None,     # 비우면 최초 /start 사용자를 자동 바인딩
    "out_json": "liquidation_strategy_output_live.json",
    "log_dir": "logs",
    "state_file": "bot_state.json",
}

ENV_MAP = {
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}


def load_config(path="config.json", quiet=False):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    elif not quiet:
        print(f"[config] {path} 없음 — 기본값 + 환경변수만 사용", file=sys.stderr)
    for env, key in ENV_MAP.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg
