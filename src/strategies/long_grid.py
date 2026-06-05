from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.backtest_config import BacktestConfig, BacktestResult, EquityPoint, RiskEvent, StrategyConfig, TradeRecord
from src.data.binance_client import Kline
from src.exceptions import DataValidationError
from src.metrics.performance import PerformanceSummary, compute_performance_summary
from src.strategies.grid_orders import GridFillEvent, generate_arithmetic_grid, intrabar_path

STRATEGY_TYPE = "long_grid"
SPEC_VERSION = "long-grid-v1"
LIQUIDATION_MODEL = "simplified"
LIQUIDATION_MODEL_VERSION = "simplified_v1"


@dataclass(slots=True)
class GridLot:
    lot_level: int
    entry_price: Decimal
    quantity: Decimal
    margin: Decimal
    entry_fee: Decimal
    entry_order_id: str
    liquidation_price: Decimal


@dataclass(slots=True)
class GridStateSnapshot:
    timestamp: datetime
    cash_available: Decimal
    margin_locked: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    equity: Decimal
    open_lot_count: int
    position_quantity: Decimal
    average_entry_price: Decimal | None


@dataclass(slots=True)
class LongGridResult:
    backtest_result: BacktestResult
    state_snapshots: list[GridStateSnapshot]


def run_long_grid(
    *,
    config: BacktestConfig,
    strategy: StrategyConfig,
    klines: list[Kline],
    finished_at: datetime | None = None,
) -> BacktestResult:
    if strategy.type != STRATEGY_TYPE:
        raise DataValidationError(f"策略類型不符: {strategy.type}")
    if not klines:
        raise DataValidationError("十倍做多網格策略需要至少一根 K 線。")

    sorted_klines = sorted(klines, key=lambda item: item.open_time)
    if [item.open_time for item in klines] != [item.open_time for item in sorted_klines]:
        raise DataValidationError("K 線必須按 open_time 排序。")

    runner = _LongGridRunner(config=config, strategy=strategy, klines=sorted_klines)
    return runner.run(finished_at=finished_at)


