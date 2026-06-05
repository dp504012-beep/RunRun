from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.data.binance_client import Kline
from src.strategies.grid_orders import GridOrderConfig, generate_arithmetic_grid, simulate_grid_orders


def _config() -> GridOrderConfig:
    return GridOrderConfig(
        lower_bound=Decimal("60000"),
        upper_bound=Decimal("120000"),
        grid_count=130,
        initial_margin=Decimal("500"),
        leverage=Decimal("10"),
        fee_rate=Decimal("0.001"),
    )


def _kline(
    hour: int,
    *,
    open_price: str,
    high: str,
    low: str,
    close: str,
) -> Kline:
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


def test_generate_arithmetic_grid_uses_130_intervals() -> None:
    grid = generate_arithmetic_grid(
        lower_bound=Decimal("60000"),
        upper_bound=Decimal("120000"),
        grid_count=130,
    )

    assert len(grid) == 131
    assert grid[0] == Decimal("60000")
    assert grid[-1] == Decimal("120000")
    assert float(grid[1] - grid[0]) == pytest.approx(float(Decimal("60000") / Decimal("130")))


def test_single_grid_buy_matches_manual_case() -> None:
    result = simulate_grid_orders(
        config=_config(),
        klines=[
            _kline(0, open_price="60500", high="60500", low="60450", close="60480"),
        ],
    )

    p1 = result.grid_prices[1]
    notional = Decimal("500") / Decimal("130") * Decimal("10")
    event = result.events[0]
    replacement = next(order for order in result.orders if order.order_id == event.replacement_order_id)

    assert len(result.events) == 1
    assert event.side == "BUY"
    assert event.grid_level == 1
    assert event.price == p1
    assert event.quantity == notional / p1
    assert event.fee == notional * Decimal("0.001")
    assert replacement.side == "SELL"
    assert replacement.grid_level == 2
    assert replacement.status == "pending"


def test_buy_then_sell_creates_replacement_buy() -> None:
    result = simulate_grid_orders(
        config=_config(),
        klines=[
            _kline(0, open_price="60500", high="60500", low="60450", close="60480"),
            _kline(1, open_price="60480", high="61000", low="60480", close="61000"),
        ],
    )

    buy_event, sell_event = result.events
    replacement_buy = next(order for order in result.orders if order.order_id == sell_event.replacement_order_id)

    assert buy_event.side == "BUY"
    assert sell_event.side == "SELL"
    assert sell_event.grid_level == 2
    assert sell_event.price == result.grid_prices[2]
    assert sell_event.quantity == buy_event.quantity
    assert sell_event.fee == sell_event.price * sell_event.quantity * Decimal("0.001")
    assert replacement_buy.side == "BUY"
    assert replacement_buy.grid_level == 1
    assert replacement_buy.status == "pending"


def test_same_kline_can_cross_multiple_grids_in_path_order() -> None:
    result = simulate_grid_orders(
        config=_config(),
        klines=[
            _kline(0, open_price="62000", high="62000", low="59900", close="60000"),
        ],
    )

    assert [event.side for event in result.events] == ["BUY", "BUY", "BUY", "BUY", "BUY"]
    assert [event.grid_level for event in result.events] == [4, 3, 2, 1, 0]
    assert [event.price for event in result.events] == [
        result.grid_prices[4],
        result.grid_prices[3],
        result.grid_prices[2],
        result.grid_prices[1],
        result.grid_prices[0],
    ]


def test_same_order_is_not_filled_twice_but_replacement_can_fill() -> None:
    result = simulate_grid_orders(
        config=_config(),
        klines=[
            _kline(0, open_price="60500", high="61000", low="60450", close="60480"),
            _kline(1, open_price="60480", high="61000", low="60480", close="61000"),
            _kline(2, open_price="61000", high="61000", low="60450", close="60480"),
        ],
    )

    assert [event.side for event in result.events] == ["BUY", "SELL", "BUY"]
    assert len({event.order_id for event in result.events}) == 3
    assert result.events[0].order_id != result.events[2].order_id
    assert result.events[0].grid_level == result.events[2].grid_level


def test_rerun_is_deterministic() -> None:
    klines = [
        _kline(0, open_price="60500", high="61000", low="60450", close="60480"),
        _kline(1, open_price="60480", high="61000", low="60480", close="61000"),
        _kline(2, open_price="61000", high="61000", low="60450", close="60480"),
    ]

    first = simulate_grid_orders(config=_config(), klines=klines)
    second = simulate_grid_orders(config=_config(), klines=klines)

    assert first == second
