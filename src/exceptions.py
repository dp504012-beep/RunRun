from __future__ import annotations


class BacktesterError(Exception):
    """Base exception for the backtester."""


class ConfigurationError(BacktesterError):
    """Raised when configuration is invalid."""


class DataValidationError(BacktesterError):
    """Raised when input data fails validation."""


class MarketDataError(BacktesterError):
    """Raised when market data cannot be fetched or parsed."""
