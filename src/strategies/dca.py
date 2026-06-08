from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from src.backtest_config import BacktestConfig, BacktestResult, EquityPoint, RiskEvent, StrategyConfig, TradeRecord
from src.data.binance_client import Kline
from src.metrics.performance import compute_performance_summary


STRATEGY_TYPE = "dca_long"
ZERO_FUNDING_FEE = 0.0


@dataclass(slots=True)
class DcaState:
    cash: float
    quantity: float = 0.0
    position_cost: float = 0.0
    entry_fees: float = 0.0
    exit_fee: float = 0.0
    entry_count: int = 0
    last_entry_price: float = 0.0
    average_price: float = 0.0
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    max_unrealized_loss: float = 0.0
    max_unrealized_loss_percent: float = 0.0


def run_dca_long(
    *,
    config: BacktestConfig,
    strategy: StrategyConfig,
    klines: list[Kline],
    finished_at: datetime,
) -> BacktestResult:
    if not klines:
        raise ValueError("klines cannot be empty")

    params = _parse_parameters(strategy)
    state = DcaState(cash=strategy.initial_capital)
    trades: list[TradeRecord] = []
    risk_events: list[RiskEvent] = []
    equity_curve: list[EquityPoint] = []
    filled_levels: set[str] = set()

    _enter_position(
        state=state,
        strategy=strategy,
        symbol=config.market.symbol,
        kline=klines[0],
        fill_price=float(klines[0].open),
        trigger_price=float(klines[0].open),
        entry_type="initial",
        params=params,
        trades=trades,
        filled_levels=filled_levels,
    )

    for kline in klines:
        _process_dca_entries(
            state=state,
            strategy=strategy,
            symbol=config.market.symbol,
            kline=kline,
            params=params,
            trades=trades,
            filled_levels=filled_levels,
        )

        exited = False
        stop_loss_price = _stop_loss_price(state, params)
        if stop_loss_price is not None and float(kline.low) <= stop_loss_price:
            _exit_position(
                state=state,
                strategy=strategy,
                symbol=config.market.symbol,
                timestamp=kline.open_time,
                exit_price=stop_loss_price,
                exit_reason="stop_loss",
                params=params,
                trades=trades,
            )
            exited = True

        if not exited:
            take_profit_price = _take_profit_price(state, params)
            if float(kline.high) >= take_profit_price:
                _exit_position(
                    state=state,
                    strategy=strategy,
                    symbol=config.market.symbol,
                    timestamp=kline.open_time,
                    exit_price=take_profit_price,
                    exit_reason="take_profit",
                    params=params,
                    trades=trades,
                )
                exited = True

        _record_equity_point(state=state, kline=kline, equity_curve=equity_curve)
        if exited:
            break

    if state.exit_price is None:
        last = klines[-1]
        _exit_position(
            state=state,
            strategy=strategy,
            symbol=config.market.symbol,
            timestamp=last.open_time,
            exit_price=float(last.close),
            exit_reason="end_of_backtest",
            params=params,
            trades=trades,
        )
        _record_equity_point(state=state, kline=last, equity_curve=equity_curve)

    performance = compute_performance_summary(
        timestamps=_performance_timestamps(equity_curve, finished_at),
        equity_curve=_performance_values(equity_curve, finished_at),
        initial_capital=strategy.initial_capital,
        trades=trades,
        risk_events=risk_events,
    )
    gross_price_pnl = (state.quantity * float(state.exit_price or 0.0)) - state.position_cost
    funding_fee = ZERO_FUNDING_FEE
    total_fees = state.entry_fees + state.exit_fee + funding_fee
    capital_used = state.position_cost

    metrics = {
        "strategy_type": STRATEGY_TYPE,
        "performance_summary": asdict(performance),
        "total_entries": state.entry_count,
        "first_entry_price": trades[0].price if trades else None,
        "last_entry_price": state.last_entry_price,
        "average_entry_price": state.average_price,
        "take_profit_price": _take_profit_price(state, params),
        "stop_loss_price": _stop_loss_price(state, params),
        "exit_time": _iso(state.exit_time) if state.exit_time else None,
        "exit_price": state.exit_price,
        "exit_reason": state.exit_reason,
        "gross_price_pnl": gross_price_pnl,
        "total_entry_fees": state.entry_fees,
        "exit_fee": state.exit_fee,
        "funding_fee": funding_fee,
        "total_fees": total_fees,
        "maximum_unrealized_loss": state.max_unrealized_loss,
        "maximum_unrealized_loss_percent": state.max_unrealized_loss_percent,
        "capital_used": capital_used,
        "capital_remaining": state.cash,
        "liquidated": False,
        "liquidation_time": None,
        "assumptions": [
            "DCA long uses intrabar conservative order: DCA entries, stop loss, then take profit.",
            "Percent YAML values use whole-percent numbers, e.g. 19 means 19%.",
            "Funding fee is zero when funding_rate_mode is zero.",
        ],
    }

    return BacktestResult(
        config=config,
        started_at=config.market.start,
        finished_at=finished_at,
        initial_capital=strategy.initial_capital,
        final_equity=performance.final_equity,
        total_return=performance.total_return,
        max_drawdown=performance.max_drawdown,
        trades=trades,
        risk_events=risk_events,
        equity_curve=equity_curve,
        metrics=metrics,
    )


