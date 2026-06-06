from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

from src import cli
from src.data.binance_client import Kline
from src.exceptions import MarketDataError


def test_cli_help_executes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "update-data" in result.stdout


def test_update_data_help_executes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "update-data",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--symbol" in result.stdout
    assert "--interval" in result.stdout
    assert "--start" in result.stdout
    assert "--end" in result.stdout


def test_export_sheets_help_executes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "export-sheets",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--run-dir" in result.stdout
    assert "--spreadsheet-id" in result.stdout


def test_validate_data_help_executes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli",
            "validate-data",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--symbol" in result.stdout
    assert "--interval" in result.stdout
    assert "--start" in result.stdout
    assert "--end" in result.stdout


def test_update_data_calls_updater_and_reports_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[datetime, datetime]] = []

    def fake_fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        calls.append((start_time, end_time))
        if start_time == datetime(2024, 6, 5, 0, 0, tzinfo=UTC):
            hours = [0, 1, 2]
        elif start_time == datetime(2024, 6, 5, 3, 0, tzinfo=UTC):
            hours = [3, 4]
        else:
            hours = []
        return [
            Kline(
                symbol=symbol,
                interval=interval,
                open_time=datetime(2024, 6, 5, hour, 0, tzinfo=UTC),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )
            for hour in hours
        ]

    monkeypatch.setattr(cli, "download_spot_klines", fake_fetch_klines)

    first_exit = cli.main(
        [
            "update-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-06-05T00:00:00Z",
            "--end",
            "2024-06-05T03:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )
    first_output = capsys.readouterr().out

    second_exit = cli.main(
        [
            "update-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-06-05T00:00:00Z",
            "--end",
            "2024-06-05T05:00:00Z",
            "--data-root",
            str(tmp_path / "parquet"),
        ]
    )
    second_output = capsys.readouterr().out

    assert first_exit == 0
    assert second_exit == 0
    assert calls == [
        (datetime(2024, 6, 5, 0, 0, tzinfo=UTC), datetime(2024, 6, 5, 3, 0, tzinfo=UTC)),
        (datetime(2024, 6, 5, 3, 0, tzinfo=UTC), datetime(2024, 6, 5, 5, 0, tzinfo=UTC)),
    ]
    assert "mode: created" in first_output
    assert "added_rows: 3" in first_output
    assert "total_rows: 3" in first_output
    assert "mode: incremental" in second_output
    assert "added_rows: 2" in second_output
    assert "total_rows: 5" in second_output
    assert "parquet_path:" in first_output
    assert "metadata_path:" in first_output


def test_update_data_rejects_invalid_date(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "update-data",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "1h",
                "--start",
                "2024-06-05",
                "--end",
                "2024-06-06T00:00:00Z",
            ]
        )

    assert excinfo.value.code == 2


def test_update_data_rejects_start_after_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    called = False

    def fake_updater(**kwargs: object) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(cli, "update_parquet_store", fake_updater)

    exit_code = cli.main(
        [
            "update-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-06-06T00:00:00Z",
            "--end",
            "2024-06-05T00:00:00Z",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert not called
    assert "update-data failed" in captured.err


def test_update_data_rejects_unsupported_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            [
                "update-data",
                "--symbol",
                "BTCUSDT",
                "--interval",
                "7h",
                "--start",
                "2024-06-05T00:00:00Z",
                "--end",
                "2024-06-06T00:00:00Z",
            ]
        )

    assert excinfo.value.code == 2


def test_update_data_returns_non_zero_on_updater_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def failing_updater(**kwargs: object) -> object:
        raise MarketDataError("boom")

    monkeypatch.setattr(cli, "update_parquet_store", failing_updater)

    exit_code = cli.main(
        [
            "update-data",
            "--symbol",
            "BTCUSDT",
            "--interval",
            "1h",
            "--start",
            "2024-06-05T00:00:00Z",
            "--end",
            "2024-06-06T00:00:00Z",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "update-data failed: boom" in captured.err
