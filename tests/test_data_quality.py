from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.data.quality import QueryResult, load_backtest_klines, query_backtest_klines
from src.exceptions import DataValidationError


def _write_parquet(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "symbol": [row["symbol"] for row in rows],
            "interval": [row["interval"] for row in rows],
            "open_time": [row["open_time"] for row in rows],
            "open": [row["open"] for row in rows],
            "high": [row["high"] for row in rows],
            "low": [row["low"] for row in rows],
            "close": [row["close"] for row in rows],
            "volume": [row["volume"] for row in rows],
        }
    )
    pq.write_table(table, path)


def _row(hour: int, *, symbol: str = "BTCUSDT", interval: str = "1h", open_price: str = "100", high: str = "110", low: str = "90", close: str = "105", volume: str = "10") -> dict[str, object]:
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time": int(datetime(2024, 1, 1, hour, 0, tzinfo=UTC).timestamp() * 1000),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_query_returns_sorted_data_and_excludes_end_boundary(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    _write_parquet(parquet, [_row(2), _row(0), _row(1)])

    result = query_backtest_klines(
        root=tmp_path / "parquet",
        symbol="BTCUSDT",
        interval="1h",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )

    assert isinstance(result, QueryResult)
    assert [kline.open_time for kline in result.klines] == [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    ]
    assert result.report.errors == []
    assert result.report.infos


def test_duplicate_kline_is_reported_as_warning(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    _write_parquet(parquet, [_row(0), _row(1), _row(1), _row(2)])

    result = query_backtest_klines(
        root=tmp_path / "parquet",
        symbol="BTCUSDT",
        interval="1h",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
    )

    assert any(issue.code == "duplicate_open_time" for issue in result.report.warnings)
    assert len(result.klines) == 4


def test_missing_hour_is_reported_and_rejected(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    _write_parquet(parquet, [_row(0), _row(1), _row(3)])

    result = query_backtest_klines(
        root=tmp_path / "parquet",
        symbol="BTCUSDT",
        interval="1h",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 4, 0, tzinfo=UTC),
    )

    assert any(issue.code == "period_missing_open_time" for issue in result.report.errors)

    with pytest.raises(DataValidationError, match="period_missing_open_time"):
        load_backtest_klines(
            root=tmp_path / "parquet",
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 4, 0, tzinfo=UTC),
        )


def test_invalid_ohlc_and_negative_volume_are_reported(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    _write_parquet(parquet, [_row(0, high="99"), _row(1, volume="-1")])

    result = query_backtest_klines(
        root=tmp_path / "parquet",
        symbol="BTCUSDT",
        interval="1h",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )

    assert any(issue.code == "ohlc_invalid" for issue in result.report.errors)
    assert any(issue.code == "negative_volume" for issue in result.report.errors)


def test_symbol_and_interval_mismatch_are_reported(tmp_path: Path) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    _write_parquet(parquet, [_row(0, symbol="ETHUSDT"), _row(1, interval="2h")])

    result = query_backtest_klines(
        root=tmp_path / "parquet",
        symbol="BTCUSDT",
        interval="1h",
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )

    assert any(issue.code == "symbol_mismatch" for issue in result.report.errors)
    assert any(issue.code == "interval_mismatch" for issue in result.report.errors)