class _LongGridRunner:
    def __init__(self, *, config: BacktestConfig, strategy: StrategyConfig, klines: list[Kline]) -> None:
        self.config = config
        self.strategy = strategy
        self.klines = klines
        self.initial_margin = Decimal(str(strategy.initial_capital))
        self.leverage = _parameter_decimal(strategy, "leverage")
        self.lower_bound = _parameter_decimal(strategy, "lower_bound")
        self.upper_bound = _parameter_decimal(strategy, "upper_bound")
        self.grid_count = _parameter_int(strategy, "grid_count")
        self.fee_rate = Decimal(str(config.common.fee_rate))
        liquidation_model = str(strategy.parameters.get("liquidation_model", LIQUIDATION_MODEL))
        if liquidation_model != LIQUIDATION_MODEL:
            raise DataValidationError(f"不支援的清算模型: {liquidation_model}")
        if self.initial_margin <= 0:
            raise DataValidationError("initial_capital 必須大於 0。")
        if self.leverage <= 1:
            raise DataValidationError("leverage 必須大於 1。")
        if self.fee_rate < 0:
            raise DataValidationError("fee_rate 不可小於 0。")

        self.grid_prices = generate_arithmetic_grid(
            lower_bound=self.lower_bound,
            upper_bound=self.upper_bound,
            grid_count=self.grid_count,
        )
        self.grid_step = self.grid_prices[1] - self.grid_prices[0]
        self.margin_per_grid = self.initial_margin / Decimal(self.grid_count)
        self.notional_per_lot = self.margin_per_grid * self.leverage
        self.cash_available = self.initial_margin
        self.margin_locked = Decimal("0")
        self.realized_pnl = Decimal("0")
        self.fees_paid = Decimal("0")
        self.open_lots: dict[int, GridLot] = {}
        self.armed_buy_order_ids: dict[int, str] = {}
        self.armed_sell_order_ids: dict[int, str] = {}
        self.order_events: list[GridFillEvent] = []
        self.trades: list[TradeRecord] = []
        self.risk_events: list[RiskEvent] = []
        self.equity_curve: list[EquityPoint] = []
        self.state_snapshots: list[GridStateSnapshot] = []
        self.skipped_orders: list[dict[str, Any]] = []
        self._next_order_sequence = 1
        self.liquidated = False
        self.liquidation_time: datetime | None = None
        self.liquidation_reason: str | None = None
        self.processed_candles = 0
        self.out_of_range_candles = 0
        self._arm_initial_buys(klines[0].open)

    def run(self, *, finished_at: datetime | None) -> BacktestResult:
        for kline in self.klines:
            if self.liquidated:
                break
            self.processed_candles += 1
            if kline.close < self.lower_bound or kline.close > self.upper_bound:
                self.out_of_range_candles += 1
            self._process_kline(kline)
            self._record_snapshot(kline.open_time, kline.close)

        performance = _compute_performance(
            equity_curve=self.equity_curve,
            initial_margin=float(self.initial_margin),
            trades=self.trades,
            risk_events=self.risk_events,
        )
        return BacktestResult(
            config=self.config,
            started_at=self.klines[0].open_time,
            finished_at=finished_at or datetime.now(UTC),
            initial_capital=float(self.initial_margin),
            final_equity=performance.final_equity,
            total_return=performance.total_return,
            max_drawdown=performance.max_drawdown,
            trades=self.trades,
            risk_events=self.risk_events,
            equity_curve=self.equity_curve,
            metrics=self._metrics(performance),
        )

    def _process_kline(self, kline: Kline) -> None:
        self._process_open_touch(kline)
        path = intrabar_path(kline)
        for start, end in zip(path, path[1:]):
            if self.liquidated:
                return
            if end < start:
                self._process_downward_segment(kline, start, end)
            elif end > start:
                self._process_upward_segment(kline, start, end)

    def _process_open_touch(self, kline: Kline) -> None:
        buy_levels = [
            level
            for level in self.armed_buy_order_ids
            if self.grid_prices[level] == kline.open
        ]
        for level in sorted(buy_levels, key=lambda item: self.grid_prices[item], reverse=True):
            self._execute_buy(kline, level)

        sell_levels = [
            level
            for level in self.armed_sell_order_ids
            if self.grid_prices[level] == kline.open
        ]
        for level in sorted(sell_levels, key=lambda item: self.grid_prices[item]):
            self._execute_sell(kline, level)

    def _process_downward_segment(self, kline: Kline, start: Decimal, end: Decimal) -> None:
        buy_levels = [
            level
            for level in self.armed_buy_order_ids
            if end <= self.grid_prices[level] <= start
        ]
        for level in sorted(buy_levels, key=lambda item: self.grid_prices[item], reverse=True):
            self._execute_buy(kline, level)
            self._check_liquidation(kline, segment_low=end)
            if self.liquidated:
                return
        self._check_liquidation(kline, segment_low=end)

    def _process_upward_segment(self, kline: Kline, start: Decimal, end: Decimal) -> None:
        sell_levels = [
            level
            for level in self.armed_sell_order_ids
            if start <= self.grid_prices[level] <= end
        ]
        for level in sorted(sell_levels, key=lambda item: self.grid_prices[item]):
            self._execute_sell(kline, level)

    def _execute_buy(self, kline: Kline, level: int) -> None:
        order_id = self.armed_buy_order_ids[level]
        price = self.grid_prices[level]
        quantity = self.notional_per_lot / price
        fee = self.notional_per_lot * self.fee_rate
        required_cash = self.margin_per_grid + fee
        if self.cash_available < required_cash:
            self.skipped_orders.append(
                {
                    "timestamp": kline.open_time.isoformat(),
                    "order_id": order_id,
                    "side": "BUY",
                    "grid_level": level,
                    "reason": "insufficient_available_margin",
                    "required_cash": float(required_cash),
                    "cash_available": float(self.cash_available),
                }
            )
            return

        replacement_order_id = self._new_order_id()
        del self.armed_buy_order_ids[level]
        self.armed_sell_order_ids[level + 1] = replacement_order_id
        self.cash_available -= required_cash
        self.margin_locked += self.margin_per_grid
        self.fees_paid += fee
        self.open_lots[level] = GridLot(
            lot_level=level,
            entry_price=price,
            quantity=quantity,
            margin=self.margin_per_grid,
            entry_fee=fee,
            entry_order_id=order_id,
            liquidation_price=_simplified_lot_liquidation_price(
                entry_price=price,
                margin=self.margin_per_grid,
                open_fee=fee,
                quantity=quantity,
            ),
        )
        self._append_event(kline, order_id, "BUY", level, price, quantity, fee, replacement_order_id)
        self._append_trade(kline, "BUY", quantity, price, fee, Decimal("0"))

    def _execute_sell(self, kline: Kline, sell_level: int) -> None:
        lot_level = sell_level - 1
        lot = self.open_lots.pop(lot_level)
        order_id = self.armed_sell_order_ids.pop(sell_level)
        price = self.grid_prices[sell_level]
        gross_pnl = (price - lot.entry_price) * lot.quantity
        fee = price * lot.quantity * self.fee_rate
        replacement_order_id = self._new_order_id()
        self.armed_buy_order_ids[lot_level] = replacement_order_id
        self.cash_available += lot.margin + gross_pnl - fee
        self.margin_locked -= lot.margin
        self.realized_pnl += gross_pnl
        self.fees_paid += fee
        self._append_event(kline, order_id, "SELL", sell_level, price, lot.quantity, fee, replacement_order_id)
        self._append_trade(kline, "SELL", lot.quantity, price, fee, gross_pnl)

    def _check_liquidation(self, kline: Kline, *, segment_low: Decimal) -> None:
        if not self.open_lots:
            return
        touched = [
            lot
            for lot in self.open_lots.values()
            if segment_low <= lot.liquidation_price
        ]
        if not touched:
            return
        lot = max(touched, key=lambda item: item.liquidation_price)
        self.liquidated = True
        self.liquidation_time = kline.open_time
        self.liquidation_reason = "simplified liquidation price touched by intrabar path."
        self.cash_available = Decimal("0")
        self.margin_locked = Decimal("0")
        self.open_lots.clear()
        self.armed_buy_order_ids.clear()
        self.armed_sell_order_ids.clear()
        self.equity_curve.append(EquityPoint(timestamp=kline.open_time, equity=0.0))
        self.risk_events.append(
            RiskEvent(
                timestamp=kline.open_time,
                strategy_id=self.strategy.id,
                event_type="liquidation",
                severity="liquidation",
                message="Simplified liquidation price touched by intrabar path.",
                details={
                    "liquidation_model": LIQUIDATION_MODEL,
                    "liquidation_model_version": LIQUIDATION_MODEL_VERSION,
                    "liquidation_price": float(lot.liquidation_price),
                    "segment_low": float(segment_low),
                    "lot_level": lot.lot_level,
                },
            )
        )

    def _record_snapshot(self, timestamp: datetime, mark_price: Decimal) -> None:
        if self.liquidated:
            snapshot = GridStateSnapshot(
                timestamp=timestamp,
                cash_available=Decimal("0"),
                margin_locked=Decimal("0"),
                realized_pnl=self.realized_pnl,
                unrealized_pnl=Decimal("0"),
                fees_paid=self.fees_paid,
                equity=Decimal("0"),
                open_lot_count=0,
                position_quantity=Decimal("0"),
                average_entry_price=None,
            )
            self.state_snapshots.append(snapshot)
            return

        unrealized_pnl = self._unrealized_pnl(mark_price)
        equity = self.cash_available + self.margin_locked + unrealized_pnl
        snapshot = GridStateSnapshot(
            timestamp=timestamp,
            cash_available=self.cash_available,
            margin_locked=self.margin_locked,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized_pnl,
            fees_paid=self.fees_paid,
            equity=equity,
            open_lot_count=len(self.open_lots),
            position_quantity=self._position_quantity(),
            average_entry_price=self._average_entry_price(),
        )
        self.state_snapshots.append(snapshot)
        self.equity_curve.append(EquityPoint(timestamp=timestamp, equity=float(equity)))

    def _arm_initial_buys(self, first_open: Decimal) -> None:
        for level, price in enumerate(self.grid_prices[: self.grid_count]):
            if price <= first_open:
                self.armed_buy_order_ids[level] = self._new_order_id()

    def _append_event(
        self,
        kline: Kline,
        order_id: str,
        side: str,
        grid_level: int,
        price: Decimal,
        quantity: Decimal,
        fee: Decimal,
        replacement_order_id: str,
    ) -> None:
        self.order_events.append(
            GridFillEvent(
                timestamp=kline.open_time,
                order_id=order_id,
                side=side,  # type: ignore[arg-type]
                grid_level=grid_level,
                price=price,
                quantity=quantity,
                fee=fee,
                replacement_order_id=replacement_order_id,
            )
        )

    def _append_trade(
        self,
        kline: Kline,
        side: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        pnl: Decimal,
    ) -> None:
        self.trades.append(
            TradeRecord(
                timestamp=kline.open_time,
                strategy_id=self.strategy.id,
                symbol=self.config.market.symbol,
                side=side,
                quantity=float(quantity),
                price=float(price),
                fee=float(fee),
                pnl=float(pnl),
                metadata={
                    "strategy_type": STRATEGY_TYPE,
                    "grid_level": self.order_events[-1].grid_level,
                    "order_id": self.order_events[-1].order_id,
                    "replacement_order_id": self.order_events[-1].replacement_order_id,
                },
            )
        )

    def _metrics(self, performance: PerformanceSummary) -> dict[str, Any]:
        final_mark = self.klines[min(self.processed_candles, len(self.klines)) - 1].close
        final_unrealized = Decimal("0") if self.liquidated else self._unrealized_pnl(final_mark)
        return {
            "strategy_type": STRATEGY_TYPE,
            "spec_version": SPEC_VERSION,
            "liquidation_model": LIQUIDATION_MODEL,
            "liquidation_model_version": LIQUIDATION_MODEL_VERSION,
            "initial_margin": float(self.initial_margin),
            "leverage": float(self.leverage),
            "grid_lower_bound": float(self.lower_bound),
            "grid_upper_bound": float(self.upper_bound),
            "grid_count": self.grid_count,
            "grid_step": float(self.grid_step),
            "margin_per_grid": float(self.margin_per_grid),
            "notional_per_lot": float(self.notional_per_lot),
            "position_quantity": float(Decimal("0") if self.liquidated else self._position_quantity()),
            "average_entry_price": _float_or_none(None if self.liquidated else self._average_entry_price()),
            "realized_pnl": float(self.realized_pnl),
            "realized_grid_profit": float(self.realized_pnl - self.fees_paid),
            "unrealized_pnl": float(final_unrealized),
            "final_available_margin": float(self.cash_available),
            "final_margin_locked": float(self.margin_locked),
            "buy_count": sum(1 for event in self.order_events if event.side == "BUY"),
            "sell_count": sum(1 for event in self.order_events if event.side == "SELL"),
            "total_fees": float(self.fees_paid),
            "order_events": [_event_dict(event) for event in self.order_events],
            "state_snapshots": [_snapshot_dict(snapshot) for snapshot in self.state_snapshots],
            "skipped_orders": self.skipped_orders,
            "liquidated": self.liquidated,
            "liquidation_time": self.liquidation_time.isoformat().replace("+00:00", "Z") if self.liquidation_time else None,
            "liquidation_reason": self.liquidation_reason,
            "minimum_equity": performance.minimum_equity,
            "out_of_range_time_ratio": self.out_of_range_candles / self.processed_candles if self.processed_candles else 0.0,
            "performance_summary": asdict(performance),
            "assumptions": [
                "策略版本 long-grid-v1，做多網格，funding rate 固定為 0。",
                "130 代表 130 個區間與 131 個價格點。",
                "每格使用相同初始保證金預算，成交名義金額等於 margin_per_grid * leverage。",
                "OHLC 依凍結規格 intrabar path 處理，無法還原真實成交順序。",
                "清算使用 simplified_v1，任一 lot 觸及清算價後整體策略停止且權益歸零。",
                "資金不足時買單跳過並保留，不允許負可用保證金後繼續開單。",
            ],
        }

    def _unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return sum((mark_price - lot.entry_price) * lot.quantity for lot in self.open_lots.values())

    def _position_quantity(self) -> Decimal:
        return sum((lot.quantity for lot in self.open_lots.values()), Decimal("0"))

    def _average_entry_price(self) -> Decimal | None:
        quantity = self._position_quantity()
        if quantity == 0:
            return None
        notional = sum((lot.entry_price * lot.quantity for lot in self.open_lots.values()), Decimal("0"))
        return notional / quantity

    def _new_order_id(self) -> str:
        order_id = f"grid-{self._next_order_sequence:06d}"
        self._next_order_sequence += 1
        return order_id


