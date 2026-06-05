from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest_config import BacktestConfig, CommonConfig, MarketConfig, OutputConfig, StrategyConfig, BacktestResult
from src.data.binance_client import Kline
from src.metrics.performance import compute_performance_summary
from src.strategies.buy_and_hold import STRATEGY_TYPE, run_btc_buy_and_hold


def _config(*, fee_rate: float = 0.001, include_exit_fee: bool = False) -> tuple[BacktestConfig, StrategyConfig]:
    strategy = StrategyConfig(
        id="buy-hold-btc",
        type=STRATEGY_TYPE,
        initial_capital=500.0,
        parameters={"include_exit_fee": include_exit_fee},
    )
    config = BacktestConfig(
        market=MarketConfig(
            source="binance",
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
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


def _kline(hour: int, *, open_price: str = "100", close_price: str = "100") -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=datetime(2024, 1, 1, hour, 0, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(max(open_price, close_price)),
        low=Decimal(min(open_price, close_price)),
        close=Decimal(close_price),
        volume=Decimal("1"),
    )


def test_500_usdt_manual_case_matches_expected_values() -> None:
    config, strategy = _config(fee_rate=0.001, include_exit_fee=True)
    klines = [
        _kline(0, open_price="100", close_price="100"),
        _kline(1, open_price="100", close_price="110"),
        _kline(2, open_price="110", close_price="120"),
    ]

    result = run_btc_buy_and_hold(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    expected_buy_fee = 0.5
    expected_btc_quantity = 499.5 / 100
    expected_equity = [499.5, 549.45, 599.4]
    expected_exit_fee = 599.4 * 0.001

    assert isinstance(result, BacktestResult)
    assert result.metrics["btc_quantity"] == pytest.approx(expected_btc_quantity)
    assert result.trades[0].quantity == pytest.approx(expected_btc_quantity)
    assert result.trades[0].fee == pytest.approx(expected_buy_fee)
    assert result.metrics["buy_fee"] == pytest.approx(expected_buy_fee)
    assert result.metrics["total_fees"] == pytest.approx(expected_buy_fee)
    assert [point.equity for point in result.equity_curve] == pytest.approx(expected_equity)
    assert result.final_equity == pytest.approx(599.4)
    assert result.total_return == pytest.approx((599.4 - 500) / 500)
    assert result.metrics["estimated_exit_fee"] == pytest.approx(expected_exit_fee)
    assert result.metrics["estimated_exit_net_equity"] == pytest.approx(599.4 - expected_exit_fee)
    assert "不預設賣出" in result.metrics["assumptions"][-1]


def test_flat_price_only_loses_buy_fee() -> None:
    config, strategy = _config(fee_rate=0.001)
    result = run_btc_buy_and_hold(
        config=config,
        strategy=strategy,
        klines=[
            _kline(0, open_price="100", close_price="100"),
            _kline(1, open_price="100", close_price="100"),
        ],
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    assert result.final_equity == pytest.approx(499.5)
    assert result.total_return == pytest.approx(-0.001)
    assert result.trades[0].fee == pytest.approx(0.5)


def test_same_input_is_deterministic() -> None:
    config, strategy = _config(fee_rate=0.001, include_exit_fee=True)
    klines = [
        _kline(0, open_price="100", close_price="100"),
        _kline(1, open_price="100", close_price="120"),
    ]
    finished_at = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)

    first = run_btc_buy_and_hold(config=config, strategy=strategy, klines=klines, finished_at=finished_at)
    second = run_btc_buy_and_hold(config=config, strategy=strategy, klines=klines, finished_at=finished_at)

    assert first == second


def test_short_period_annualization_overflow_does_not_break_result() -> None:
    start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    summary = compute_performance_summary(
        timestamps=[start, start + timedelta(hours=1)],
        equity_curve=[100.0, 1_000_000.0],
        initial_capital=100.0,
    )

    assert summary.final_equity == 1_000_000.0
    assert summary.annualized_return == float("inf")
