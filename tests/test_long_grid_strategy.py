from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.backtest_config import BacktestConfig, CommonConfig, MarketConfig, OutputConfig, StrategyConfig
from src.data.binance_client import Kline
from src.strategies.long_grid import run_long_grid


def _config(*, fee_rate: float = 0.001) -> BacktestConfig:
    return BacktestConfig(
        market=MarketConfig(
            source="binance",
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 2, tzinfo=UTC),
        ),
        common=CommonConfig(timezone="UTC", fee_rate=fee_rate, slippage_rate=0.0, quote_currency="USDT"),
        strategies=[],
        output=OutputConfig(
            local_report=True,
            google_sheet_id=None,
            equity_curve_sampling=1,
            write_trade_log=True,
            create_charts=False,
        ),
    )


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        id="grid-10x",
        type="long_grid",
        initial_capital=500.0,
        parameters={
            "leverage": 10,
            "lower_bound": 60000,
            "upper_bound": 120000,
            "grid_count": 130,
            "liquidation_model": "simplified",
        },
    )


def _kline(hour: int, *, open_price: str, high: str, low: str, close: str) -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=datetime(2024, 1, 1, hour, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def test_case_a_one_buy_sell_cycle_matches_manual_accounting() -> None:
    result = run_long_grid(
        config=_config(),
        strategy=_strategy(),
        klines=[
            _kline(
                0,
                open_price="60000",
                high="60461.53846153846153846153846",
                low="60000",
                close="60461.53846153846153846153846",
            ),
        ],
    )

    assert [trade.side for trade in result.trades] == ["BUY", "SELL"]
    assert result.metrics["buy_count"] == 1
    assert result.metrics["sell_count"] == 1
    assert result.metrics["final_available_margin"] == pytest.approx(500.21863905)
    assert result.metrics["final_margin_locked"] == pytest.approx(0)
    assert result.metrics["realized_grid_profit"] == pytest.approx(0.21863905)
    assert result.equity_curve[-1].equity == pytest.approx(500.21863905)


def test_downside_open_lot_tracks_unrealized_pnl_without_liquidation() -> None:
    result = run_long_grid(
        config=_config(),
        strategy=_strategy(),
        klines=[
            _kline(0, open_price="60000", high="60050", low="59500", close="59500"),
            _kline(1, open_price="59500", high="59600", low="59400", close="59400"),
        ],
    )

    assert result.metrics["liquidated"] is False
    assert result.metrics["buy_count"] == 1
    assert result.metrics["sell_count"] == 0
    assert result.metrics["position_quantity"] == pytest.approx(0.00064102564)
    assert result.metrics["final_margin_locked"] == pytest.approx(3.84615385)
    assert result.equity_curve[-1].equity == pytest.approx(499.57692308)


def test_liquidation_stops_later_candles_and_sets_equity_zero() -> None:
    result = run_long_grid(
        config=_config(),
        strategy=_strategy(),
        klines=[
            _kline(0, open_price="60000", high="60050", low="54000", close="55000"),
            _kline(1, open_price="55000", high="56000", low="53000", close="54000"),
        ],
    )

    assert result.metrics["liquidated"] is True
    assert result.metrics["liquidation_time"] == "2024-01-01T00:00:00Z"
    assert result.metrics["buy_count"] == 1
    assert result.metrics["sell_count"] == 0
    assert len(result.equity_curve) == 1
    assert result.equity_curve[-1].equity == 0
    assert len(result.risk_events) == 1


def test_insufficient_available_margin_skips_buy_without_negative_cash() -> None:
    result = run_long_grid(
        config=_config(fee_rate=200.0),
        strategy=_strategy(),
        klines=[
            _kline(0, open_price="60000", high="60050", low="60000", close="60050"),
        ],
    )

    assert result.trades == []
    assert result.metrics["skipped_orders"][0]["reason"] == "insufficient_available_margin"
    assert result.metrics["final_available_margin"] == pytest.approx(500)
    assert result.equity_curve[-1].equity == pytest.approx(500)


def test_rerun_is_deterministic() -> None:
    klines = [
        _kline(0, open_price="60000", high="60050", low="59500", close="59500"),
        _kline(1, open_price="59500", high="60461.53846153846153846153846", low="59500", close="60461.53846153846153846153846"),
    ]

    first = run_long_grid(config=_config(), strategy=_strategy(), klines=klines)
    second = run_long_grid(config=_config(), strategy=_strategy(), klines=klines)

    assert first.trades == second.trades
    assert first.equity_curve == second.equity_curve
    assert first.metrics["order_events"] == second.metrics["order_events"]
