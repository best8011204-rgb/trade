from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import websockets

LOGGER = logging.getLogger(__name__)


class BinanceWebSocket:
    """Auto-reconnecting Binance WebSocket stream wrapper."""

    def __init__(self, stream_url: str) -> None:
        self._stream_url = stream_url
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def messages(self) -> AsyncIterator[dict[str, object]]:
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                async with websockets.connect(self._stream_url, ping_interval=20) as ws:
                    backoff = 1.0
                    async for message in ws:
                        yield json.loads(message)
            except Exception:
                LOGGER.exception("websocket_disconnected")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

