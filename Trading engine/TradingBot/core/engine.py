from __future__ import annotations

import logging

from TradingBot.core.config import Settings
from TradingBot.core.event_bus import EventBus
from TradingBot.core.logger import configure_logging
from TradingBot.core.scheduler import Scheduler
from TradingBot.database.sqlite import SQLiteDatabase
from TradingBot.exchange.account_data import AccountData
from TradingBot.exchange.binance_client import BinanceClient
from TradingBot.exchange.rest import BinanceRestClient
from TradingBot.execution.account_manager import AccountManager
from TradingBot.execution.execution_engine import ExecutionEngine
from TradingBot.execution.order_manager import OrderManager
from TradingBot.execution.position_manager import PositionManager
from TradingBot.risk.risk_manager import RiskManager

LOGGER = logging.getLogger(__name__)


class TradingEngine:
    """Application composition root for the trading engine."""

    def __init__(
        self,
        event_bus: EventBus,
        scheduler: Scheduler,
        database: SQLiteDatabase,
        exchange: BinanceClient,
        execution_engine: ExecutionEngine,
    ) -> None:
        self.event_bus = event_bus
        self._scheduler = scheduler
        self._database = database
        self._exchange = exchange
        self._execution_engine = execution_engine

    @classmethod
    def from_settings(cls, settings: Settings) -> TradingEngine:
        configure_logging(settings.log_dir, settings.log_level)
        database = SQLiteDatabase(settings.database_path)
        rest = BinanceRestClient(
            api_key=settings.api_key,
            api_secret=settings.api_secret,
            testnet=settings.testnet,
            timeout_seconds=settings.request_timeout_seconds,
            retries=settings.rest_retries,
        )
        exchange = BinanceClient(rest)
        account_data = AccountData(rest)
        event_bus = EventBus()
        scheduler = Scheduler()
        account_manager = AccountManager(account_data)
        position_manager = PositionManager(account_data)
        order_manager = OrderManager(exchange)
        risk_manager = RiskManager(
            max_position_size=settings.max_position_size,
            max_leverage=settings.max_leverage,
            leverage=settings.leverage,
            order_manager=order_manager,
        )
        execution_engine = ExecutionEngine(
            event_bus=event_bus,
            exchange=exchange,
            account_manager=account_manager,
            position_manager=position_manager,
            order_manager=order_manager,
            risk_manager=risk_manager,
        )
        return cls(event_bus, scheduler, database, exchange, execution_engine)

    async def start(self) -> None:
        self._database.initialize()
        await self._exchange.sync_time()
        await self._execution_engine.start()
        LOGGER.info("trading_engine_started")

    async def stop(self) -> None:
        await self._execution_engine.stop()
        await self._scheduler.stop()
        await self._exchange.close()
        LOGGER.info("trading_engine_stopped")

