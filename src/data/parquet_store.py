from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import shutil
from pathlib import Path
from typing import Callable, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from src.data.binance_client import Kline, _interval_to_ms
from src.exceptions import MarketDataError


class KlineFetcher(Protocol):
    def __call__(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[Kline]: ...


@dataclass(slots=True)
class ParquetMetadata:
    source: str
    symbol: str
    interval: str
    first_open_time: str | None
    last_open_time: str | None
    row_count: int
    updated_at: str


class ParquetKlineStore:
    def __init__(self, root: str | Path = Path("data/parquet")) -> None:
        self._root = Path(root)

    def dataset_path(self, symbol: str, interval: str) -> Path:
        return self._root / symbol / f"{interval}.parquet"

    def metadata_path(self, symbol: str, interval: str) -> Path:
        return self._root / symbol / f"{interval}.metadata.json"

    def load_klines(self, symbol: str, interval: str) -> list[Kline]:
        path = self.dataset_path(symbol, interval)
        if not path.exists():
            return []
        table = pq.read_table(path)
        return [_row_to_kline(symbol, interval, row) for row in table.to_pylist()]

    def read_last_open_time(self, symbol: str, interval: str) -> datetime | None:
        path = self.dataset_path(symbol, interval)
        if not path.exists():
            return None
        table = pq.read_table(path, columns=["open_time"])
        if table.num_rows == 0:
            return None
        value = table.column("open_time")[-1].as_py()
        if value is None:
            return None
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)

    def load_metadata(self, symbol: str, interval: str) -> ParquetMetadata | None:
        path = self.metadata_path(symbol, interval)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return ParquetMetadata(**data)

    def update(
        self,
        *,
        source: str,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
        fetch_klines: KlineFetcher,
        now: Callable[[], datetime] | None = None,
    ) -> ParquetMetadata:
        self._root.mkdir(parents=True, exist_ok=True)

        path = self.dataset_path(symbol, interval)
        metadata_path = self.metadata_path(symbol, interval)
        existing = self.load_klines(symbol, interval)
        interval_ms = _interval_to_ms(interval)

        requested_start = _ensure_utc(start_time)
        requested_end = _ensure_utc(end_time)
        if requested_start >= requested_end:
            raise MarketDataError("start_time 必須早於 end_time。")

        if existing:
            last_open_time = max(kline.open_time for kline in existing)
            fetch_start = last_open_time + timedelta(milliseconds=interval_ms)
        else:
            fetch_start = requested_start

        if fetch_start > requested_end - timedelta(milliseconds=interval_ms):
            if existing:
                metadata = _build_metadata(source, symbol, interval, existing, now=now)
                self._write_metadata(metadata_path, metadata)
                return metadata
            metadata = _build_metadata(source, symbol, interval, [], now=now)
            self._write_metadata(metadata_path, metadata)
            return metadata

        new_rows = fetch_klines(
            symbol=symbol,
            interval=interval,
            start_time=fetch_start,
            end_time=requested_end,
        )
        filtered_new_rows = [
            kline
            for kline in new_rows
            if _is_completed(kline, interval_ms, requested_end)
        ]

        merged = _merge_klines(existing, filtered_new_rows)
        metadata = _build_metadata(source, symbol, interval, merged, now=now)

        if merged == existing:
            return metadata

        self._atomic_replace_dataset(path, metadata_path, merged, metadata)
        return metadata

    def _atomic_replace_dataset(
        self,
        parquet_path: Path,
        metadata_path: Path,
        klines: list[Kline],
        metadata: ParquetMetadata,
    ) -> None:
        parquet_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = parquet_path.parent
        temp_parquet = temp_dir / f"{parquet_path.name}.tmp"
        temp_metadata = temp_dir / f"{metadata_path.name}.tmp"
        backup_parquet = temp_dir / f"{parquet_path.name}.bak"

        _write_parquet(temp_parquet, klines)
        temp_metadata.write_text(json.dumps(asdict(metadata), ensure_ascii=False, indent=2), encoding="utf-8")

        if parquet_path.exists():
            shutil.copy2(parquet_path, backup_parquet)

        try:
            temp_parquet.replace(parquet_path)
            temp_metadata.replace(metadata_path)
        except Exception:
            if backup_parquet.exists():
                backup_parquet.replace(parquet_path)
            elif parquet_path.exists():
                parquet_path.unlink()
            raise
        finally:
            for temp_file in (temp_parquet, temp_metadata, backup_parquet):
                if temp_file.exists():
                    temp_file.unlink()

    def _write_metadata(self, metadata_path: Path, metadata: ParquetMetadata) -> None:
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temp_metadata = metadata_path.with_suffix(metadata_path.suffix + ".tmp")
        temp_metadata.write_text(json.dumps(asdict(metadata), ensure_ascii=False, indent=2), encoding="utf-8")
        temp_metadata.replace(metadata_path)


def update_parquet_store(
    *,
    root: str | Path,
    source: str,
    symbol: str,
    interval: str,
    start_time: datetime,
    end_time: datetime,
    fetch_klines: KlineFetcher,
    now: Callable[[], datetime] | None = None,
) -> ParquetMetadata:
    return ParquetKlineStore(root=root).update(
        source=source,
        symbol=symbol,
        interval=interval,
        start_time=start_time,
        end_time=end_time,
        fetch_klines=fetch_klines,
        now=now,
    )


def _build_metadata(
    source: str,
    symbol: str,
    interval: str,
    klines: list[Kline],
    now: Callable[[], datetime] | None = None,
) -> ParquetMetadata:
    timestamp = _iso_z((now or (lambda: datetime.now(UTC)))())
    first_open_time = _iso_z(klines[0].open_time) if klines else None
    last_open_time = _iso_z(klines[-1].open_time) if klines else None
    return ParquetMetadata(
        source=source,
        symbol=symbol,
        interval=interval,
        first_open_time=first_open_time,
        last_open_time=last_open_time,
        row_count=len(klines),
        updated_at=timestamp,
    )


def _merge_klines(existing: list[Kline], new_rows: list[Kline]) -> list[Kline]:
    merged: dict[tuple[str, str, datetime], Kline] = {}
    for row in existing + new_rows:
        merged[(row.symbol, row.interval, row.open_time)] = row
    return sorted(merged.values(), key=lambda item: item.open_time)


def _write_parquet(path: Path, klines: list[Kline]) -> None:
    table = pa.table(
        {
            "symbol": [k.symbol for k in klines],
            "interval": [k.interval for k in klines],
            "open_time": [_to_utc_millis(k.open_time) for k in klines],
            "open": [str(k.open) for k in klines],
            "high": [str(k.high) for k in klines],
            "low": [str(k.low) for k in klines],
            "close": [str(k.close) for k in klines],
            "volume": [str(k.volume) for k in klines],
        }
    )
    pq.write_table(table, path)


def _row_to_kline(symbol: str, interval: str, row: dict[str, object]) -> Kline:
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=UTC),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row["volume"])),
    )


def _is_completed(kline: Kline, interval_ms: int, end_time: datetime) -> bool:
    return kline.open_time + timedelta(milliseconds=interval_ms) <= end_time.astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _to_utc_millis(value: datetime) -> int:
    return int(_ensure_utc(value).timestamp() * 1000)
