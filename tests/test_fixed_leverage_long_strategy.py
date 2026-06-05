from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.backtest_config import BacktestConfig, CommonConfig, MarketConfig, OutputConfig, StrategyConfig, BacktestResult
from src.data.binance_client import Kline
from src.strategies.fixed_leverage_long import (
    LIQUIDATION_MODEL_VERSION,
    STRATEGY_TYPE,
    run_fixed_leverage_long,
)


def _config(*, fee_rate: float = 0.001, leverage: float = 2.0) -> tuple[BacktestConfig, StrategyConfig]:
    strategy = StrategyConfig(
        id="btc-2x-long",
        type=STRATEGY_TYPE,
        initial_capital=500.0,
        parameters={"leverage": leverage, "liquidation_model": "simplified"},
    )
    config = BacktestConfig(
        market=MarketConfig(
            source="binance",
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 4, 0, tzinfo=UTC),
        ),
        common=CommonConfig(timezone="UTC", fee_rate=fee_rate, slippage_rate=0.0, quote_currency="USDT"),
        strategies=[strategy],
        output=OutputConfig(
            local_report=True,
            google_sheet_id=None,
            equity_curve_sampling=1,
            write_trade_log=True,
            create_charts=False,
        ),
    )
    return config, strategy


def _kline(hour: int, *, open_price: str = "100", high: str = "100", low: str = "100", close: str = "100") -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=datetime(2024, 1, 1, hour, 0, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def test_2x_long_up_case_matches_manual_values() -> None:
    config, strategy = _config(fee_rate=0.001)
    result = run_fixed_leverage_long(
        config=config,
        strategy=strategy,
        klines=[
            _kline(0, open_price="100", high="100", low="100", close="100"),
            _kline(1, open_price="100", high="111", low="99", close="110"),
            _kline(2, open_price="110", high="121", low="109", close="120"),
        ],
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    assert isinstance(result, BacktestResult)
    assert result.metrics["notional_position"] == pytest.approx(1000.0)
    assert result.metrics["entry_price"] == pytest.approx(100.0)
    assert result.trades[0].quantity == pytest.approx(10.0)
    assert result.trades[0].fee == pytest.approx(1.0)
    assert result.metrics["liquidation_price"] == pytest.approx(50.1)
    assert [point.equity for point in result.equity_curve] == pytest.approx([499.0, 599.0, 699.0])
    assert result.final_equity == pytest.approx(699.0)
    assert result.total_return == pytest.approx((699.0 - 500.0) / 500.0)
    assert result.metrics["liquidated"] is False
    assert result.metrics["liquidation_model_version"] == LIQUIDATION_MODEL_VERSION


def test_2x_long_down_without_liquidation_matches_manual_values() -> None:
    config, strategy = _config(fee_rate=0.001)
    result = run_fixed_leverage_long(
        config=config,
        strategy=strategy,
        klines=[
            _kline(0, open_price="100", high="100", low="100", close="100"),
            _kline(1, open_price="100", high="100", low="80", close="80"),
            _kline(2, open_price="80", high="81", low="60", close="60"),
        ],
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    assert [point.equity for point in result.equity_curve] == pytest.approx([499.0, 299.0, 99.0])
    assert result.final_equity == pytest.approx(99.0)
    assert result.max_drawdown == pytest.approx((99.0 / 500.0) - 1.0)
    assert result.metrics["minimum_equity"] == pytest.approx(99.0)
    assert result.metrics["liquidated"] is False


def test_intrabar_liquidation_is_detected_and_stops_strategy() -> None:
    config, strategy = _config(fee_rate=0.001)
    result = run_fixed_leverage_long(
        config=config,
        strategy=strategy,
        klines=[
            _kline(0, open_price="100", high="100", low="100", close="100"),
            _kline(1, open_price="100", high="105", low="50.1", close="90"),
            _kline(2, open_price="90", high="130", low="90", close="130"),
        ],
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    assert result.metrics["liquidated"] is True
    assert result.metrics["liquidation_time"] == "2024-01-01T01:00:00Z"
    assert result.final_equity == 0.0
    assert result.total_return == -1.0
    assert [point.equity for point in result.equity_curve] == pytest.approx([499.0, 0.0])
    assert len(result.risk_events) == 1
    assert result.risk_events[0].details["liquidation_model_version"] == LIQUIDATION_MODEL_VERSION


def test_same_input_is_deterministic() -> None:
    config, strategy = _config(fee_rate=0.001)
    klines = [
        _kline(0, open_price="100", high="100", low="100", close="100"),
        _kline(1, open_price="100", high="111", low="99", close="110"),
    ]
    finished_at = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)

    first = run_fixed_leverage_long(config=config, strategy=strategy, klines=klines, finished_at=finished_at)
    second = run_fixed_leverage_long(config=config, strategy=strategy, klines=klines, finished_at=finished_at)

    assert first == second
