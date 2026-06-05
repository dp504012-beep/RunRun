# Liquidation Models

## simplified v1

Scope:

- Long only.
- Isolated-style fixed initial margin.
- Funding rate is fixed at `0`.
- Maintenance margin, liquidation fee, insurance fund rules, and Binance tiered brackets are not modeled.

Definitions:

- `entry_price`: first kline open price.
- `initial_margin`: strategy initial capital.
- `leverage`: configured fixed leverage.
- `notional_position = initial_margin * leverage`.
- `quantity = notional_position / entry_price`.
- `open_fee = notional_position * fee_rate`.
- `equity_after_open = initial_margin - open_fee`.

Long equity at price `p`:

```text
equity(p) = equity_after_open + (p - entry_price) * quantity
```

The simplified liquidation price is the price where equity becomes `0`:

```text
liquidation_price = entry_price - (equity_after_open / quantity)
```

Equivalent form:

```text
liquidation_price = entry_price * (1 - (1 - leverage * fee_rate) / leverage)
```

During backtest, a long position is considered liquidated when a kline `low`
is less than or equal to `liquidation_price`. The liquidation timestamp is the
kline `open_time` because intrabar event time is not available from OHLCV data.

