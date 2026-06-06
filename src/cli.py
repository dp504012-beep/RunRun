from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.backtest_runner import execute_backtest_run
from src.data.quality import DataIssue, DataQualityReport, QueryResult, query_backtest_klines
from src.data.binance_client import INTERVAL_TO_MS, download_spot_klines
from src.data.parquet_store import ParquetKlineStore, update_parquet_store
from src.exceptions import BacktesterError, ConfigurationError
from src.logging_config import configure_logging

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UpdateDataResult:
    symbol: str
    interval: str
    requested_start: datetime
    requested_end: datetime
    actual_start: str | None
    actual_end: str | None
    added_rows: int
    total_rows: int
    parquet_path: Path
    metadata_path: Path
    mode: str


@dataclass(slots=True)
class ValidateDataResult:
    symbol: str
    interval: str
    parquet_path: Path
    metadata_path: Path
    requested_start: datetime
    requested_end: datetime
    actual_start: str | None
    actual_end: str | None
    row_count: int
    issue_count: int
    error_count: int
    warning_count: int
    info_count: int
    issues: list[DataIssue]
    status: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crypto-backtester")
    subparsers = parser.add_subparsers(dest="command")

    update_data_parser = subparsers.add_parser("update-data", help="download or increment local market data")
    update_data_parser.add_argument("--symbol", required=True, type=_parse_symbol)
    update_data_parser.add_argument("--interval", required=True, type=_parse_interval)
    update_data_parser.add_argument("--start", required=True, type=_parse_utc_datetime)
    update_data_parser.add_argument("--end", required=True, type=_parse_utc_datetime)
    update_data_parser.add_argument("--data-root", default=Path("data/parquet"), type=Path)

    validate_data_parser = subparsers.add_parser("validate-data", help="validate local market data")
    validate_data_parser.add_argument("--symbol", required=True, type=_parse_symbol)
    validate_data_parser.add_argument("--interval", required=True, type=_parse_interval)
    validate_data_parser.add_argument("--start", required=True, type=_parse_utc_datetime)
    validate_data_parser.add_argument("--end", required=True, type=_parse_utc_datetime)
    validate_data_parser.add_argument("--data-root", default=Path("data/parquet"), type=Path)

    for command in ("export-sheets",):
        subparsers.add_parser(command, help="not implemented")

    run_parser = subparsers.add_parser("run", help="run a backtest config")
    run_parser.add_argument("config_path", nargs="?", default="config/backtest.yaml")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "update-data":
        return _handle_update_data(args)

    if args.command == "validate-data":
        return _handle_validate_data(args)

    if args.command == "run":
        try:
            execution = execute_backtest_run(args.config_path)
        except BacktesterError as exc:
            logger.error("run failed: %s", exc)
            return 1

        if execution.report_paths is not None:
            logger.info("report_dir=%s", execution.report_paths.report_dir)
        if execution.failed_strategy_count:
            logger.error("failed_strategies=%s", execution.failed_strategy_count)
            return 1
        return 0

    return _handle_command(args.command)


def _handle_command(command: str) -> int:
    logger.info("%s not implemented", command)
    return 0


