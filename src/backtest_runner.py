from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
import hashlib

from src.backtest_config import BacktestConfig, BacktestResult, StrategyConfig, load_backtest_config
from src.data.binance_client import Kline, download_spot_klines
from src.data.parquet_store import update_parquet_store
from src.data.quality import query_backtest_klines
from src.exceptions import DataValidationError, MarketDataError
from src.outputs.local_report import LocalReportPaths, generate_local_report
from src.strategies.buy_and_hold import run_btc_buy_and_hold
from src.strategies.dca import run_dca_long
from src.strategies.fixed_leverage_long import run_fixed_leverage_long
from src.strategies.long_grid import run_long_grid


StrategyRunner = Callable[
    [BacktestConfig, StrategyConfig, list[Kline], datetime],
    BacktestResult,
]


STRATEGY_RUNNERS: dict[str, StrategyRunner] = {
    "btc_buy_and_hold": lambda config, strategy, klines, finished_at: run_btc_buy_and_hold(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=finished_at,
    ),
    "fixed_leverage_long": lambda config, strategy, klines, finished_at: run_fixed_leverage_long(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=finished_at,
    ),
    "long_grid": lambda config, strategy, klines, finished_at: run_long_grid(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=finished_at,
    ),
    "dca_long": lambda config, strategy, klines, finished_at: run_dca_long(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=finished_at,
    ),
}


@dataclass(slots=True)
class BacktestRunExecution:
    config: BacktestConfig
    run_id: str
    results: list[BacktestResult]
    warnings: list[str]
    report_paths: LocalReportPaths | None

    @property
    def failed_strategy_count(self) -> int:
        return sum(1 for result in self.results if result.metrics.get("status") == "failed")


def execute_backtest_run(
    config_path: str | Path,
    *,
    data_root: str | Path = Path("data/parquet"),
    output_dir: str | Path = Path("reports"),
    fetch_klines: Callable[..., list[Kline]] = download_spot_klines,
    created_at: datetime | None = None,
    run_id: str | None = None,
) -> BacktestRunExecution:
    config = load_backtest_config(config_path)
    created_at = _ensure_utc(created_at or datetime.now(UTC))
    run_id = run_id or created_at.strftime("%Y%m%dT%H%M%S%fZ")
    warnings: list[str] = []

    _update_or_warn(config=config, data_root=data_root, fetch_klines=fetch_klines, warnings=warnings)
    query = query_backtest_klines(
        root=data_root,
        symbol=config.market.symbol,
        interval=config.market.interval,
        start=config.market.start,
        end=config.market.end,
    )
    if query.report.errors:
        first = query.report.errors[0]
        raise DataValidationError(f"{first.code}: {first.message}")

    warnings.extend(f"data_warning:{issue.code}:{issue.message}" for issue in query.report.warnings)
    data_snapshot = _data_snapshot(config, query.klines)
    results = _run_strategies(config=config, klines=query.klines, finished_at=created_at, data_snapshot=data_snapshot)

    report_paths = None
    if config.output.local_report:
        report_paths = generate_local_report(
            config=config,
            results=results,
            output_dir=output_dir,
            run_id=run_id,
            warnings=warnings,
            created_at=created_at,
        )

    return BacktestRunExecution(
        config=config,
        run_id=run_id,
        results=results,
        warnings=warnings,
        report_paths=report_paths,
    )


def _update_or_warn(
    *,
    config: BacktestConfig,
    data_root: str | Path,
    fetch_klines: Callable[..., list[Kline]],
    warnings: list[str],
) -> None:
    if config.market.source != "binance":
        warnings.append(f"data_update_skipped: unsupported source {config.market.source}")
        return

    try:
        update_parquet_store(
            root=data_root,
            source=config.market.source,
            symbol=config.market.symbol,
            interval=config.market.interval,
            start_time=config.market.start,
            end_time=config.market.end,
            fetch_klines=fetch_klines,
        )
    except MarketDataError as exc:
        warnings.append(f"data_update_failed: {exc}")


def _run_strategies(
    *,
    config: BacktestConfig,
    klines: list[Kline],
    finished_at: datetime,
    data_snapshot: dict[str, object],
) -> list[BacktestResult]:
    results: list[BacktestResult] = []
    for strategy in config.strategies:
        try:
            runner = STRATEGY_RUNNERS[strategy.type]
            result = runner(config, strategy, klines, finished_at)
            result.metrics.update(
                {
                    "status": "success",
                    "strategy_id": strategy.id,
                    "data_snapshot": data_snapshot,
                }
            )
        except Exception as exc:  # noqa: BLE001
            result = _failed_result(
                config=config,
                strategy=strategy,
                finished_at=finished_at,
                error=f"{type(exc).__name__}: {exc}",
                data_snapshot=data_snapshot,
            )
        results.append(result)
    return results


def _failed_result(
    *,
    config: BacktestConfig,
    strategy: StrategyConfig,
    finished_at: datetime,
    error: str,
    data_snapshot: dict[str, object],
) -> BacktestResult:
    return BacktestResult(
        config=config,
        started_at=config.market.start,
        finished_at=finished_at,
        initial_capital=strategy.initial_capital,
        final_equity=None,
        total_return=None,
        max_drawdown=None,
        metrics={
            "status": "failed",
            "strategy_id": strategy.id,
            "strategy_type": strategy.type,
            "error": error,
            "data_snapshot": data_snapshot,
            "assumptions": [],
        },
    )


def _data_snapshot(config: BacktestConfig, klines: list[Kline]) -> dict[str, object]:
    return {
        "symbol": config.market.symbol,
        "interval": config.market.interval,
        "requested_start": _iso(config.market.start),
        "requested_end": _iso(config.market.end),
        "first_open_time": _iso(klines[0].open_time) if klines else None,
        "last_open_time": _iso(klines[-1].open_time) if klines else None,
        "row_count": len(klines),
        "fingerprint": _fingerprint(klines),
    }


def _fingerprint(klines: list[Kline]) -> str:
    digest = hashlib.sha256()
    for kline in klines:
        digest.update(
            "|".join(
                [
                    kline.symbol,
                    kline.interval,
                    _iso(kline.open_time),
                    str(kline.open),
                    str(kline.high),
                    str(kline.low),
                    str(kline.close),
                    str(kline.volume),
                ]
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
