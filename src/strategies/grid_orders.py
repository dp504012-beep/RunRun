from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from src.data.binance_client import Kline
from src.exceptions import DataValidationError

OrderSide = Literal["BUY", "SELL"]
OrderStatus = Literal["pending", "filled", "replaced"]


@dataclass(slots=True)
class GridOrderConfig:
    lower_bound: Decimal
    upper_bound: Decimal
    grid_count: int
    initial_margin: Decimal
    leverage: Decimal
    fee_rate: Decimal


@dataclass(slots=True)
class GridOrder:
    order_id: str
    side: OrderSide
    grid_level: int
    lot_level: int
    price: Decimal
    quantity: Decimal
    status: OrderStatus = "pending"
    replacement_order_id: str | None = None


@dataclass(slots=True)
class GridFillEvent:
    timestamp: datetime
    order_id: str
    side: OrderSide
    grid_level: int
    price: Decimal
    quantity: Decimal
    fee: Decimal
    replacement_order_id: str


@dataclass(slots=True)
class GridOrderSimulationResult:
    grid_prices: list[Decimal]
    orders: list[GridOrder]
    events: list[GridFillEvent]


class GridOrderBook:
    def __init__(self, *, config: GridOrderConfig, first_open: Decimal) -> None:
        _validate_config(config)
        self.config = config
        self.grid_prices = generate_arithmetic_grid(
            lower_bound=config.lower_bound,
            upper_bound=config.upper_bound,
            grid_count=config.grid_count,
        )
        self.margin_per_grid = config.initial_margin / Decimal(config.grid_count)
        self.notional_per_lot = self.margin_per_grid * config.leverage
        self.orders: list[GridOrder] = []
        self.events: list[GridFillEvent] = []
        self._next_order_sequence = 1
        self._create_initial_orders(first_open)

    def process_kline(self, kline: Kline) -> None:
        path = intrabar_path(kline)
        for start, end in zip(path, path[1:]):
            if end < start:
                self._process_downward_segment(kline, start, end)
            elif end > start:
                self._process_upward_segment(kline, start, end)

    def result(self) -> GridOrderSimulationResult:
        return GridOrderSimulationResult(
            grid_prices=self.grid_prices,
            orders=self.orders,
            events=self.events,
        )

    def _create_initial_orders(self, first_open: Decimal) -> None:
        last_buy_level = self.config.grid_count - 1
        for level, price in enumerate(self.grid_prices[: self.config.grid_count]):
            if level <= last_buy_level and price < first_open:
                self._append_order("BUY", grid_level=level, lot_level=level, price=price, quantity=self._buy_quantity(price))

    def _process_downward_segment(self, kline: Kline, start: Decimal, end: Decimal) -> None:
        orders = [
            order
            for order in self._pending_orders("BUY")
            if end <= order.price < start
        ]
        for order in sorted(orders, key=lambda item: item.price, reverse=True):
            self._fill_order(kline, order)

    def _process_upward_segment(self, kline: Kline, start: Decimal, end: Decimal) -> None:
        orders = [
            order
            for order in self._pending_orders("SELL")
            if start < order.price <= end
        ]
        for order in sorted(orders, key=lambda item: item.price):
            self._fill_order(kline, order)

    def _pending_orders(self, side: OrderSide) -> list[GridOrder]:
        return [order for order in self.orders if order.side == side and order.status == "pending"]

    def _fill_order(self, kline: Kline, order: GridOrder) -> None:
        if order.status != "pending":
            return

        replacement = self._create_replacement_order(order)
        order.status = "replaced"
        order.replacement_order_id = replacement.order_id
        self.events.append(
            GridFillEvent(
                timestamp=kline.open_time,
                order_id=order.order_id,
                side=order.side,
                grid_level=order.grid_level,
                price=order.price,
                quantity=order.quantity,
                fee=self._fee(order),
                replacement_order_id=replacement.order_id,
            )
        )

    def _create_replacement_order(self, order: GridOrder) -> GridOrder:
        if order.side == "BUY":
            sell_level = order.lot_level + 1
            return self._append_order(
                "SELL",
                grid_level=sell_level,
                lot_level=order.lot_level,
                price=self.grid_prices[sell_level],
                quantity=order.quantity,
            )

        buy_level = order.lot_level
        buy_price = self.grid_prices[buy_level]
        return self._append_order(
            "BUY",
            grid_level=buy_level,
            lot_level=buy_level,
            price=buy_price,
            quantity=self._buy_quantity(buy_price),
        )

    def _append_order(
        self,
        side: OrderSide,
        *,
        grid_level: int,
        lot_level: int,
        price: Decimal,
        quantity: Decimal,
    ) -> GridOrder:
        order = GridOrder(
            order_id=f"grid-{self._next_order_sequence:06d}",
            side=side,
            grid_level=grid_level,
            lot_level=lot_level,
            price=price,
            quantity=quantity,
        )
        self._next_order_sequence += 1
        self.orders.append(order)
        return order

    def _buy_quantity(self, price: Decimal) -> Decimal:
        return self.notional_per_lot / price

    def _fee(self, order: GridOrder) -> Decimal:
        return order.price * order.quantity * self.config.fee_rate


def generate_arithmetic_grid(
    *,
    lower_bound: Decimal,
    upper_bound: Decimal,
    grid_count: int,
) -> list[Decimal]:
    if grid_count <= 0:
        raise DataValidationError("grid_count 必須大於 0。")
    if lower_bound >= upper_bound:
        raise DataValidationError("lower_bound 必須小於 upper_bound。")
    step = (upper_bound - lower_bound) / Decimal(grid_count)
    return [lower_bound + Decimal(index) * step for index in range(grid_count + 1)]


def simulate_grid_orders(
    *,
    config: GridOrderConfig,
    klines: list[Kline],
) -> GridOrderSimulationResult:
    if not klines:
        raise DataValidationError("網格訂單模擬需要至少一根 K 線。")
    sorted_klines = sorted(klines, key=lambda item: item.open_time)
    if [item.open_time for item in klines] != [item.open_time for item in sorted_klines]:
        raise DataValidationError("K 線必須按 open_time 排序。")

    order_book = GridOrderBook(config=config, first_open=klines[0].open)
    for kline in klines:
        order_book.process_kline(kline)
    return order_book.result()


def intrabar_path(kline: Kline) -> list[Decimal]:
    if kline.close >= kline.open:
        return [kline.open, kline.low, kline.high, kline.close]
    return [kline.open, kline.high, kline.low, kline.close]


def _validate_config(config: GridOrderConfig) -> None:
    if config.initial_margin <= 0:
        raise DataValidationError("initial_margin 必須大於 0。")
    if config.leverage <= 0:
        raise DataValidationError("leverage 必須大於 0。")
    if config.fee_rate < 0:
        raise DataValidationError("fee_rate 不可小於 0。")
