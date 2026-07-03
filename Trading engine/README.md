# Binance Trading Engine

Production-oriented, modular, event-driven Binance trading engine in Python.

This project intentionally does **not** implement a trading strategy. Strategies are external plugins that emit standardized signals. The engine validates those signals, applies risk checks, converts them to exchange orders, tracks account and position state, and persists operational data.

## Highlights

- Event-driven module communication
- Strategy layer separated from Binance
- Binance REST and WebSocket exchange layer
- Order, position, account, execution, and risk managers
- SQLite persistence through repositories
- Structured rotating logs
- Environment-based configuration
- Fully typed Python 3.12+ code
- Pytest coverage for core engine behavior

## Project Layout

```text
TradingBot/
  core/
  database/
  exchange/
  execution/
  risk/
  strategy/
  utils/
  tests/
main.py
```

## Quick Start

```powershell
cd "Trading engine"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python main.py
```

The default configuration uses Binance testnet mode. Put API credentials in `.env`; never hardcode secrets.

## Configuration

Supported environment variables:

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `BINANCE_TESTNET`
- `TRADING_SYMBOLS`
- `TRADING_LEVERAGE`
- `TRADING_MAX_POSITION_SIZE`
- `TRADING_MAX_LEVERAGE`
- `TRADING_DATABASE_PATH`
- `TRADING_LOG_DIR`
- `TRADING_LOG_LEVEL`

## Architecture

Strategies depend only on `TradingBot.strategy.strategy_base` and emit `Signal` objects. They do not import Binance clients.

The `ExecutionEngine` receives `StrategySignal` events and coordinates:

1. Signal validation
2. Account and position checks
3. Risk validation
4. Order creation
5. Exchange submission
6. State persistence
7. Event publication

All module communication flows through the event bus. This keeps the engine extensible for future exchanges, paper trading, backtesting, replay, dashboards, notifications, and plugin strategies.

## Safety

This is infrastructure for automated trading, not financial advice. Start on testnet, use small quantities, and add strategy-specific risk controls before live trading.

