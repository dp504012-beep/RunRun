from __future__ import annotations

from pathlib import Path

import pytest

from src.backtest_config import BacktestConfig, BacktestResult, load_backtest_config
from src.exceptions import ConfigurationError


def test_valid_config_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-123")
    config = load_backtest_config(Path("config/backtest.example.yaml"))

    assert isinstance(config, BacktestConfig)
    assert config.market.symbol == "BTCUSDT"
    assert config.output.google_sheet_id == "sheet-123"
    assert config.strategies[0].id == "demo-1"


def test_invalid_date_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad-date.yaml"
    path.write_text(
        """
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-02-01T00:00:00Z"
  end: "2024-01-01T00:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0005
  quote_currency: USDT
strategies:
  - id: demo-1
    type: template
    initial_capital: 10000
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="market.start 必須早於 market.end。"):
        load_backtest_config(path)


def test_negative_capital_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad-capital.yaml"
    path.write_text(
        """
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-01-01T00:00:00Z"
  end: "2024-02-01T00:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0005
  quote_currency: USDT
strategies:
  - id: demo-1
    type: template
    initial_capital: 0
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="initial_capital 必須大於 0"):
        load_backtest_config(path)


def test_duplicate_strategy_ids_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate-strategy.yaml"
    path.write_text(
        """
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-01-01T00:00:00Z"
  end: "2024-02-01T00:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0005
  quote_currency: USDT
strategies:
  - id: demo-1
    type: template
    initial_capital: 10000
  - id: demo-1
    type: template
    initial_capital: 20000
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="策略 id 重複: demo-1"):
        load_backtest_config(path)


def test_missing_env_var_rejected(tmp_path: Path) -> None:
    path = tmp_path / "missing-env.yaml"
    path.write_text(
        """
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-01-01T00:00:00Z"
  end: "2024-02-01T00:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0005
  quote_currency: USDT
strategies:
  - id: demo-1
    type: template
    initial_capital: 10000
output:
  local_report: true
  google_sheet_id: ${GOOGLE_SHEET_ID}
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="缺少環境變數: GOOGLE_SHEET_ID"):
        load_backtest_config(path)


def test_unknown_strategy_type_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unknown-type.yaml"
    path.write_text(
        """
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-01-01T00:00:00Z"
  end: "2024-02-01T00:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0005
  quote_currency: USDT
strategies:
  - id: demo-1
    type: unknown
    initial_capital: 10000
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="未知策略類型: unknown"):
        load_backtest_config(path)


def test_result_model_can_be_constructed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-123")
    config = load_backtest_config(Path("config/backtest.example.yaml"))

    result = BacktestResult(
        config=config,
        started_at=config.market.start,
        finished_at=None,
        initial_capital=10000.0,
        final_equity=None,
        total_return=None,
        max_drawdown=None,
    )

    assert result.config.common.timezone == "UTC"
