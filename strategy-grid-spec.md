# BTCUSDT 10x Long Grid Spec

Version: `long-grid-v1`
Liquidation model: `simplified v1` from [docs/liquidation_models.md](docs/liquidation_models.md)

## 1. Fixed Parameters

- Symbol: `BTCUSDT`
- Timeframe: `1h`
- Direction: long only
- Initial margin: `500 USDT`
- Leverage: `10x`
- Grid lower bound: `60,000`
- Grid upper bound: `120,000`
- Grid count: `130`
- Grid type: arithmetic
- Funding rate: `0`
- Fees: from config

## 2. Grid Definition

1. `130` means `130 intervals`, not `130 price points`.
2. There are `131` grid price points: `P0..P130`.
3. `P0 = 60000`, `P130 = 120000`.
4. `step = (120000 - 60000) / 130 = 461.53846153846155`.
5. `Pi = 60000 + i * step`, where `i in [0, 130]`.
6. Interval `Gi` means the band between `Pi` and `P(i+1)`.
7. Each interval receives equal margin budget:
   - `margin_per_grid = 500 / 130 = 3.8461538461538463 USDT`.
8. Each filled buy uses notional:
   - `notional_per_lot = margin_per_grid * leverage = 38.46153846153846 USDT`.
9. Each lot quantity:
   - `qty = notional_per_lot / fill_price`.

## 3. Required Rules

1. `130` is `130 intervals`.
2. Price spacing is fixed arithmetic step `461.53846153846155`.
3. Each grid uses equal margin budget, not equal notional.
4. If the first kline open is inside the range, the strategy starts flat and arms only buy orders below the open price.
5. Initial orders:
   - all buy orders at grid points strictly below the first open price are armed;
   - no sell order exists until a buy fill opens that lot.
6. Buy trigger:
   - when the assumed intrabar path crosses a grid point downward, the buy at that grid point fills at that exact grid price.
7. Sell trigger:
   - after a lot is open, its take-profit sell is the next higher grid point.
8. Rebuild after fill:
   - buy fill opens one lot and arms the next sell;
   - sell fill closes the lot and immediately re-arms the same interval's buy for the next revisit.
9. Multi-grid candle processing:
   - the path is traversed in price order;
   - crossed levels are filled one by one in the order the path touches them.
10. Intrabar path assumption:
   - green candle (`close >= open`): `open -> low -> high -> close`
   - red candle (`close < open`): `open -> high -> low -> close`
11. Above upper bound:
   - no buy orders are armed above `P130`;
   - existing open lots may still sell up to `P130`.
12. Below lower bound:
   - no new buys are armed below `P0`;
   - open lots remain exposed to liquidation.
13. Position and margin math:
   - each lot is isolated to its interval margin budget;
   - on buy: lock `margin_per_grid`, deduct open fee, open one lot;
   - on sell: release `margin_per_grid`, realize PnL, deduct close fee.
14. Liquidation:
   - each open lot uses the `simplified v1` liquidation price from the docs;
   - if any open lot touches liquidation during the path, the whole strategy stops immediately;
   - equity after liquidation is `0`.
15. Fees:
   - buy fee = `buy_notional * fee_rate`
   - sell fee = `sell_notional * fee_rate`
   - funding = `0`
16. Insufficient funds:
   - a buy fill requires enough free cash to pay the open fee;
   - if the fee cannot be paid, that fill is skipped and the order remains unfilled;
   - sell fills never fail due to cash shortage because the sell itself creates proceeds.

## 4. Output Fields

The strategy result must expose all of these fields:

- `strategy_id`
- `strategy_type`
- `spec_version`
- `liquidation_model`
- `liquidation_model_version`
- `initial_margin`
- `leverage`
- `grid_lower_bound`
- `grid_upper_bound`
- `grid_count`
- `grid_step`
- `margin_per_grid`
- `notional_per_lot`
- `entry_price`
- `liquidation_price`
- `btc_quantity`
- `equity_curve`
- `cash_available_curve`
- `margin_locked_curve`
- `realized_pnl_curve`
- `unrealized_pnl_curve`
- `fee_curve`
- `order_events`
- `trade_records`
- `liquidated`
- `liquidation_time`
- `liquidation_reason`
- `minimum_equity`
- `performance_summary`
- `assumptions`

## 5. State Machine

Per lot states:

- `ARMED_BUY`
- `OPEN_LONG`
- `ARMED_SELL`
- `CLOSED`
- `LIQUIDATED`

Account states:

- `RUNNING`
- `HALTED_BY_LIQUIDATION`

Transitions:

1. `ARMED_BUY -> OPEN_LONG` when price crosses the buy grid level downward.
2. `OPEN_LONG -> ARMED_SELL` immediately after buy fill.
3. `ARMED_SELL -> CLOSED` when price crosses the next higher grid level upward.
4. `OPEN_LONG -> LIQUIDATED` when candle low touches the lot liquidation price.
5. `LIQUIDATED -> HALTED_BY_LIQUIDATION` ends the whole strategy.

## 6. Pseudocode

