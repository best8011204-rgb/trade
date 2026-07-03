from __future__ import annotations

import asyncio

from TradingBot.exchange.account_data import AccountData
from TradingBot.execution.models import AccountState


class AccountManager:
    """Maintains synchronized account state."""

    def __init__(self, account_data: AccountData) -> None:
        self._account_data = account_data
        self._state: AccountState | None = None
        self._lock = asyncio.Lock()

    async def sync(self) -> AccountState:
        raw = await self._account_data.account()
        state = AccountState(
            wallet_balance=float(raw.get("totalWalletBalance", 0)),
            available_balance=float(raw.get("availableBalance", 0)),
            margin_balance=float(raw.get("totalMarginBalance", 0)),
            unrealized_pnl=float(raw.get("totalUnrealizedProfit", 0)),
            realized_pnl=0.0,
            leverage=0,
            margin_mode=str(raw.get("multiAssetsMargin", "single")),
        )
        async with self._lock:
            self._state = state
        return state

    async def get_state(self) -> AccountState | None:
        async with self._lock:
            return self._state

