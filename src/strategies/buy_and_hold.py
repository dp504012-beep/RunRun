from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from src.backtest_config import BacktestConfig, BacktestResult, EquityPoint, StrategyConfig, TradeRecord
from src.data.binance_client import Kline
from src.exceptions import DataValidationError
from src.metrics.performance import compute_performance_summary

STRATEGY_TYPE = "btc_buy_and_hold"


def run_btc_buy_and_hold(
    *,
    config: BacktestConfig,
    strategy: StrategyConfig,
    klines: list[Kline],
    finished_at: datetime | None = None,
) -> BacktestResult:
    if strategy.type != STRATEGY_TYPE:
        raise DataValidationError(f"策略類型不符: {strategy.type}")
    if not klines:
        raise DataValidationError("BTC 買入持有策略需要至少一根 K 線。")

    sorted_klines = sorted(klines, key=lambda item: item.open_time)
    if [item.open_time for item in klines] != [item.open_time for item in sorted_klines]:
        raise DataValidationError("K 線必須按 open_time 排序。")

    initial_capital = Decimal(str(strategy.initial_capital))
    fee_rate = Decimal(str(config.common.fee_rate))
    if initial_capital <= 0:
        raise DataValidationError("initial_capital 必須大於 0。")
    if fee_rate < 0:
        raise DataValidationError("fee_rate 不可小於 0。")

    first_kline = sorted_klines[0]
    if first_kline.open <= 0:
        raise DataValidationError("第一根 K 線 open 必須大於 0。")

    buy_fee = initial_capital * fee_rate
    invested_quote = initial_capital - buy_fee
    btc_quantity = invested_quote / first_kline.open

    equity_curve = [
        EquityPoint(timestamp=kline.open_time, equity=float(btc_quantity * kline.close))
        for kline in sorted_klines
    ]
    trade = TradeRecord(
        timestamp=first_kline.open_time,
        strategy_id=strategy.id,
        symbol=config.market.symbol,
        side="BUY",
        quantity=float(btc_quantity),
        price=float(first_kline.open),
        fee=float(buy_fee),
        metadata={
            "strategy_type": STRATEGY_TYPE,
            "fee_rate": float(fee_rate),
            "quote_spent": float(initial_capital),
            "invested_quote_after_fee": float(invested_quote),
        },
    )

    performance = compute_performance_summary(
        timestamps=[point.timestamp for point in equity_curve],
        equity_curve=[point.equity for point in equity_curve],
        initial_capital=float(initial_capital),
        trades=[trade],
        risk_events=[],
    )
    final_equity = Decimal(str(equity_curve[-1].equity))
    include_exit_fee = bool(strategy.parameters.get("include_exit_fee", False))
    estimated_exit_fee = final_equity * fee_rate if include_exit_fee else Decimal("0")
    estimated_exit_net_equity = final_equity - estimated_exit_fee if include_exit_fee else None

    return BacktestResult(
        config=config,
        started_at=sorted_klines[0].open_time,
        finished_at=finished_at or datetime.now(UTC),
        initial_capital=float(initial_capital),
        final_equity=performance.final_equity,
        total_return=performance.total_return,
        max_drawdown=performance.max_drawdown,
        trades=[trade],
        risk_events=[],
        equity_curve=equity_curve,
        metrics={
            "strategy_type": STRATEGY_TYPE,
            "btc_quantity": float(btc_quantity),
            "buy_fee": float(buy_fee),
            "total_fees": performance.total_fees,
            "include_exit_fee": include_exit_fee,
            "estimated_exit_fee": float(estimated_exit_fee),
            "estimated_exit_net_equity": float(estimated_exit_net_equity) if estimated_exit_net_equity is not None else None,
            "performance_summary": asdict(performance),
            "assumptions": [
                "第一根 K 線開盤價一次性買入。",
                "買入手續費先從初始報價本金扣除，再計算 BTC 數量。",
                "買入後不再進行中途交易。",
                "每根 K 線以 close 價格估算權益。",
                "最後一根 K 線只估值，不預設賣出。",
            ],
        },
    )