def _process_dca_entries(
    *,
    state: DcaState,
    strategy: StrategyConfig,
    symbol: str,
    kline: Kline,
    params: dict[str, Any],
    trades: list[TradeRecord],
    filled_levels: set[str],
) -> None:
    while state.entry_count < params["max_entries"]:
        trigger_price = state.last_entry_price * (1.0 - params["dca_drop_pct"])
        unique_level_id = _level_id(trigger_price)
        if float(kline.low) > trigger_price or unique_level_id in filled_levels:
            return
        required_cash = params["dca_notional"] + (params["dca_notional"] * params["entry_fee_rate"])
        if state.cash < required_cash:
            return
        _enter_position(
            state=state,
            strategy=strategy,
            symbol=symbol,
            kline=kline,
            fill_price=trigger_price,
            trigger_price=trigger_price,
            entry_type="dca",
            params=params,
            trades=trades,
            filled_levels=filled_levels,
        )


def _enter_position(
    *,
    state: DcaState,
    strategy: StrategyConfig,
    symbol: str,
    kline: Kline,
    fill_price: float,
    trigger_price: float,
    entry_type: str,
    params: dict[str, Any],
    trades: list[TradeRecord],
    filled_levels: set[str],
) -> None:
    notional = params["dca_notional"] if entry_type == "dca" else params["first_notional"]
    fee = notional * params["entry_fee_rate"]
    if state.cash < notional + fee:
        raise ValueError("insufficient capital for initial DCA entry")

    quantity = notional / fill_price
    state.cash -= notional + fee
    state.quantity += quantity
    state.position_cost += notional
    state.entry_fees += fee
    state.entry_count += 1
    state.last_entry_price = fill_price
    state.average_price = state.position_cost / state.quantity
    unique_level_id = _level_id(fill_price)
    filled_levels.add(unique_level_id)

    trades.append(
        TradeRecord(
            timestamp=kline.open_time,
            strategy_id=strategy.id,
            symbol=symbol,
            side="buy",
            quantity=quantity,
            price=fill_price,
            fee=fee,
            notes=entry_type,
            metadata={
                "entry_sequence": state.entry_count,
                "entry_type": entry_type,
                "trigger_price": trigger_price,
                "fill_price": fill_price,
                "notional": notional,
                "average_price_after": state.average_price,
                "take_profit_price_after": _take_profit_price(state, params),
                "stop_loss_price_after": _stop_loss_price(state, params),
                "available_capital_after": state.cash,
                "unique_level_id": unique_level_id,
            },
        )
    )


