from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.backtest_config import RiskEvent, TradeRecord
from src.exceptions import DataValidationError
from src.metrics.performance import compute_performance_summary


def _times(start: datetime, count: int, step_hours: int = 1) -> list[datetime]:
    return [start + timedelta(hours=index * step_hours) for index in range(count)]


def _trade(fee: float) -> TradeRecord:
    return TradeRecord(
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        strategy_id="demo-1",
        symbol="BTCUSDT",
        side="BUY",
        quantity=1.0,
        price=100.0,
        fee=fee,
    )


def test_up_down_flat_cases_are_correct() -> None:
    start = datetime(2023, 1, 1, 0, 0, tzinfo=UTC)

    up = compute_performance_summary(
        timestamps=[start, start + timedelta(days=180), start + timedelta(days=360)],
        equity_curve=[100.0, 120.0, 150.0],
        initial_capital=100.0,
        trades=[_trade(1.0), _trade(2.0)],
    )
    assert up.final_equity == 150.0
    assert up.net_profit == 50.0
    assert up.total_return == 0.5
    assert up.high_water_marks == [100.0, 120.0, 150.0]
    assert up.drawdown_series == pytest.approx([0.0, 0.0, 0.0])
    assert up.max_drawdown == 0.0
    assert up.minimum_equity == 100.0
    assert up.total_fees == 3.0
    assert up.trade_count == 2
    up_elapsed_years = (timedelta(days=360).total_seconds()) / (365.2425 * 24 * 60 * 60)
    up_expected_annualized = (150.0 / 100.0) ** (1.0 / up_elapsed_years) - 1.0
    assert up.annualized_return == pytest.approx(up_expected_annualized)

    down = compute_performance_summary(
        timestamps=[start, start + timedelta(days=180), start + timedelta(days=360)],
        equity_curve=[100.0, 90.0, 80.0],
        initial_capital=100.0,
    )
    assert down.final_equity == 80.0
    assert down.net_profit == -20.0
    assert down.total_return == -0.2
    assert down.high_water_marks == [100.0, 100.0, 100.0]
    assert down.drawdown_series == pytest.approx([0.0, -0.1, -0.2])
    assert down.max_drawdown == pytest.approx(-0.2)
    assert down.minimum_equity == 80.0
    down_expected_annualized = (80.0 / 100.0) ** (1.0 / up_elapsed_years) - 1.0
    assert down.annualized_return == pytest.approx(down_expected_annualized)

    flat = compute_performance_summary(
        timestamps=[start, start + timedelta(days=180), start + timedelta(days=360)],
        equity_curve=[100.0, 100.0, 100.0],
        initial_capital=100.0,
    )
    assert flat.final_equity == 100.0
    assert flat.total_return == 0.0
    assert flat.max_drawdown == 0.0
    assert flat.drawdown_series == pytest.approx([0.0, 0.0, 0.0])


def test_cross_year_annualized_return_uses_elapsed_time() -> None:
    start = datetime(2023, 12, 31, 0, 0, tzinfo=UTC)
    end = datetime(2024, 12, 30, 0, 0, tzinfo=UTC)
    summary = compute_performance_summary(
        timestamps=[start, end],
        equity_curve=[100.0, 121.0],
        initial_capital=100.0,
    )

    elapsed_years = (end - start).total_seconds() / (365.2425 * 24 * 60 * 60)
    expected = (121.0 / 100.0) ** (1.0 / elapsed_years) - 1.0
    assert summary.annualized_return == pytest.approx(expected)


def test_empty_and_invalid_input_raise_clear_errors() -> None:
    with pytest.raises(DataValidationError, match="權益序列不可為空"):
        compute_performance_summary(
            timestamps=[],
            equity_curve=[],
            initial_capital=100.0,
        )

    with pytest.raises(DataValidationError, match="initial_capital 必須大於 0"):
        compute_performance_summary(
            timestamps=[datetime(2024, 1, 1, 0, 0, tzinfo=UTC)],
            equity_curve=[100.0],
            initial_capital=0.0,
        )

    with pytest.raises(DataValidationError, match="嚴格遞增排序"):
        compute_performance_summary(
            timestamps=[
                datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
                datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            ],
            equity_curve=[100.0, 90.0],
            initial_capital=100.0,
        )


def test_liquidation_cuts_off_later_equity_points() -> None:
    start = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    liquidation_event = RiskEvent(
        timestamp=start + timedelta(hours=2),
        strategy_id="demo-1",
        event_type="liquidation",
        severity="liquidation",
        message="force liquidated",
    )

    summary = compute_performance_summary(
        timestamps=_times(start, 5),
        equity_curve=[100.0, 95.0, 0.0, 50.0, 60.0],
        initial_capital=100.0,
        risk_events=[liquidation_event],
    )

    assert summary.liquidation.liquidated is True
    assert summary.liquidation.liquidation_time == start + timedelta(hours=2)
    assert summary.liquidation.ignored_points_after_liquidation == 2
    assert summary.final_equity == 0.0
    assert summary.total_return == -1.0
    assert summary.max_drawdown == -1.0
    assert summary.minimum_equity == 0.0
