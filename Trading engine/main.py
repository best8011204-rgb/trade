from __future__ import annotations

import asyncio

from TradingBot.core.config import Settings
from TradingBot.core.engine import TradingEngine


async def main() -> None:
    settings = Settings.from_env()
    engine = TradingEngine.from_settings(settings)
    await engine.start()
    try:
        await asyncio.Event().wait()
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())

