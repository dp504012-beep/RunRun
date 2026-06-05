from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import csv
import json

import pytest

from src.backtest_runner import execute_backtest_run
from src.data.binance_client import Kline


def _write_config(path: Path, *, bad_strategy: bool = False) -> None:
    fixed_parameters = "      liquidation_model: simplified" if bad_strategy else "      leverage: 2\n      liquidation_model: simplified"
    path.write_text(
        f"""
market:
  source: binance
  symbol: BTCUSDT
  interval: 1h
  start: "2024-01-01T00:00:00Z"
  end: "2024-01-01T03:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0004
  slippage_rate: 0.0
  quote_currency: USDT
strategies:
  - id: spot-btc
    type: btc_buy_and_hold
    initial_capital: 1000
    parameters:
      include_exit_fee: true
  - id: leverage-2x
    type: fixed_leverage_long
    initial_capital: 500
    parameters:
{fixed_parameters}
  - id: grid-10x
    type: long_grid
    initial_capital: 1000
    parameters:
      leverage: 10
      lower_bound: 90
      upper_bound: 120
      grid_count: 3
      liquidation_model: simplified
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )


def _fake_fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
    return [
        _kline(0, symbol=symbol, interval=interval, open_price="100", high="110", low="99", close="105"),
        _kline(1, symbol=symbol, interval=interval, open_price="105", high="115", low="104", close="110"),
        _kline(2, symbol=symbol, interval=interval, open_price="110", high="120", low="109", close="115"),
    ]


def _kline(
    hour: int,
    *,
    symbol: str,
    interval: str,
    open_price: str,
    high: str,
    low: str,
    close: str,
) -> Kline:
    return Kline(
        symbol=symbol,
        interval=interval,
        open_time=datetime(2024, 1, 1, hour, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def test_execute_backtest_run_writes_unified_success_report(tmp_path: Path) -> None:
    config_path = tmp_path / "backtest.yaml"
    _write_config(config_path)

    execution = execute_backtest_run(
        config_path,
        data_root=tmp_path / "parquet",
        output_dir=tmp_path / "reports",
        fetch_klines=_fake_fetch_klines,
        created_at=datetime(2024, 1, 2, tzinfo=UTC),
        run_id="run-ok",
    )

    assert execution.failed_strategy_count == 0
    assert [result.metrics["strategy_id"] for result in execution.results] == ["spot-btc", "leverage-2x", "grid-10x"]
    assert [result.metrics["status"] for result in execution.results] == ["success", "success", "success"]
    assert len({result.metrics["data_snapshot"]["fingerprint"] for result in execution.results}) == 1

    assert execution.report_paths is not None
    with execution.report_paths.performance_summary_csv.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["strategy_id"] for row in rows] == ["spot-btc", "leverage-2x", "grid-10x"]
    assert [row["status"] for row in rows] == ["success", "success", "success"]
    assert float(rows[0]["final_equity"]) == pytest.approx(execution.results[0].final_equity)

    snapshot = json.loads(execution.report_paths.config_snapshot.read_text(encoding="utf-8"))
    assert snapshot["_run_id"] == "run-ok"
    assert [item["id"] for item in snapshot["strategies"]] == ["spot-btc", "leverage-2x", "grid-10x"]


def test_execute_backtest_run_marks_single_strategy_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "backtest.yaml"
    _write_config(config_path, bad_strategy=True)

    execution = execute_backtest_run(
        config_path,
        data_root=tmp_path / "parquet",
        output_dir=tmp_path / "reports",
        fetch_klines=_fake_fetch_klines,
        created_at=datetime(2024, 1, 2, tzinfo=UTC),
        run_id="run-failed-strategy",
    )

    assert [result.metrics["strategy_id"] for result in execution.results] == ["spot-btc", "leverage-2x", "grid-10x"]
    assert [result.metrics["status"] for result in execution.results] == ["success", "failed", "success"]
    assert execution.results[1].final_equity is None
    assert "leverage" in execution.results[1].metrics["error"]

    assert execution.report_paths is not None
    with execution.report_paths.performance_summary_csv.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert rows[1]["strategy_id"] == "leverage-2x"
    assert rows[1]["status"] == "failed"
    assert rows[1]["final_equity"] == ""


def test_default_run_id_preserves_microseconds(tmp_path: Path) -> None:
    config_path = tmp_path / "backtest.yaml"
    _write_config(config_path)

    execution = execute_backtest_run(
        config_path,
        data_root=tmp_path / "parquet",
        output_dir=tmp_path / "reports",
        fetch_klines=_fake_fetch_klines,
        created_at=datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=UTC),
    )

    assert execution.run_id == "20240102T030405678901Z"
    assert execution.report_paths is not None
    assert execution.report_paths.run_id == execution.run_id