def _handle_update_data(args: argparse.Namespace) -> int:
    try:
        result = execute_update_data(
            symbol=args.symbol,
            interval=args.interval,
            start=args.start,
            end=args.end,
            data_root=args.data_root,
        )
    except BacktesterError as exc:
        logger.error("update-data failed: %s", exc)
        print(f"update-data failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        logger.error("update-data failed: %s", exc)
        print(f"update-data failed: {exc}", file=sys.stderr)
        return 1

    _print_update_data_result(result)
    return 0


def _handle_validate_data(args: argparse.Namespace) -> int:
    try:
        result = execute_validate_data(
            symbol=args.symbol,
            interval=args.interval,
            start=args.start,
            end=args.end,
            data_root=args.data_root,
        )
    except BacktesterError as exc:
        logger.error("validate-data failed: %s", exc)
        print(f"validate-data failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        logger.error("validate-data failed: %s", exc)
        print(f"validate-data failed: {exc}", file=sys.stderr)
        return 1

    _print_validate_data_result(result)
    return 0 if result.error_count == 0 else 1


def execute_update_data(
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    data_root: str | Path = Path("data/parquet"),
    fetch_klines=None,
    updater=None,
) -> UpdateDataResult:
    if start >= end:
        raise ConfigurationError("start must be earlier than end")
    fetch_klines = fetch_klines or download_spot_klines
    updater = updater or update_parquet_store
    store = ParquetKlineStore(data_root)
    before = store.load_metadata(symbol, interval)
    metadata = updater(
        root=data_root,
        source="binance",
        symbol=symbol,
        interval=interval,
        start_time=start,
        end_time=end,
        fetch_klines=fetch_klines,
    )
    before_rows = before.row_count if before is not None else 0
    added_rows = max(metadata.row_count - before_rows, 0)
    return UpdateDataResult(
        symbol=symbol,
        interval=interval,
        requested_start=start,
        requested_end=end,
        actual_start=metadata.first_open_time,
        actual_end=metadata.last_open_time,
        added_rows=added_rows,
        total_rows=metadata.row_count,
        parquet_path=store.dataset_path(symbol, interval),
        metadata_path=store.metadata_path(symbol, interval),
        mode="created" if before is None else "incremental",
    )


def execute_validate_data(
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    data_root: str | Path = Path("data/parquet"),
) -> ValidateDataResult:
    if start >= end:
        raise ConfigurationError("start must be earlier than end")
    store = ParquetKlineStore(data_root)
    parquet_path = store.dataset_path(symbol, interval)
    metadata_path = store.metadata_path(symbol, interval)

    extra_issues: list[DataIssue] = []
    if not parquet_path.exists():
        extra_issues.append(
            DataIssue(
                severity="error",
                code="dataset_missing",
                message="Parquet file does not exist",
            )
        )
        report = DataQualityReport(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            row_count=0,
            issues=extra_issues,
        )
        return _build_validate_data_result(
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            parquet_path=parquet_path,
            metadata_path=metadata_path,
            query_result=QueryResult(klines=[], report=report),
            extra_issues=extra_issues,
        )

    query_result = query_backtest_klines(
        root=data_root,
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
    )

    if not metadata_path.exists():
        extra_issues.append(
            DataIssue(
                severity="error",
                code="metadata_missing",
                message="Metadata file does not exist",
            )
        )

    return _build_validate_data_result(
        symbol=symbol,
        interval=interval,
        start=start,
        end=end,
        parquet_path=parquet_path,
        metadata_path=metadata_path,
        query_result=query_result,
        extra_issues=extra_issues,
    )


def _build_validate_data_result(
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    parquet_path: Path,
    metadata_path: Path,
    query_result: QueryResult,
    extra_issues: list[DataIssue],
) -> ValidateDataResult:
    issues = [*query_result.report.issues, *extra_issues]
    klines = query_result.klines
    actual_start = klines[0].open_time.isoformat().replace("+00:00", "Z") if klines else None
    actual_end = klines[-1].open_time.isoformat().replace("+00:00", "Z") if klines else None
    error_count = sum(1 for issue in issues if issue.severity == "error")
    warning_count = sum(1 for issue in issues if issue.severity == "warning")
    info_count = sum(1 for issue in issues if issue.severity == "info")
    return ValidateDataResult(
        symbol=symbol,
        interval=interval,
        parquet_path=parquet_path,
        metadata_path=metadata_path,
        requested_start=start,
        requested_end=end,
        actual_start=actual_start,
        actual_end=actual_end,
        row_count=len(klines),
        issue_count=len(issues),
        error_count=error_count,
        warning_count=warning_count,
        info_count=info_count,
        issues=issues,
        status="VALID" if error_count == 0 else "INVALID",
    )


def _print_update_data_result(result: UpdateDataResult) -> None:
    print(f"symbol: {result.symbol}")
    print(f"interval: {result.interval}")
    print(
        "requested_range: "
        f"{result.requested_start.isoformat().replace('+00:00', 'Z')} -> "
        f"{result.requested_end.isoformat().replace('+00:00', 'Z')}"
    )
    print(f"data_period: {result.actual_start or 'None'} -> {result.actual_end or 'None'}")
    print(f"added_rows: {result.added_rows}")
    print(f"total_rows: {result.total_rows}")
    print(f"parquet_path: {result.parquet_path}")
    print(f"metadata_path: {result.metadata_path}")
    print(f"mode: {result.mode}")


def _print_validate_data_result(result: ValidateDataResult) -> None:
    print(f"symbol: {result.symbol}")
    print(f"interval: {result.interval}")
    print(f"parquet_path: {result.parquet_path}")
    print(f"metadata_path: {result.metadata_path}")
    print(
        "requested_range: "
        f"{result.requested_start.isoformat().replace('+00:00', 'Z')} -> "
        f"{result.requested_end.isoformat().replace('+00:00', 'Z')}"
    )
    print(f"data_period: {result.actual_start or 'None'} -> {result.actual_end or 'None'}")
    print(f"row_count: {result.row_count}")
    print(f"error_count: {result.error_count}")
    print(f"warning_count: {result.warning_count}")
    print(f"info_count: {result.info_count}")
    print(f"status: {result.status}")
    for issue in result.issues:
        prefix = issue.severity.upper()
        if issue.open_time is None:
            print(f"{prefix} {issue.code}: {issue.message}")
        else:
            print(f"{prefix} {issue.code} {issue.open_time.isoformat().replace('+00:00', 'Z')}: {issue.message}")


def _parse_symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError("symbol cannot be empty")
    return value.strip()


def _parse_interval(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError("interval cannot be empty")
    normalized = value.strip()
    if normalized not in INTERVAL_TO_MS:
        raise argparse.ArgumentTypeError(f"unsupported interval: {normalized}")
    return normalized


def _parse_utc_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise argparse.ArgumentTypeError("datetime cannot be empty")
    raw = value.strip()
    if "T" not in raw:
        raise argparse.ArgumentTypeError("datetime must include time and timezone")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include timezone")
    return parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
