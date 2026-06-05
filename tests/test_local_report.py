from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import csv
import json
import xml.etree.ElementTree as ET

import pytest

from src.backtest_config import BacktestConfig, CommonConfig, MarketConfig, OutputConfig, StrategyConfig
from src.data.binance_client import Kline
from src.outputs.local_report import generate_local_report
from src.strategies.buy_and_hold import STRATEGY_TYPE, run_btc_buy_and_hold


def _config(strategy: StrategyConfig) -> BacktestConfig:
    return BacktestConfig(
        market=MarketConfig(
            source="binance",
            symbol="BTCUSDT",
            interval="1h",
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 3, 0, tzinfo=UTC),
        ),
        common=CommonConfig(timezone="UTC", fee_rate=0.001, slippage_rate=0.0, quote_currency="USDT"),
        strategies=[strategy],
        output=OutputConfig(
            local_report=True,
            google_sheet_id=None,
            equity_curve_sampling=1,
            write_trade_log=True,
            create_charts=True,
        ),
    )


def _kline(hour: int, *, open_price: str = "100", close_price: str = "100") -> Kline:
    return Kline(
        symbol="BTCUSDT",
        interval="1h",
        open_time=datetime(2024, 1, 1, hour, 0, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(max(open_price, close_price)),
        low=Decimal(min(open_price, close_price)),
        close=Decimal(close_price),
        volume=Decimal("1"),
    )


def _result(strategy_id: str, initial_capital: float) -> tuple[BacktestConfig, object]:
    strategy = StrategyConfig(
        id=strategy_id,
        type=STRATEGY_TYPE,
        initial_capital=initial_capital,
        parameters={"include_exit_fee": True},
    )
    config = _config(strategy)
    result = run_btc_buy_and_hold(
        config=config,
        strategy=strategy,
        klines=[
            _kline(0, open_price="100", close_price="100"),
            _kline(1, open_price="100", close_price="110"),
            _kline(2, open_price="110", close_price="120"),
        ],
        finished_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )
    return config, result


def test_local_report_writes_html_csv_and_charts_from_results(tmp_path: Path) -> None:
    config, result = _result("buy-hold-btc", 500.0)
    paths = generate_local_report(
        config=config,
        results=[result],
        output_dir=tmp_path,
        run_id="test-run-001",
        warnings=["data quality checked before report"],
        created_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    for path in [
        paths.config_snapshot,
        paths.performance_summary_csv,
        paths.equity_curve_csv,
        paths.trade_log_csv,
        paths.html_report,
        paths.equity_curve_chart,
        paths.return_comparison_chart,
        paths.max_drawdown_comparison_chart,
        paths.warnings_and_limitations,
    ]:
        assert path.exists()
        assert "test-run-001" in path.name

    html = paths.html_report.read_text(encoding="utf-8")
    assert "binance" in html
    assert "2024-01-01T00:00:00Z" in html
    assert "BTCUSDT / 1h" in html
    assert "data quality checked before report" in html
    assert "第一根 K 線開盤價一次性買入" in html
    assert paths.equity_curve_chart.name in html

    snapshot = json.loads(paths.config_snapshot.read_text(encoding="utf-8"))
    assert snapshot["market"]["source"] == "binance"
    assert snapshot["market"]["symbol"] == "BTCUSDT"

    with paths.performance_summary_csv.open(encoding="utf-8", newline="") as file:
        summary_rows = list(csv.DictReader(file))
    assert len(summary_rows) == 1
    assert float(summary_rows[0]["final_equity"]) == pytest.approx(result.final_equity)
    assert float(summary_rows[0]["total_return"]) == pytest.approx(result.total_return)
    assert float(summary_rows[0]["total_fees"]) == pytest.approx(result.trades[0].fee)

    with paths.equity_curve_csv.open(encoding="utf-8", newline="") as file:
        equity_rows = list(csv.DictReader(file))
    assert len(equity_rows) == len(result.equity_curve)
    assert float(equity_rows[-1]["equity"]) == pytest.approx(result.final_equity)

    with paths.trade_log_csv.open(encoding="utf-8", newline="") as file:
        trade_rows = list(csv.DictReader(file))
    assert float(trade_rows[0]["fee"]) == pytest.approx(result.trades[0].fee)

    for chart in [paths.equity_curve_chart, paths.return_comparison_chart, paths.max_drawdown_comparison_chart]:
        ET.parse(chart)


def test_local_report_supports_multiple_strategy_results(tmp_path: Path) -> None:
    config, first = _result("buy-hold-btc-a", 500.0)
    _, second = _result("buy-hold-btc-b", 800.0)

    paths = generate_local_report(
        config=config,
        results=[first, second],
        output_dir=tmp_path,
        run_id="multi-run",
        created_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
    )

    with paths.performance_summary_csv.open(encoding="utf-8", newline="") as file:
        summary_rows = list(csv.DictReader(file))
    assert [row["strategy_id"] for row in summary_rows] == ["buy-hold-btc-a", "buy-hold-btc-b"]


def test_local_report_does_not_overwrite_existing_report_without_opt_in(tmp_path: Path) -> None:
    config, result = _result("buy-hold-btc", 500.0)
    generate_local_report(config=config, results=[result], output_dir=tmp_path, run_id="same-run")

    with pytest.raises(FileExistsError, match="報告已存在"):
        generate_local_report(config=config, results=[result], output_dir=tmp_path, run_id="same-run")