def _compute_performance(
    *,
    equity_curve: list[EquityPoint],
    initial_margin: float,
    trades: list[TradeRecord],
    risk_events: list[RiskEvent],
) -> PerformanceSummary:
    if len(equity_curve) == 1:
        point = equity_curve[0]
        if risk_events:
            equity_curve = [
                EquityPoint(timestamp=point.timestamp - timedelta(microseconds=1), equity=initial_margin),
                point,
            ]
        else:
            equity_curve = [point, EquityPoint(timestamp=point.timestamp + timedelta(microseconds=1), equity=point.equity)]
    return compute_performance_summary(
        timestamps=[point.timestamp for point in equity_curve],
        equity_curve=[point.equity for point in equity_curve],
        initial_capital=initial_margin,
        trades=trades,
        risk_events=risk_events,
    )


def _simplified_lot_liquidation_price(
    *,
    entry_price: Decimal,
    margin: Decimal,
    open_fee: Decimal,
    quantity: Decimal,
) -> Decimal:
    return entry_price - ((margin - open_fee) / quantity)


def _parameter_decimal(strategy: StrategyConfig, name: str) -> Decimal:
    if name not in strategy.parameters:
        raise DataValidationError(f"缺少策略參數: {name}")
    return Decimal(str(strategy.parameters[name]))


