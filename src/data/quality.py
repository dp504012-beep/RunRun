from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pyarrow.parquet as pq

from src.data.binance_client import INTERVAL_TO_MS, Kline
from src.data.parquet_store import ParquetKlineStore
from src.exceptions import DataValidationError

Severity = Literal["error", "warning", "info"]


@dataclass(slots=True)
class DataIssue:
    severity: Severity
    code: str
    message: str
    open_time: datetime | None = None


@dataclass(slots=True)
class DataQualityReport:
    symbol: str
    interval: str
    start: datetime
    end: datetime
    row_count: int
    issues: list[DataIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[DataIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[DataIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def infos(self) -> list[DataIssue]:
        return [issue for issue in self.issues if issue.severity == "info"]


@dataclass(slots=True)
class QueryResult:
    klines: list[Kline]
    report: DataQualityReport


def query_backtest_klines(
    *,
    root: str | Path,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> QueryResult:
    store = ParquetKlineStore(root)
    dataset_path = store.dataset_path(symbol, interval)
    if not dataset_path.exists():
        report = DataQualityReport(
            symbol=symbol,
            interval=interval,
            start=_ensure_utc(start),
            end=_ensure_utc(end),
            row_count=0,
            issues=[
                DataIssue(
                    severity="error",
                    code="dataset_missing",
                    message="找不到對應的 Parquet 資料檔。",
                )
            ],
        )
        return QueryResult(klines=[], report=report)

    table = pq.read_table(
        dataset_path,
        columns=["symbol", "interval", "open_time", "open", "high", "low", "close", "volume"],
    )
    rows = table.to_pylist()
    klines = [_row_to_kline(row) for row in rows]
    filtered = [
        kline
        for kline in klines
        if _ensure_utc(start) <= kline.open_time < _ensure_utc(end)
    ]
    report = _validate_rows(
        symbol=symbol,
        interval=interval,
        start=_ensure_utc(start),
        end=_ensure_utc(end),
        klines=filtered,
    )
    sorted_klines = sorted(filtered, key=lambda item: item.open_time)
    return QueryResult(klines=sorted_klines, report=report)


def load_backtest_klines(
    *,
    root: str | Path,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
) -> list[Kline]:
    result = query_backtest_klines(root=root, symbol=symbol, interval=interval, start=start, end=end)
    if result.report.errors:
        first = next(
            (issue for issue in result.report.errors if issue.code == "period_missing_open_time"),
            result.report.errors[0],
        )
        raise DataValidationError(f"{first.code}: {first.message}")
    return result.klines


def _validate_rows(
    *,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    klines: list[Kline],
) -> DataQualityReport:
    issues: list[DataIssue] = [
        DataIssue(
            severity="info",
            code="range_loaded",
            message=f"載入區間 {start.isoformat()} 到 {end.isoformat()}，共 {len(klines)} 筆。",
        )
    ]

    if interval not in INTERVAL_TO_MS:
        issues.append(DataIssue(severity="error", code="interval_unsupported", message=f"不支援的 interval: {interval}"))
        return DataQualityReport(symbol=symbol, interval=interval, start=start, end=end, row_count=len(klines), issues=issues)

    interval_ms = INTERVAL_TO_MS[interval]
    if not klines:
        issues.append(
            DataIssue(
                severity="error",
                code="period_incomplete",
                message="查詢區間沒有任何可用 K 線資料。",
            )
        )
        return DataQualityReport(symbol=symbol, interval=interval, start=start, end=end, row_count=0, issues=issues)

    filtered = [kline for kline in klines if start <= kline.open_time < end]
    if not filtered:
        issues.append(
            DataIssue(
                severity="error",
                code="period_incomplete",
                message="查詢區間沒有任何可用 K 線資料。",
            )
        )
        return DataQualityReport(symbol=symbol, interval=interval, start=start, end=end, row_count=0, issues=issues)

    if any(kline.symbol != symbol for kline in filtered):
        issues.append(DataIssue(severity="error", code="symbol_mismatch", message="K 線 symbol 與查詢條件不一致。"))

    if any(kline.interval != interval for kline in filtered):
        issues.append(DataIssue(severity="error", code="interval_mismatch", message="K 線 interval 與查詢條件不一致。"))

    open_times = [kline.open_time for kline in filtered]
    if open_times != sorted(open_times):
        issues.append(DataIssue(severity="warning", code="open_time_unsorted", message="open_time 順序不正確，已回傳排序後資料。"))

    seen: set[datetime] = set()
    for kline in filtered:
        if kline.open_time in seen:
            issues.append(DataIssue(severity="warning", code="duplicate_open_time", message="發現重複 K 線。", open_time=kline.open_time))
        seen.add(kline.open_time)

    for prev, current in zip(sorted(filtered, key=lambda item: item.open_time), sorted(filtered, key=lambda item: item.open_time)[1:]):
        delta_ms = int((current.open_time - prev.open_time).total_seconds() * 1000)
        if delta_ms != interval_ms:
            if delta_ms > interval_ms:
                missing = prev.open_time + timedelta(milliseconds=interval_ms)
                while missing < current.open_time:
                    issues.append(
                        DataIssue(
                            severity="error",
                            code="time_gap",
                            message="發現時間缺口。",
                            open_time=missing,
                        )
                    )
                    missing += timedelta(milliseconds=interval_ms)
            else:
                issues.append(DataIssue(severity="warning", code="duplicate_or_overlap", message="發現重疊或異常時間順序。"))

    expected = _expected_open_times(start, end, interval_ms)
    actual = {kline.open_time for kline in filtered}
    missing_expected = [moment for moment in expected if moment not in actual]
    for missing in missing_expected:
        issues.append(DataIssue(severity="error", code="period_missing_open_time", message="期間資料不完整。", open_time=missing))

    for kline in filtered:
        _validate_ohlc(kline, issues)

    return DataQualityReport(symbol=symbol, interval=interval, start=start, end=end, row_count=len(filtered), issues=issues)


def _validate_ohlc(kline: Kline, issues: list[DataIssue]) -> None:
    values = [kline.open, kline.high, kline.low, kline.close]
    if any(value <= 0 for value in values):
        issues.append(DataIssue(severity="error", code="non_positive_price", message="價格必須大於 0。", open_time=kline.open_time))
    if kline.volume < 0:
        issues.append(DataIssue(severity="error", code="negative_volume", message="volume 不可為負值。", open_time=kline.open_time))
    if kline.high < kline.open or kline.high < kline.close or kline.low > kline.open or kline.low > kline.close or kline.high < kline.low:
        issues.append(DataIssue(severity="error", code="ohlc_invalid", message="OHLC 不符合合理性規則。", open_time=kline.open_time))


def _expected_open_times(start: datetime, end: datetime, interval_ms: int) -> list[datetime]:
    expected: list[datetime] = []
    cursor = start
    while cursor < end:
        expected.append(cursor)
        cursor += timedelta(milliseconds=interval_ms)
    return expected


def _row_to_kline(row: dict[str, object]) -> Kline:
    return Kline(
        symbol=str(row["symbol"]),
        interval=str(row["interval"]),
        open_time=datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=UTC),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