```text
setup:
  read initial margin, leverage, fee_rate
  compute grid points P0..P130
  margin_per_grid = initial_margin / 130
  notional_per_lot = margin_per_grid * leverage
  start with no open lots
  arm buy orders only below the first candle open price

for each candle:
  path = green ? [open, low, high, close] : [open, high, low, close]

  for each consecutive segment on the path:
    walk through crossed prices in exact order

    if moving downward:
      execute any armed buys at crossed grid points
      for each new open lot, check liquidation if the segment continues below its liquidation price

    if moving upward:
      execute any armed sells at crossed next-higher grid points

    after each fill:
      update cash_available, margin_locked, realized_pnl, fees, equity
      re-arm the opposite side for that same lot interval

    if any open lot is liquidated:
      set equity = 0
      stop processing all later candles
```

## 7. Worked Examples

Assume `fee_rate = 0.1%` in all examples. Rounded values are shown to 8 decimals. The exact formulas are always the source of truth.

### Case A: 5 candles, one full buy-sell cycle, then one repeat cycle

Derived values for the first lot:

- `qty = 38.46153846 / 60000 = 0.00064102564`
- `buy_fee = 38.46153846 * 0.001 = 0.03846154`
- `sell_fee at 60461.53846154 = 38.75739645 * 0.001 = 0.03875740`
- `one full cycle net profit = 0.21863905`

| Candle | OHLC | Path | Event | Open lots | Cash available | Margin locked | Equity | Fees |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | O=60000 H=60461.54 L=60000 C=60461.54 | green | Buy at 60000, then sell at 60461.54 | 0 | 500.21863905 | 0 | 500.21863905 | 0.07721893 |
| 2 | O=60461.54 H=60923.08 L=60461.54 C=60923.08 | green | No fill | 0 | 500.21863905 | 0 | 500.21863905 | 0 |
| 3 | O=60923.08 H=60923.08 L=60000 C=60000 | red | Buy at 60000 | 1 | 496.11538462 | 3.84615385 | 499.96153846 | 0.03846154 |
| 4 | O=60000 H=60461.54 L=60000 C=60461.54 | green | Sell at 60461.54 | 0 | 500.18017751 | 0 | 500.18017751 | 0.03875740 |
| 5 | O=60461.54 H=60923.08 L=60461.54 C=60923.08 | green | No fill | 0 | 500.18017751 | 0 | 500.18017751 | 0 |

Interpretation:

- Candle 3 re-arms the same interval after Candle 1 closed it.
- The final equity equals the sum of the two completed cycle profits on top of the initial 500 USDT.

### Case B: 5 candles, downside move, no liquidation

Start at `60000`, so the first buy is at `60000`.

| Candle | OHLC | Path | Event | Open lots | Cash available | Margin locked | Equity | Fees |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | O=60000 H=60050 L=59500 C=59500 | red | Buy at 60000 | 1 | 496.11538462 | 3.84615385 | 499.64102564 | 0.03846154 |
| 2 | O=59500 H=59600 L=59400 C=59400 | red | No fill | 1 | 496.11538462 | 3.84615385 | 499.57692308 | 0 |
| 3 | O=59400 H=59500 L=59200 C=59200 | red | No fill | 1 | 496.11538462 | 3.84615385 | 499.44871795 | 0 |
| 4 | O=59200 H=59600 L=59100 C=59550 | green | No fill | 1 | 496.11538462 | 3.84615385 | 499.67307692 | 0 |
| 5 | O=59550 H=59650 L=59300 C=59300 | red | No fill | 1 | 496.11538462 | 3.84615385 | 499.51282051 | 0 |

Interpretation:

- The lot stays open the whole time.
- The lowest equity is above zero, so no liquidation occurs.
- The final state is one open lot, unrealized loss only.

### Case C: 5 candles, intrabar liquidation

Start at `60000`.

| Candle | OHLC | Path | Event | Open lots | Cash available | Margin locked | Equity | Fees |
|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | O=60000 H=60050 L=54000 C=55000 | red | Buy at 60000, then liquidation is hit on the same candle | 0 | 0 | 0 | 0 | 0.03846154 |
| 2 | O=55000 H=56000 L=53000 C=54000 | red | Ignored, strategy halted | 0 | 0 | 0 | 0 | 0 |
| 3 | O=54000 H=54500 L=52000 C=53000 | red | Ignored, strategy halted | 0 | 0 | 0 | 0 | 0 |
| 4 | O=53000 H=53500 L=51500 C=52000 | red | Ignored, strategy halted | 0 | 0 | 0 | 0 | 0 |
| 5 | O=52000 H=52500 L=50000 C=51000 | red | Ignored, strategy halted | 0 | 0 | 0 | 0 | 0 |

Liquidation details:

- Entry at `60000`.
- `liquidation_price = 54060.0` under `simplified v1`.
- Candle 1 low `54000 <= 54060`, so liquidation is triggered.
- After liquidation, equity is forced to `0` and no later candle is processed.

## 8. Outstanding Questions

These are intentionally postponed to later versions:

- Whether to support `binance_tiered` liquidation pricing in addition to `simplified`.
- Whether to model maker/taker fee differences.
- Whether to model slippage or tick-size rounding at order placement.
- Whether to support partial fills.
- Whether to support simultaneous long/short hedging. This spec does not.

## 9. Version Rule

Any later engine must keep:

- the `130 intervals` interpretation,
- the path assumption,
- the per-lot isolated margin model,
- the liquidation precedence,
- and the output field names above

unless a new spec version explicitly replaces them.
