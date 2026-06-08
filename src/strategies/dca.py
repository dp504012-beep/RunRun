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
    cycle_id: int
    cycle_start_time: datetime
    cycle_start_capital: float
    cycle_base_capital: float
    first_notional: float
    dca_notional: float
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
    trades: list[TradeRecord] = []
    risk_events: list[RiskEvent] = []
    equity_curve: list[EquityPoint] = []
    cycle_results: list[dict[str, Any]] = []
    account_cash = strategy.initial_capital
    account_stop_reason: str | None = None
    state: DcaState | None = None
    filled_levels: set[str] = set()
    cycle_id = 0

    for index, kline in enumerate(klines):
        if state is None:
            if params["cycle_mode"] == "single" and cycle_id >= 1:
                break
            if params["max_cycles"] is not None and cycle_id >= params["max_cycles"]:
                account_stop_reason = "max_cycles"
                break
            account_stop_reason = _account_stop_reason(account_cash=account_cash, strategy=strategy, params=params)
            if account_stop_reason is not None:
                break
            if params["cycle_mode"] == "repeat" and cycle_id > 0 and index == len(klines) - 1:
                break
            next_cycle_id = cycle_id + 1
            state = _start_cycle(
                account_cash=account_cash,
                cycle_id=next_cycle_id,
                strategy=strategy,
                symbol=config.market.symbol,
                kline=kline,
                params=params,
                trades=trades,
            )
            if state is None:
                account_stop_reason = "insufficient_capital"
                break
            cycle_id = next_cycle_id
            filled_levels = {_level_id(float(kline.open))}

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
            cycle_results.append(_cycle_result(state, params))
            account_cash = state.cash
            state = None
            if params["cycle_mode"] == "single":
                break

    if state is not None and state.exit_price is None:
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
        cycle_results.append(_cycle_result(state, params))
        account_cash = state.cash

    if not equity_curve:
        equity_curve.append(EquityPoint(timestamp=klines[0].open_time, equity=account_cash))

    performance = compute_performance_summary(
        timestamps=_performance_timestamps(equity_curve, finished_at),
        equity_curve=_performance_values(equity_curve, finished_at),
        initial_capital=strategy.initial_capital,
        trades=trades,
        risk_events=risk_events,
    )
    funding_fee = ZERO_FUNDING_FEE
    entry_trades = [trade for trade in trades if trade.side == "buy"]
    exit_trades = [trade for trade in trades if trade.side == "sell"]
    total_entry_fees = sum(trade.fee for trade in entry_trades)
    total_exit_fees = sum(trade.fee for trade in exit_trades)
    total_fees = total_entry_fees + total_exit_fees + funding_fee
    total_entries = len(entry_trades)
    total_exits = len(exit_trades)
    final_cycle = cycle_results[-1] if cycle_results else {}
    cycle_returns = [float(item["return"]) for item in cycle_results]
    total_cycle_entries = [int(item["entries"]) for item in cycle_results]

    metrics = {
        "strategy_type": STRATEGY_TYPE,
        "performance_summary": asdict(performance),
        "cycle_mode": params["cycle_mode"],
        "capital_mode": params["capital_mode"],
        "max_cycles": params["max_cycles"],
        "account_stop_reason": account_stop_reason,
        "total_cycles": len(cycle_results),
        "completed_cycles": len(cycle_results),
        "profitable_cycles": sum(1 for item in cycle_results if float(item["net_pnl"]) > 0),
        "losing_cycles": sum(1 for item in cycle_results if float(item["net_pnl"]) < 0),
        "take_profit_cycles": sum(1 for item in cycle_results if item["exit_reason"] == "take_profit"),
        "stop_loss_cycles": sum(1 for item in cycle_results if item["exit_reason"] == "stop_loss"),
        "end_of_backtest_cycles": sum(1 for item in cycle_results if item["exit_reason"] == "end_of_backtest"),
        "average_cycle_return": sum(cycle_returns) / len(cycle_returns) if cycle_returns else 0.0,
        "best_cycle_return": max(cycle_returns) if cycle_returns else 0.0,
        "worst_cycle_return": min(cycle_returns) if cycle_returns else 0.0,
        "average_entries_per_cycle": sum(total_cycle_entries) / len(total_cycle_entries) if total_cycle_entries else 0.0,
        "max_entries_in_a_cycle": max(total_cycle_entries) if total_cycle_entries else 0,
        "total_entries": total_entries,
        "total_exits": total_exits,
        "first_cycle_start": _iso(cycle_results[0]["start_time"]) if cycle_results else None,
        "last_cycle_end": _iso(cycle_results[-1]["end_time"]) if cycle_results else None,
        "cycle_results": cycle_results,
        "first_entry_price": entry_trades[0].price if entry_trades else None,
        "last_entry_price": entry_trades[-1].price if entry_trades else None,
        "average_entry_price": final_cycle.get("average_entry_price"),
        "take_profit_price": final_cycle.get("take_profit_price"),
        "stop_loss_price": final_cycle.get("stop_loss_price"),
        "exit_time": _iso(final_cycle["end_time"]) if final_cycle else None,
        "exit_price": final_cycle.get("exit_price"),
        "exit_reason": final_cycle.get("exit_reason"),
        "gross_price_pnl": sum(float(item["gross_pnl"]) for item in cycle_results),
        "total_entry_fees": total_entry_fees,
        "exit_fee": total_exit_fees,
        "funding_fee": funding_fee,
        "total_funding_fee": funding_fee,
        "total_fees": total_fees,
        "maximum_unrealized_loss": min((float(item["maximum_unrealized_loss"]) for item in cycle_results), default=0.0),
        "maximum_unrealized_loss_percent": min((float(item["maximum_unrealized_loss_percent"]) for item in cycle_results), default=0.0),
        "capital_used": sum(float(trade.metadata.get("notional", 0.0)) for trade in entry_trades),
        "capital_remaining": account_cash,
        "liquidated": False,
        "liquidation_time": None,
        "assumptions": [
            "DCA long uses intrabar conservative order: DCA entries, stop loss, then take profit.",
            f"DCA cycle_mode is {params['cycle_mode']} and capital_mode is {params['capital_mode']}.",
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
        required_cash = state.dca_notional + (state.dca_notional * params["entry_fee_rate"])
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


def _start_cycle(
    *,
    account_cash: float,
    cycle_id: int,
    strategy: StrategyConfig,
    symbol: str,
    kline: Kline,
    params: dict[str, Any],
    trades: list[TradeRecord],
) -> DcaState | None:
    cycle_base_capital = account_cash if params["capital_mode"] == "rolling" else strategy.initial_capital
    first_notional = cycle_base_capital * params["first_entry_pct"]
    dca_notional = cycle_base_capital * params["dca_entry_pct"]
    required_cash = first_notional + (first_notional * params["entry_fee_rate"])
    if account_cash < required_cash:
        return None
    state = DcaState(
        cash=account_cash,
        cycle_id=cycle_id,
        cycle_start_time=kline.open_time,
        cycle_start_capital=account_cash,
        cycle_base_capital=cycle_base_capital,
        first_notional=first_notional,
        dca_notional=dca_notional,
    )
    _enter_position(
        state=state,
        strategy=strategy,
        symbol=symbol,
        kline=kline,
        fill_price=float(kline.open),
        trigger_price=float(kline.open),
        entry_type="initial",
        params=params,
        trades=trades,
        filled_levels=set(),
    )
    return state


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
    notional = state.dca_notional if entry_type == "dca" else state.first_notional
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
                "cycle_id": state.cycle_id,
                "cycle_entry_sequence": state.entry_count,
                "global_trade_sequence": len(trades) + 1,
                "cycle_start_time": _iso(state.cycle_start_time),
                "cycle_start_capital": state.cycle_start_capital,
                "entry_sequence": state.entry_count,
                "entry_type": entry_type,
                "fill_time": _iso(kline.open_time),
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
    cycle_fees = state.entry_fees + exit_fee
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
                "cycle_id": state.cycle_id,
                "cycle_entry_sequence": "",
                "global_trade_sequence": len(trades) + 1,
                "cycle_start_time": _iso(state.cycle_start_time),
                "cycle_start_capital": state.cycle_start_capital,
                "exit_time": _iso(timestamp),
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "exit_quantity": state.quantity,
                "exit_fee": exit_fee,
                "gross_pnl": gross_pnl,
                "net_pnl": net_pnl,
                "cycle_gross_pnl": gross_pnl,
                "cycle_fees": cycle_fees,
                "cycle_funding_fee": ZERO_FUNDING_FEE,
                "cycle_net_pnl": net_pnl,
                "cycle_return": (state.cash - state.cycle_start_capital) / state.cycle_start_capital if state.cycle_start_capital else 0.0,
                "cycle_ending_equity": state.cash,
            },
        )
    )


