from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from TradingBot.core.events import Event, EventType

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus:
    """Thread-safe async event bus for decoupled module communication."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        async with self._lock:
            self._handlers[event_type].append(handler)

    async def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        async with self._lock:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        async with self._lock:
            handlers = tuple(self._handlers.get(event.type, ()))
        if handlers:
            await asyncio.gather(*(handler(event) for handler in handlers))