def _parameter_int(strategy: StrategyConfig, name: str) -> int:
    if name not in strategy.parameters:
        raise DataValidationError(f"缺少策略參數: {name}")
    value = strategy.parameters[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataValidationError(f"{name} 必須是整數。")
    return value


def _event_dict(event: GridFillEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
        "order_id": event.order_id,
        "side": event.side,
        "grid_level": event.grid_level,
        "price": float(event.price),
        "quantity": float(event.quantity),
        "fee": float(event.fee),
        "replacement_order_id": event.replacement_order_id,
    }


def _snapshot_dict(snapshot: GridStateSnapshot) -> dict[str, Any]:
    return {
        "timestamp": snapshot.timestamp.isoformat().replace("+00:00", "Z"),
        "cash_available": float(snapshot.cash_available),
        "margin_locked": float(snapshot.margin_locked),
        "realized_pnl": float(snapshot.realized_pnl),
        "unrealized_pnl": float(snapshot.unrealized_pnl),
        "fees_paid": float(snapshot.fees_paid),
        "equity": float(snapshot.equity),
        "open_lot_count": snapshot.open_lot_count,
        "position_quantity": float(snapshot.position_quantity),
        "average_entry_price": _float_or_none(snapshot.average_entry_price),
    }


def _float_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)
