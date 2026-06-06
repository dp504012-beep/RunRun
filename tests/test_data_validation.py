from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src import cli


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


def _write_metadata(path: Path, *, symbol: str = "BTCUSDT", interval: str = "1h", row_count: int = 0, first_open_time: str | None = None, last_open_time: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": "binance",
                "symbol": symbol,
                "interval": interval,
                "first_open_time": first_open_time,
                "last_open_time": last_open_time,
                "row_count": row_count,
                "updated_at": "2024-01-01T00:00:00Z",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
def test_validate_data_valid_returns_zero_and_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    metadata = tmp_path / "parquet" / "BTCUSDT" / "1h.metadata.json"
    rows = [_row(0), _row(1)]
    _write_parquet(parquet, rows)
    _write_metadata(
        metadata,
        row_count=2,
        first_open_time="2024-01-01T00:00:00Z",
        last_open_time="2024-01-01T01:00:00Z",
    )

    exit_code = cli.main(
        [
            "validate-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T02:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "row_count: 2" in captured.out
    assert "status: VALID" in captured.out
    assert "error_count: 0" in captured.out


def test_validate_data_warning_only_returns_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    metadata = tmp_path / "parquet" / "BTCUSDT" / "1h.metadata.json"
    rows = [_row(0), _row(1), _row(1), _row(2)]
    _write_parquet(parquet, rows)
    _write_metadata(
        metadata,
        row_count=4,
        first_open_time="2024-01-01T00:00:00Z",
        last_open_time="2024-01-01T02:00:00Z",
    )

    exit_code = cli.main(
        [
            "validate-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T03:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "warning_count:" in captured.out
    assert "duplicate_open_time" in captured.out
    assert "status: VALID" in captured.out


def test_validate_data_error_returns_non_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    parquet = tmp_path / "parquet" / "BTCUSDT" / "1h.parquet"
    metadata = tmp_path / "parquet" / "BTCUSDT" / "1h.metadata.json"
    rows = [_row(0, high="99")]
    _write_parquet(parquet, rows)
    _write_metadata(
        metadata,
        row_count=1,
        first_open_time="2024-01-01T00:00:00Z",
        last_open_time="2024-01-01T00:00:00Z",
    )

    exit_code = cli.main(
        [
            "validate-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T01:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "ohlc_invalid" in captured.out
    assert "status: INVALID" in captured.out


def test_validate_data_missing_file_returns_non_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(
        [
            "validate-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-01T00:00:00Z",
            "--end",
            "2024-01-01T01:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "dataset_missing" in captured.out
    assert "status: INVALID" in captured.out


def test_validate_data_rejects_invalid_date() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "validate-data",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "1h",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-02T00:00:00Z",
            ]
        )

    assert excinfo.value.code == 2


def test_validate_data_rejects_start_after_end(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(
        [
            "validate-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-01-02T00:00:00Z",
            "--end",
            "2024-01-01T00:00:00Z",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "start must be earlier than end" in captured.err


def test_validate_data_rejects_unsupported_interval() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "validate-data",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "7h",
                "--start",
                "2024-01-01T00:00:00Z",
                "--end",
                "2024-01-02T00:00:00Z",
            ]
        )

    assert excinfo.value.code == 2
