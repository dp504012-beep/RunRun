from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.binance_client import Kline
from src.data.parquet_store import ParquetKlineStore, update_parquet_store


def _kline(hour: int, symbol: str = "BTCUSDT", interval: str = "1h") -> Kline:
    open_time = datetime(2024, 1, 1, hour, 0, tzinfo=UTC)
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=open_time,
        open=Decimal("1.0") + Decimal(hour),
        high=Decimal("2.0") + Decimal(hour),
        low=Decimal("0.5") + Decimal(hour),
        close=Decimal("1.5") + Decimal(hour),
        volume=Decimal("10.0") + Decimal(hour),
    )


def test_first_update_creates_parquet_and_metadata(tmp_path: Path) -> None:
    store = ParquetKlineStore(tmp_path / "parquet")
    calls: list[tuple[datetime, datetime]] = []

    def fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        calls.append((start_time, end_time))
        return [_kline(0), _kline(1), _kline(2)]

    metadata = update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    assert calls == [(datetime(2024, 1, 1, 0, 0, tzinfo=UTC), datetime(2024, 1, 1, 3, 0, tzinfo=UTC))]
    assert store.dataset_path("BTCUSDT", "1h").exists()
    assert store.metadata_path("BTCUSDT", "1h").exists()
    assert metadata.row_count == 3
    assert metadata.first_open_time == "2024-01-01T00:00:00Z"
    assert metadata.last_open_time == "2024-01-01T02:00:00Z"

    loaded = store.load_klines("BTCUSDT", "1h")
    assert [item.open_time for item in loaded] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    ]


def test_second_update_only_requests_missing_period(tmp_path: Path) -> None:
    store = ParquetKlineStore(tmp_path / "parquet")
    calls: list[tuple[datetime, datetime]] = []
    page_map = {
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC): [_kline(0), _kline(1), _kline(2)],
        datetime(2024, 1, 1, 3, 0, tzinfo=UTC): [_kline(3), _kline(4)],
    }

    def fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        calls.append((start_time, end_time))
        return page_map.get(start_time, [])

    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )
    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 5, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 1, 0, tzinfo=UTC),
    )

    assert calls == [
        (datetime(2024, 1, 1, 0, 0, tzinfo=UTC), datetime(2024, 1, 1, 3, 0, tzinfo=UTC)),
        (datetime(2024, 1, 1, 3, 0, tzinfo=UTC), datetime(2024, 1, 1, 5, 0, tzinfo=UTC)),
    ]
    assert store.load_metadata("BTCUSDT", "1h").row_count == 5
    assert [item.open_time for item in store.load_klines("BTCUSDT", "1h")] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 4, 0, tzinfo=UTC),
    ]


def test_duplicate_rows_do_not_increase_row_count(tmp_path: Path) -> None:
    store = ParquetKlineStore(tmp_path / "parquet")

    def fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        return [_kline(0), _kline(1), _kline(1), _kline(2)]

    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    rows = store.load_klines("BTCUSDT", "1h")
    assert len(rows) == 3
    assert [row.open_time for row in rows] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    ]


def test_failed_update_keeps_old_data_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ParquetKlineStore(tmp_path / "parquet")

    def initial_fetch(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        return [_kline(0), _kline(1)]

    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        fetch_klines=initial_fetch,
        now=lambda: datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    original_replace = Path.replace

    def flaky_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".metadata.json.tmp") and target.name.endswith(".metadata.json"):
            raise OSError("metadata write failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    def update_fetch(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        return [_kline(2)]

    with pytest.raises(OSError, match="metadata write failed"):
        update_parquet_store(
            root=store._root,
            source="binance",
            symbol="BTCUSDT",
            interval="1h",
            start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end_time=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
            fetch_klines=update_fetch,
            now=lambda: datetime(2024, 1, 2, 1, 0, tzinfo=UTC),
        )

    assert [row.open_time for row in store.load_klines("BTCUSDT", "1h")] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    ]


def test_same_input_rerun_is_idempotent(tmp_path: Path) -> None:
    store = ParquetKlineStore(tmp_path / "parquet")
    calls = 0

    def fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        nonlocal calls
        calls += 1
        return [_kline(0), _kline(1)]

    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )
    update_parquet_store(
        root=store._root,
        source="binance",
        symbol="BTCUSDT",
        interval="1h",
        start_time=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end_time=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        fetch_klines=fetch_klines,
        now=lambda: datetime(2024, 1, 2, 1, 0, tzinfo=UTC),
    )

    assert calls == 1
    assert store.load_metadata("BTCUSDT", "1h").row_count == 2
