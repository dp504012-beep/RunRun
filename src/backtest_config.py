from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo
import os
import re

import yaml

from src.exceptions import ConfigurationError

ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")
SUPPORTED_STRATEGY_TYPES = {"template", "btc_buy_and_hold", "fixed_leverage_long", "long_grid"}


@dataclass(slots=True)
class MarketConfig:
    source: str
    symbol: str
    interval: str
    start: datetime
    end: datetime


@dataclass(slots=True)
class CommonConfig:
    timezone: str = "UTC"
    fee_rate: float = 0.0
    slippage_rate: float = 0.0
    quote_currency: str = "USDT"


@dataclass(slots=True)
class StrategyConfig:
    id: str
    type: str
    initial_capital: float
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OutputConfig:
    local_report: bool
    google_sheet_id: str | None
    equity_curve_sampling: int
    write_trade_log: bool
    create_charts: bool


@dataclass(slots=True)
class BacktestConfig:
    market: MarketConfig
    common: CommonConfig
    strategies: list[StrategyConfig]
    output: OutputConfig


@dataclass(slots=True)
class TradeRecord:
    timestamp: datetime
    strategy_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    pnl: float = 0.0
    notes: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskEvent:
    timestamp: datetime
    strategy_id: str
    event_type: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    started_at: datetime
    finished_at: datetime | None
    initial_capital: float
    final_equity: float | None
    total_return: float | None
    max_drawdown: float | None
    trades: list[TradeRecord] = field(default_factory=list)
    risk_events: list[RiskEvent] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def load_backtest_config(path: str | Path) -> BacktestConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ConfigurationError("設定檔必須是 YAML mapping。")
    return parse_backtest_config(_expand_env(data))


def parse_backtest_config(data: Mapping[str, Any]) -> BacktestConfig:
    common = _parse_common(data.get("common"))
    market = _parse_market(data.get("market"), common.timezone)
    strategies = _parse_strategies(data.get("strategies"))
    output = _parse_output(data.get("output"))
    return BacktestConfig(market=market, common=common, strategies=strategies, output=output)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            env_name = match.group(1)
            if env_name not in os.environ:
                raise ConfigurationError(f"缺少環境變數: {env_name}")
            return os.environ[env_name]

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _parse_market(raw: Any, timezone_name: str) -> MarketConfig:
    data = _require_mapping(raw, "market")
    source = _require_str(data.get("source"), "market.source")
    symbol = _require_str(data.get("symbol"), "market.symbol")
    interval = _require_str(data.get("interval"), "market.interval")
    start = _parse_datetime(data.get("start"), "market.start", timezone_name)
    end = _parse_datetime(data.get("end"), "market.end", timezone_name)
    if start >= end:
        raise ConfigurationError("market.start 必須早於 market.end。")
    return MarketConfig(source=source, symbol=symbol, interval=interval, start=start, end=end)


def _parse_common(raw: Any) -> CommonConfig:
    data = _require_mapping(raw, "common")
    timezone_name = _require_str(data.get("timezone", "UTC"), "common.timezone")
    _resolve_timezone_name(timezone_name)
    fee_rate = _require_number(data.get("fee_rate"), "common.fee_rate")
    slippage_rate = _require_number(data.get("slippage_rate"), "common.slippage_rate")
    if fee_rate < 0:
        raise ConfigurationError("common.fee_rate 不可小於 0。")
    if slippage_rate < 0:
        raise ConfigurationError("common.slippage_rate 不可小於 0。")
    quote_currency = _require_str(data.get("quote_currency"), "common.quote_currency")
    return CommonConfig(
        timezone=timezone_name,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
        quote_currency=quote_currency,
    )


def _parse_strategies(raw: Any) -> list[StrategyConfig]:
    if raw is None:
        raise ConfigurationError("strategies 不可為空。")
    if not isinstance(raw, list):
        raise ConfigurationError("strategies 必須是清單。")

    strategies: list[StrategyConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        data = _require_mapping(item, f"strategies[{index}]")
        strategy_id = _require_str(data.get("id"), f"strategies[{index}].id")
        if strategy_id in seen_ids:
            raise ConfigurationError(f"策略 id 重複: {strategy_id}")
        seen_ids.add(strategy_id)

        strategy_type = _require_str(data.get("type"), f"strategies[{index}].type")
        if strategy_type not in SUPPORTED_STRATEGY_TYPES:
            raise ConfigurationError(f"未知策略類型: {strategy_type}")

        initial_capital = _require_number(data.get("initial_capital"), f"strategies[{index}].initial_capital")
        if initial_capital <= 0:
            raise ConfigurationError(f"strategies[{index}].initial_capital 必須大於 0。")

        parameters = data.get("parameters", {})
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, dict):
            raise ConfigurationError(f"strategies[{index}].parameters 必須是 mapping。")

        strategies.append(
            StrategyConfig(
                id=strategy_id,
                type=strategy_type,
                initial_capital=initial_capital,
                parameters=parameters,
            )
        )

    return strategies


def _parse_output(raw: Any) -> OutputConfig:
    data = _require_mapping(raw, "output")
    local_report = _require_bool(data.get("local_report"), "output.local_report")
    google_sheet_id = data.get("google_sheet_id")
    if google_sheet_id is not None and not isinstance(google_sheet_id, str):
        raise ConfigurationError("output.google_sheet_id 必須是字串或空值。")
    equity_curve_sampling = _require_int(data.get("equity_curve_sampling"), "output.equity_curve_sampling")
    if equity_curve_sampling <= 0:
        raise ConfigurationError("output.equity_curve_sampling 必須大於 0。")
    write_trade_log = _require_bool(data.get("write_trade_log"), "output.write_trade_log")
    create_charts = _require_bool(data.get("create_charts"), "output.create_charts")
    return OutputConfig(
        local_report=local_report,
        google_sheet_id=google_sheet_id,
        equity_curve_sampling=equity_curve_sampling,
        write_trade_log=write_trade_log,
        create_charts=create_charts,
    )


def _parse_datetime(value: Any, field_name: str, timezone_name: str) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationError(f"{field_name} 必須是 ISO 8601 字串。")

    parsed: datetime
    if "T" in value or " " in value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.combine(date.fromisoformat(value), dt_time.min)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_resolve_timezone_name(timezone_name))
    return parsed


def _resolve_timezone_name(timezone_name: str) -> tzinfo:
    if timezone_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(f"無效時區: {timezone_name}") from exc


def _require_mapping(raw: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigurationError(f"{field_name} 必須是 mapping。")
    return raw


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field_name} 必須是非空字串。")
    return value


def _require_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field_name} 必須是數值。")
    return float(value)


def _require_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{field_name} 必須是整數。")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field_name} 必須是布林值。")
    return value
