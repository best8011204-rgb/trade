from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class Scheduler:
    """Tracks long-running background tasks and cancels them cleanly."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def create_task(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def every(self, seconds: float, job: Callable[[], Awaitable[None]]) -> None:
        while True:
            await job()
            await asyncio.sleep(seconds)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

