from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import exp, inf, isfinite, log
from typing import Sequence

from src.backtest_config import RiskEvent, TradeRecord
from src.exceptions import DataValidationError

SECONDS_PER_YEAR = 365.2425 * 24 * 60 * 60


@dataclass(slots=True)
class LiquidationSummary:
    liquidated: bool
    liquidation_time: datetime | None = None
    liquidation_reason: str | None = None
    equity_at_liquidation: float | None = None
    ignored_points_after_liquidation: int = 0


@dataclass(slots=True)
class PerformanceSummary:
    initial_capital: float
    final_equity: float
    net_profit: float
    total_return: float
    annualized_return: float
    high_water_marks: list[float] = field(default_factory=list)
    drawdown_series: list[float] = field(default_factory=list)
    max_drawdown: float = 0.0
    minimum_equity: float = 0.0
    total_fees: float = 0.0
    trade_count: int = 0
    liquidation: LiquidationSummary = field(default_factory=lambda: LiquidationSummary(liquidated=False))


def compute_performance_summary(
    *,
    timestamps: Sequence[datetime],
    equity_curve: Sequence[float],
    initial_capital: float,
    trades: Sequence[TradeRecord] | None = None,
    risk_events: Sequence[RiskEvent] | None = None,
) -> PerformanceSummary:
    _validate_initial_capital(initial_capital)
    _validate_inputs(timestamps, equity_curve)

    aligned_points = _align_and_validate_points(timestamps, equity_curve)
    liquidation = _extract_liquidation_summary(aligned_points, risk_events or [])
    used_points = _apply_liquidation_cutoff(aligned_points, liquidation)

    if not used_points:
        raise DataValidationError("清算後沒有可用的權益資料。")

    equity_values = [equity for _, equity in used_points]
    high_water_marks, drawdowns = _compute_hwm_and_drawdowns(initial_capital, equity_values)

    final_equity = equity_values[-1]
    net_profit = final_equity - initial_capital
    total_return = net_profit / initial_capital
    annualized_return = _annualized_return(initial_capital, final_equity, used_points[0][0], used_points[-1][0])
    total_fees = sum((trade.fee for trade in trades or []), 0.0)
    trade_count = len(trades or [])

    return PerformanceSummary(
        initial_capital=initial_capital,
        final_equity=final_equity,
        net_profit=net_profit,
        total_return=total_return,
        annualized_return=annualized_return,
        high_water_marks=high_water_marks,
        drawdown_series=drawdowns,
        max_drawdown=min(drawdowns),
        minimum_equity=min(equity_values),
        total_fees=total_fees,
        trade_count=trade_count,
        liquidation=liquidation,
    )


def _validate_initial_capital(initial_capital: float) -> None:
    if initial_capital <= 0:
        raise DataValidationError("initial_capital 必須大於 0。")


def _validate_inputs(timestamps: Sequence[datetime], equity_curve: Sequence[float]) -> None:
    if not timestamps or not equity_curve:
        raise DataValidationError("權益序列不可為空。")
    if len(timestamps) != len(equity_curve):
        raise DataValidationError("時間索引與權益序列長度必須一致。")


def _align_and_validate_points(
    timestamps: Sequence[datetime],
    equity_curve: Sequence[float],
) -> list[tuple[datetime, float]]:
    points = [(_ensure_utc(timestamp), float(equity)) for timestamp, equity in zip(timestamps, equity_curve)]
    for index, (_, equity) in enumerate(points):
        if not isfinite(equity):
            raise DataValidationError(f"權益序列包含無效數值: index={index}")
    for left, right in zip(points, points[1:]):
        if right[0] <= left[0]:
            raise DataValidationError("權益序列必須按時間嚴格遞增排序。")
    return points


def _extract_liquidation_summary(
    aligned_points: list[tuple[datetime, float]],
    risk_events: Sequence[RiskEvent],
) -> LiquidationSummary:
    liquidation_events = [
        event for event in risk_events if event.event_type.lower() == "liquidation" or event.severity.lower() == "liquidation"
    ]
    if not liquidation_events:
        return LiquidationSummary(liquidated=False)

    first_event = min(liquidation_events, key=lambda event: _ensure_utc(event.timestamp))
    event_time = _ensure_utc(first_event.timestamp)
    equity_at_event = next((equity for timestamp, equity in aligned_points if timestamp == event_time), None)
    ignored = sum(1 for timestamp, _ in aligned_points if timestamp > event_time)
    return LiquidationSummary(
        liquidated=True,
        liquidation_time=event_time,
        liquidation_reason=first_event.message,
        equity_at_liquidation=equity_at_event,
        ignored_points_after_liquidation=ignored,
    )


def _apply_liquidation_cutoff(
    aligned_points: list[tuple[datetime, float]],
    liquidation: LiquidationSummary,
) -> list[tuple[datetime, float]]:
    if not liquidation.liquidated or liquidation.liquidation_time is None:
        return aligned_points
    cutoff = liquidation.liquidation_time
    used = [point for point in aligned_points if point[0] <= cutoff]
    return used


def _compute_hwm_and_drawdowns(initial_capital: float, equity_values: Sequence[float]) -> tuple[list[float], list[float]]:
    high_water_marks: list[float] = []
    drawdowns: list[float] = []
    running_hwm = initial_capital
    for equity in equity_values:
        running_hwm = max(running_hwm, equity)
        high_water_marks.append(running_hwm)
        drawdowns.append((equity / running_hwm) - 1.0)
    return high_water_marks, drawdowns


def _annualized_return(
    initial_capital: float,
    final_equity: float,
    start_time: datetime,
    end_time: datetime,
) -> float:
    elapsed_seconds = (_ensure_utc(end_time) - _ensure_utc(start_time)).total_seconds()
    if elapsed_seconds <= 0:
        raise DataValidationError("年化報酬率需要正的經過時間。")
    years = elapsed_seconds / SECONDS_PER_YEAR
    if final_equity <= 0:
        return -1.0
    try:
        return exp(log(final_equity / initial_capital) / years) - 1.0
    except OverflowError:
        return inf


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
