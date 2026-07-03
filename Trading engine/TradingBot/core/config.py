from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    api_key: str
    api_secret: str
    testnet: bool
    symbols: tuple[str, ...]
    leverage: int
    max_position_size: float
    max_leverage: int
    database_path: Path
    log_dir: Path
    log_level: str
    request_timeout_seconds: float = 10.0
    rest_retries: int = 3

    @classmethod
    def from_env(cls) -> Settings:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in os.getenv("TRADING_SYMBOLS", "BTCUSDT").split(",")
            if symbol.strip()
        )
        return cls(
            api_key=os.getenv("BINANCE_API_KEY", ""),
            api_secret=os.getenv("BINANCE_API_SECRET", ""),
            testnet=_bool_env("BINANCE_TESTNET", True),
            symbols=symbols,
            leverage=int(os.getenv("TRADING_LEVERAGE", "1")),
            max_position_size=float(os.getenv("TRADING_MAX_POSITION_SIZE", "0.01")),
            max_leverage=int(os.getenv("TRADING_MAX_LEVERAGE", "3")),
            database_path=Path(os.getenv("TRADING_DATABASE_PATH", "trading_engine.db")),
            log_dir=Path(os.getenv("TRADING_LOG_DIR", "logs")),
            log_level=os.getenv("TRADING_LOG_LEVEL", "INFO").upper(),
        )

