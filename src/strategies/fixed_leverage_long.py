from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from src.backtest_config import BacktestConfig, BacktestResult, EquityPoint, RiskEvent, StrategyConfig, TradeRecord
from src.data.binance_client import Kline
from src.exceptions import DataValidationError
from src.metrics.performance import PerformanceSummary, compute_performance_summary

STRATEGY_TYPE = "fixed_leverage_long"
LIQUIDATION_MODEL = "simplified"
LIQUIDATION_MODEL_VERSION = "simplified_v1"


def run_fixed_leverage_long(
    *,
    config: BacktestConfig,
    strategy: StrategyConfig,
    klines: list[Kline],
    finished_at: datetime | None = None,
) -> BacktestResult:
    if strategy.type != STRATEGY_TYPE:
        raise DataValidationError(f"策略類型不符: {strategy.type}")
    if not klines:
        raise DataValidationError("固定槓桿做多策略需要至少一根 K 線。")

    sorted_klines = sorted(klines, key=lambda item: item.open_time)
    if [item.open_time for item in klines] != [item.open_time for item in sorted_klines]:
        raise DataValidationError("K 線必須按 open_time 排序。")

    leverage = _parameter_decimal(strategy, "leverage")
    liquidation_model = str(strategy.parameters.get("liquidation_model", LIQUIDATION_MODEL))
    if liquidation_model != LIQUIDATION_MODEL:
        raise DataValidationError(f"不支援的清算模型: {liquidation_model}")
    if leverage <= 1:
        raise DataValidationError("leverage 必須大於 1。")

    initial_margin = Decimal(str(strategy.initial_capital))
    fee_rate = Decimal(str(config.common.fee_rate))
    if initial_margin <= 0:
        raise DataValidationError("initial_capital 必須大於 0。")
    if fee_rate < 0:
        raise DataValidationError("fee_rate 不可小於 0。")

    first_kline = sorted_klines[0]
    if first_kline.open <= 0:
        raise DataValidationError("第一根 K 線 open 必須大於 0。")

    entry_price = first_kline.open
    notional_position = initial_margin * leverage
    quantity = notional_position / entry_price
    open_fee = notional_position * fee_rate
    equity_after_open = initial_margin - open_fee
    liquidation_price = _simplified_long_liquidation_price(
        entry_price=entry_price,
        equity_after_open=equity_after_open,
        quantity=quantity,
    )

    equity_curve: list[EquityPoint] = []
    risk_events: list[RiskEvent] = []
    liquidated = False
    liquidation_time: datetime | None = None

    for kline in sorted_klines:
        if kline.low <= liquidation_price:
            liquidated = True
            liquidation_time = kline.open_time
            equity_curve.append(EquityPoint(timestamp=kline.open_time, equity=0.0))
            risk_events.append(
                RiskEvent(
                    timestamp=kline.open_time,
                    strategy_id=strategy.id,
                    event_type="liquidation",
                    severity="liquidation",
                    message="Simplified liquidation price touched by kline low.",
                    details={
                        "liquidation_model": LIQUIDATION_MODEL,
                        "liquidation_model_version": LIQUIDATION_MODEL_VERSION,
                        "liquidation_price": float(liquidation_price),
                        "kline_low": float(kline.low),
                    },
                )
            )
            break

        equity = equity_after_open + (kline.close - entry_price) * quantity
        equity_curve.append(EquityPoint(timestamp=kline.open_time, equity=float(equity)))

    trade = TradeRecord(
        timestamp=first_kline.open_time,
        strategy_id=strategy.id,
        symbol=config.market.symbol,
        side="LONG",
        quantity=float(quantity),
        price=float(entry_price),
        fee=float(open_fee),
        metadata={
            "strategy_type": STRATEGY_TYPE,
            "fee_rate": float(fee_rate),
            "leverage": float(leverage),
            "initial_margin": float(initial_margin),
            "notional_position": float(notional_position),
            "funding_rate": 0.0,
        },
    )

    performance = _compute_performance(
        equity_curve=equity_curve,
        initial_margin=float(initial_margin),
        trades=[trade],
        risk_events=risk_events,
    )

    return BacktestResult(
        config=config,
        started_at=sorted_klines[0].open_time,
        finished_at=finished_at or datetime.now(UTC),
        initial_capital=float(initial_margin),
        final_equity=performance.final_equity,
        total_return=performance.total_return,
        max_drawdown=performance.max_drawdown,
        trades=[trade],
        risk_events=risk_events,
        equity_curve=equity_curve,
        metrics={
            "strategy_type": STRATEGY_TYPE,
            "leverage": float(leverage),
            "initial_margin": float(initial_margin),
            "notional_position": float(notional_position),
            "entry_price": float(entry_price),
            "quantity": float(quantity),
            "liquidation_model": LIQUIDATION_MODEL,
            "liquidation_model_version": LIQUIDATION_MODEL_VERSION,
            "liquidation_price": float(liquidation_price),
            "liquidated": liquidated,
            "liquidation_time": liquidation_time.isoformat().replace("+00:00", "Z") if liquidation_time else None,
            "minimum_equity": performance.minimum_equity,
            "open_fee": float(open_fee),
            "total_fees": performance.total_fees,
            "funding_rate": 0.0,
            "performance_summary": asdict(performance),
            "assumptions": [
                "第一根 K 線開盤價建立固定槓桿多單。",
                "名義部位等於初始保證金乘以 leverage。",
                "開倉手續費從初始保證金扣除。",
                "每根 K 線以 close 更新未實現損益與權益。",
                "使用 high/low 判斷盤中是否觸及 simplified 清算價。",
                "清算後策略終止，後續 K 線不再產生收益。",
                "第一版 funding rate 固定為 0。",
            ],
        },
    )


def _compute_performance(
    *,
    equity_curve: list[EquityPoint],
    initial_margin: float,
    trades: list[TradeRecord],
    risk_events: list[RiskEvent],
) -> PerformanceSummary:
    if len(equity_curve) == 1:
        point = equity_curve[0]
        extra_point = EquityPoint(timestamp=point.timestamp.replace(microsecond=1), equity=point.equity)
        equity_curve = [point, extra_point]
    return compute_performance_summary(
        timestamps=[point.timestamp for point in equity_curve],
        equity_curve=[point.equity for point in equity_curve],
        initial_capital=initial_margin,
        trades=trades,
        risk_events=risk_events,
    )


def _simplified_long_liquidation_price(
    *,
    entry_price: Decimal,
    equity_after_open: Decimal,
    quantity: Decimal,
) -> Decimal:
    return entry_price - (equity_after_open / quantity)


def _parameter_decimal(strategy: StrategyConfig, name: str) -> Decimal:
    if name not in strategy.parameters:
        raise DataValidationError(f"缺少策略參數: {name}")
    value = Decimal(str(strategy.parameters[name]))
    return value