def _exit_position(
    *,
    state: DcaState,
    strategy: StrategyConfig,
    symbol: str,
    timestamp: datetime,
    exit_price: float,
    exit_reason: str,
    params: dict[str, Any],
    trades: list[TradeRecord],
) -> None:
    gross_value = state.quantity * exit_price
    gross_pnl = gross_value - state.position_cost
    exit_fee = gross_value * params["exit_fee_rate"]
    net_pnl = gross_pnl - state.entry_fees - exit_fee
    state.cash += gross_value - exit_fee
    state.exit_fee = exit_fee
    state.exit_time = timestamp
    state.exit_price = exit_price
    state.exit_reason = exit_reason

    trades.append(
        TradeRecord(
            timestamp=timestamp,
            strategy_id=strategy.id,
            symbol=symbol,
            side="sell",
            quantity=state.quantity,
            price=exit_price,
            fee=exit_fee,
            pnl=net_pnl,
            notes=exit_reason,
            metadata={
                "exit_time": _iso(timestamp),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "exit_quantity": state.quantity,
                "exit_fee": exit_fee,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
            },
        )
    )


def _record_equity_point(*, state: DcaState, kline: Kline, equity_curve: list[EquityPoint]) -> None:
    mark_price = float(state.exit_price) if state.exit_price is not None else float(kline.close)
    position_value = 0.0 if state.exit_price is not None else state.quantity * mark_price
    equity = state.cash + position_value
    unrealized = position_value - state.position_cost if state.exit_price is None else 0.0
    state.max_unrealized_loss = min(state.max_unrealized_loss, unrealized)
    if state.position_cost > 0:
        state.max_unrealized_loss_percent = min(state.max_unrealized_loss_percent, unrealized / state.position_cost)
    point = EquityPoint(timestamp=kline.open_time, equity=equity)
    if equity_curve and equity_curve[-1].timestamp == kline.open_time:
        equity_curve[-1] = point
    else:
        equity_curve.append(point)


def _performance_timestamps(equity_curve: list[EquityPoint], finished_at: datetime) -> list[datetime]:
    timestamps = [point.timestamp for point in equity_curve]
    if len(timestamps) == 1 and finished_at > timestamps[0]:
        return [timestamps[0], finished_at]
    return timestamps


def _performance_values(equity_curve: list[EquityPoint], finished_at: datetime) -> list[float]:
    values = [point.equity for point in equity_curve]
    if len(values) == 1 and finished_at > equity_curve[0].timestamp:
        return [values[0], values[0]]
    return values


def _parse_parameters(strategy: StrategyConfig) -> dict[str, Any]:
    raw = strategy.parameters
    first_pct = _percent(raw.get("first_entry_percent"))
    dca_pct = _percent(raw.get("dca_entry_percent"))
    return {
        "position_mode": raw.get("position_mode", "spot"),
        "leverage": float(raw.get("leverage", 1)),
        "first_notional": strategy.initial_capital * first_pct,
        "dca_notional": strategy.initial_capital * dca_pct,
        "dca_drop_pct": _percent(raw.get("dca_drop_percent")),
        "max_entries": int(raw.get("max_entries")),
        "take_profit_pct": _percent(raw.get("take_profit_percent")),
        "stop_loss_pct": None if raw.get("stop_loss_percent") is None else _percent(raw.get("stop_loss_percent")),
        "entry_fee_rate": float(raw.get("entry_fee_rate", 0.0005)),
        "exit_fee_rate": float(raw.get("exit_fee_rate", 0.0005)),
        "funding_rate_mode": raw.get("funding_rate_mode", "zero"),
    }


def _percent(value: Any) -> float:
    if value is None:
        raise ValueError("DCA percent parameter is required")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError("DCA percent parameter must be greater than 0")
    if parsed >= 1:
        return parsed / 100.0
    return parsed


def _take_profit_price(state: DcaState, params: dict[str, Any]) -> float:
    return state.average_price * (1.0 + params["take_profit_pct"])


def _stop_loss_price(state: DcaState, params: dict[str, Any]) -> float | None:
    if params["stop_loss_pct"] is None:
        return None
    return state.average_price * (1.0 - params["stop_loss_pct"])


def _level_id(price: float) -> str:
    return f"{price:.12f}"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
