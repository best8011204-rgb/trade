# Architecture

The engine is composed around a central async event bus. Strategies emit `Signal` objects, and the execution engine handles validation, risk checks, order submission, and state synchronization.

## Boundaries

- `strategy`: Strategy contracts only. No Binance imports.
- `exchange`: Binance REST/WebSocket adapters and normalized market/account data.
- `execution`: Order, account, position, and execution orchestration.
- `risk`: Pre-trade validation rules.
- `database`: SQLite schema and repositories.
- `core`: Application composition, configuration, logging, scheduling, and events.

## Extension Points

Future paper trading, multiple exchanges, backtesting, replay, dashboards, notifications, and plugin strategies should be added by implementing new adapters around the existing contracts. Engine modules should keep communicating through events instead of importing each other directly.

