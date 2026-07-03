class TradingEngineError(Exception):
    """Base engine exception."""


class ExchangeError(TradingEngineError):
    """Raised when the exchange rejects or fails a request."""


class RiskRejectedError(TradingEngineError):
    """Raised when risk validation rejects a signal or order."""


class ConfigurationError(TradingEngineError):
    """Raised when configuration is invalid."""

