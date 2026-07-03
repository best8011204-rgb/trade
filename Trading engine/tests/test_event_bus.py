from __future__ import annotations

from TradingBot.core.event_bus import EventBus
from TradingBot.core.events import Event, EventType


async def test_event_bus_publishes_to_subscribers() -> None:
    bus = EventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    await bus.subscribe(EventType.PRICE_UPDATED, handler)
    event = Event(EventType.PRICE_UPDATED, {"symbol": "BTCUSDT"})

    await bus.publish(event)

    assert received == [event]