def _cycle_result(state: DcaState, params: dict[str, Any]) -> dict[str, Any]:
    gross_pnl = (state.quantity * float(state.exit_price or 0.0)) - state.position_cost
    total_fees = state.entry_fees + state.exit_fee + ZERO_FUNDING_FEE
    net_pnl = gross_pnl - total_fees
    cycle_return = (state.cash - state.cycle_start_capital) / state.cycle_start_capital if state.cycle_start_capital else 0.0
    return {
        "cycle_id": state.cycle_id,
        "start_time": state.cycle_start_time,
        "end_time": state.exit_time,
        "start_capital": state.cycle_start_capital,
        "base_capital": state.cycle_base_capital,
        "end_equity": state.cash,
        "entries": state.entry_count,
        "average_entry_price": state.average_price,
        "take_profit_price": _take_profit_price(state, params),
        "stop_loss_price": _stop_loss_price(state, params),
        "exit_price": state.exit_price,
        "exit_reason": state.exit_reason,
        "gross_pnl": gross_pnl,
        "total_fees": total_fees,
        "funding_fee": ZERO_FUNDING_FEE,
        "net_pnl": net_pnl,
        "return": cycle_return,
        "maximum_unrealized_loss": state.max_unrealized_loss,
        "maximum_unrealized_loss_percent": state.max_unrealized_loss_percent,
    }


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
    cycle_mode = str(raw.get("cycle_mode", "single"))
    capital_mode = str(raw.get("capital_mode", "rolling"))
    if cycle_mode not in {"single", "repeat"}:
        raise ValueError("cycle_mode must be single or repeat")
    if capital_mode not in {"rolling", "fixed_initial"}:
        raise ValueError("capital_mode must be rolling or fixed_initial")
    max_cycles = raw.get("max_cycles")
    return {
        "cycle_mode": cycle_mode,
        "capital_mode": capital_mode,
        "max_cycles": None if max_cycles is None else int(max_cycles),
        "position_mode": raw.get("position_mode", "spot"),
        "leverage": float(raw.get("leverage", 1)),
        "first_entry_pct": first_pct,
        "dca_entry_pct": dca_pct,
        "dca_drop_pct": _percent(raw.get("dca_drop_percent")),
        "max_entries": int(raw.get("max_entries")),
        "take_profit_pct": _percent(raw.get("take_profit_percent")),
        "stop_loss_pct": None if raw.get("stop_loss_percent") is None else _percent(raw.get("stop_loss_percent")),
        "account_stop_loss_pct": None if raw.get("account_stop_loss_percent") is None else _percent(raw.get("account_stop_loss_percent")),
        "account_take_profit_pct": None if raw.get("account_take_profit_percent") is None else _percent(raw.get("account_take_profit_percent")),
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


def _account_stop_reason(*, account_cash: float, strategy: StrategyConfig, params: dict[str, Any]) -> str | None:
    if params["account_stop_loss_pct"] is not None and account_cash <= strategy.initial_capital * (1.0 - params["account_stop_loss_pct"]):
        return "account_stop_loss"
    if params["account_take_profit_pct"] is not None and account_cash >= strategy.initial_capital * (1.0 + params["account_take_profit_pct"]):
        return "account_take_profit"
    return None


def _level_id(price: float) -> str:
    return f"{price:.12f}"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
