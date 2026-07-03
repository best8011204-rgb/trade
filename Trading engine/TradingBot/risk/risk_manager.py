from __future__ import annotations

from TradingBot.exchange.models import OrderRequest, SymbolRules
from TradingBot.execution.order_manager import OrderManager
from TradingBot.utils.exceptions import RiskRejectedError


class RiskManager:
    """Composable risk framework for pre-trade validation."""

    def __init__(
        self,
        max_position_size: float,
        max_leverage: int,
        leverage: int,
        order_manager: OrderManager,
    ) -> None:
        self._max_position_size = max_position_size
        self._max_leverage = max_leverage
        self._leverage = leverage
        self._order_manager = order_manager

    async def validate(self, request: OrderRequest, rules: SymbolRules) -> None:
        if request.quantity <= 0:
            raise RiskRejectedError("Quantity must be greater than zero.")
        if request.quantity < rules.min_quantity:
            raise RiskRejectedError(f"Quantity is below exchange minimum: {rules.min_quantity}.")
        if request.quantity > self._max_position_size:
            raise RiskRejectedError("Quantity exceeds configured maximum position size.")
        if self._leverage > self._max_leverage:
            raise RiskRejectedError("Configured leverage exceeds maximum allowed leverage.")
        if await self._order_manager.has_open_duplicate(request):
            raise RiskRejectedError("Duplicate open order rejected.")

