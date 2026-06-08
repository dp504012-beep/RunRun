from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import csv

import pytest

from src.backtest_config import BacktestConfig, CommonConfig, MarketConfig, OutputConfig, StrategyConfig, parse_backtest_config
from src.backtest_runner import execute_backtest_run
from src.data.binance_client import Kline
from src.strategies.dca import run_dca_long


def _config(strategy: StrategyConfig | None = None, *, symbol: str = "ETHUSDT") -> tuple[BacktestConfig, StrategyConfig]:
    strategy = strategy or _strategy()
    config = BacktestConfig(
        market=MarketConfig(
            source="binance",
            symbol=symbol,
            interval="1h",
            start=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 4, 0, tzinfo=UTC),
        ),
        common=CommonConfig(timezone="UTC", fee_rate=0.0, slippage_rate=0.0, quote_currency="USDT"),
        strategies=[strategy],
        output=OutputConfig(
            local_report=True,
            google_sheet_id=None,
            equity_curve_sampling=1,
            write_trade_log=True,
            create_charts=True,
        ),
    )
    return config, strategy


def _strategy(**overrides: object) -> StrategyConfig:
    parameters: dict[str, object] = {
        "position_mode": "spot",
        "leverage": 1,
        "first_entry_percent": 20,
        "dca_drop_percent": 1.5,
        "dca_entry_percent": 20,
        "max_entries": 5,
        "take_profit_percent": 3,
        "stop_loss_percent": None,
        "entry_fee_rate": 0.0005,
        "exit_fee_rate": 0.0005,
        "funding_rate_mode": "zero",
    }
    parameters.update(overrides)
    return StrategyConfig(id="dca-asset", type="dca_long", initial_capital=1000.0, parameters=parameters)


def _kline(hour: int, *, symbol: str = "ETHUSDT", open_price: str, high: str, low: str, close: str) -> Kline:
    return Kline(
        symbol=symbol,
        interval="1h",
        open_time=datetime(2026, 1, 1, hour, 0, tzinfo=UTC),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def _run(klines: list[Kline], strategy: StrategyConfig | None = None, *, symbol: str = "ETHUSDT"):
    config, strategy = _config(strategy, symbol=symbol)
    return run_dca_long(
        config=config,
        strategy=strategy,
        klines=klines,
        finished_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_yaml_can_load_dca_inline_parameters() -> None:
    config = parse_backtest_config(
        {
            "market": {"source": "binance", "symbol": "ETHUSDT", "interval": "1h", "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
            "common": {"timezone": "UTC", "fee_rate": 0.0, "slippage_rate": 0.0, "quote_currency": "USDT"},
            "strategies": [
                {
                    "id": "dca-eth",
                    "type": "dca_long",
                    "initial_capital": 1000,
                    "first_entry_percent": 19,
                    "dca_drop_percent": 5,
                    "dca_entry_percent": 19,
                    "max_entries": 5,
                    "take_profit_percent": 8,
                    "stop_loss_percent": None,
                    "entry_fee_rate": 0.0005,
                    "exit_fee_rate": 0.0005,
                    "funding_rate_mode": "zero",
                }
            ],
            "output": {"local_report": True, "google_sheet_id": None, "equity_curve_sampling": 1, "write_trade_log": True, "create_charts": True},
        }
    )

    assert config.strategies[0].type == "dca_long"
    assert config.strategies[0].parameters["first_entry_percent"] == 19


def test_first_entry_then_take_profit_fee_and_metrics() -> None:
    result = _run([_kline(0, open_price="100", high="104", low="100", close="103")])

    assert result.metrics["total_entries"] == 1
    assert result.metrics["exit_reason"] == "take_profit"
    assert result.final_equity == pytest.approx(1005.797)
    assert result.metrics["total_fees"] == pytest.approx(0.203)
    assert result.trades[0].metadata["entry_sequence"] == 1
    assert result.trades[-1].metadata["exit_reason"] == "take_profit"


def test_same_candle_multiple_dca_then_take_profit_recalculates_average() -> None:
    result = _run([_kline(0, open_price="100", high="101.5", low="95", close="100")], _strategy(take_profit_percent=1))

    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert len(buys) >= 4
    assert buys[1].price == pytest.approx(98.5)
    assert buys[2].price == pytest.approx(97.0225)
    assert result.metrics["exit_reason"] == "take_profit"
    assert result.metrics["average_entry_price"] < 100
    assert len({point.timestamp for point in result.equity_curve}) == len(result.equity_curve)


def test_same_level_is_not_repeated_across_candles() -> None:
    result = _run(
        [
            _kline(0, open_price="100", high="100", low="98.5", close="99"),
            _kline(1, open_price="99", high="99", low="98.5", close="99"),
        ]
    )

    level_ids = [trade.metadata["unique_level_id"] for trade in result.trades if trade.side == "buy"]
    assert len(level_ids) == len(set(level_ids))


def test_max_entries_includes_initial_entry() -> None:
    result = _run([_kline(0, open_price="100", high="100", low="90", close="90")], _strategy(max_entries=2))

    assert result.metrics["total_entries"] == 2
    assert len([trade for trade in result.trades if trade.side == "buy"]) == 2


def test_insufficient_capital_does_not_partial_fill() -> None:
    strategy = _strategy(first_entry_percent=60, dca_entry_percent=60, max_entries=5)
    result = _run([_kline(0, open_price="100", high="100", low="90", close="90")], strategy)

    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert len(buys) == 1
    assert result.metrics["capital_remaining"] > 0


def test_optional_stop_loss_runs_before_take_profit_after_dca() -> None:
    result = _run(
        [_kline(0, open_price="100", high="110", low="94", close="100")],
        _strategy(stop_loss_percent=1, take_profit_percent=3),
    )

    assert result.metrics["exit_reason"] == "stop_loss"
    assert result.trades[-1].side == "sell"


def test_end_of_backtest_closes_at_last_close() -> None:
    result = _run(
        [
            _kline(0, open_price="100", high="101", low="100", close="99"),
            _kline(1, open_price="99", high="100", low="99", close="99"),
        ]
    )

    assert result.metrics["exit_reason"] == "end_of_backtest"
    assert result.metrics["exit_price"] == pytest.approx(99.0)


def test_maximum_unrealized_loss_and_drawdown_are_recorded() -> None:
    result = _run([_kline(0, open_price="100", high="100", low="100", close="90")])

    assert result.metrics["maximum_unrealized_loss"] < 0
    assert result.max_drawdown is not None
    assert result.max_drawdown < 0


def test_btc_and_eth_are_both_supported() -> None:
    eth = _run([_kline(0, symbol="ETHUSDT", open_price="100", high="104", low="100", close="103")], symbol="ETHUSDT")
    btc = _run([_kline(0, symbol="BTCUSDT", open_price="100", high="104", low="100", close="103")], symbol="BTCUSDT")

    assert eth.trades[0].symbol == "ETHUSDT"
    assert btc.trades[0].symbol == "BTCUSDT"


def test_runner_executes_dca_and_writes_local_report(tmp_path: Path) -> None:
    config_path = tmp_path / "backtest.yaml"
    config_path.write_text(
        """
market:
  source: binance
  symbol: ETHUSDT
  interval: 1h
  start: "2026-01-01T00:00:00Z"
  end: "2026-01-01T02:00:00Z"
common:
  timezone: UTC
  fee_rate: 0.0
  slippage_rate: 0.0
  quote_currency: USDT
strategies:
  - id: dca-eth
    type: dca_long
    initial_capital: 1000
    first_entry_percent: 20
    dca_drop_percent: 1.5
    dca_entry_percent: 20
    max_entries: 5
    take_profit_percent: 3
    stop_loss_percent: null
    entry_fee_rate: 0.0005
    exit_fee_rate: 0.0005
    funding_rate_mode: zero
output:
  local_report: true
  google_sheet_id: null
  equity_curve_sampling: 1
  write_trade_log: true
  create_charts: true
""".strip(),
        encoding="utf-8",
    )

    def fetch_klines(*, symbol: str, interval: str, start_time: datetime, end_time: datetime) -> list[Kline]:
        return [
            _kline(0, symbol=symbol, open_price="100", high="104", low="100", close="103"),
            _kline(1, symbol=symbol, open_price="103", high="103", low="103", close="103"),
        ]

    execution = execute_backtest_run(
        config_path,
        data_root=tmp_path / "parquet",
        output_dir=tmp_path / "reports",
        fetch_klines=fetch_klines,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        run_id="dca-run",
    )

    assert execution.failed_strategy_count == 0
    assert execution.results[0].metrics["strategy_type"] == "dca_long"
    assert execution.report_paths is not None
    assert execution.report_paths.html_report.exists()
    assert execution.report_paths.equity_curve_chart.exists()
    assert execution.report_paths.natural_language_summary.exists()
    with execution.report_paths.performance_summary_csv.open(encoding="utf-8", newline="") as file:
        row = next(csv.DictReader(file))
    assert row["total_entries"] == "1"
    assert row["exit_reason"] == "take_profit"
    with execution.report_paths.trade_log_csv.open(encoding="utf-8", newline="") as file:
        trade_rows = list(csv.DictReader(file))
    assert "entry_sequence" in trade_rows[0]
    assert "DCA long" in execution.report_paths.natural_language_summary.read_text(encoding="utf-8")


def test_single_mode_runs_only_one_cycle() -> None:
    result = _run(
        [
            _kline(0, open_price="100", high="104", low="100", close="103"),
            _kline(1, open_price="103", high="107", low="103", close="106"),
        ],
        _strategy(cycle_mode="single"),
    )

    assert result.metrics["total_cycles"] == 1
    assert result.metrics["cycle_mode"] == "single"


def test_repeat_mode_reopens_on_next_kline_and_not_same_kline() -> None:
    result = _run(
        [
            _kline(0, open_price="100", high="104", low="100", close="103"),
            _kline(1, open_price="103", high="103", low="103", close="103"),
            _kline(2, open_price="103", high="107", low="97.85", close="106"),
            _kline(3, open_price="104", high="104", low="104", close="104"),
            _kline(4, open_price="104", high="104", low="104", close="101"),
        ],
        _strategy(cycle_mode="repeat", capital_mode="rolling", dca_drop_percent=5, take_profit_percent=3),
    )

    buys = [trade for trade in result.trades if trade.side == "buy"]
    sells = [trade for trade in result.trades if trade.side == "sell"]
    assert result.metrics["total_cycles"] == 3
    assert result.metrics["take_profit_cycles"] == 2
    assert result.metrics["end_of_backtest_cycles"] == 1
    assert result.metrics["cycle_results"][1]["entries"] == 2
    assert buys[1].timestamp == datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    assert buys[1].price == pytest.approx(103.0)
    assert buys[2].price == pytest.approx(97.85)
    assert buys[3].timestamp == datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    assert not any(trade.side == "buy" and trade.timestamp == sells[0].timestamp and trade.metadata["cycle_id"] == 2 for trade in result.trades)
    assert len({point.timestamp for point in result.equity_curve}) == len(result.equity_curve)


def test_rolling_and_fixed_initial_cycle_capital_modes() -> None:
    klines = [
        _kline(0, open_price="100", high="104", low="100", close="103"),
        _kline(1, open_price="103", high="103", low="103", close="103"),
        _kline(2, open_price="103", high="107", low="103", close="106"),
    ]
    rolling = _run(klines, _strategy(cycle_mode="repeat", capital_mode="rolling"))
    fixed = _run(klines, _strategy(cycle_mode="repeat", capital_mode="fixed_initial"))

    rolling_second_initial = [trade for trade in rolling.trades if trade.side == "buy"][1]
    fixed_second_initial = [trade for trade in fixed.trades if trade.side == "buy"][1]
    assert rolling_second_initial.metadata["notional"] > 200
    assert fixed_second_initial.metadata["notional"] == pytest.approx(200)


def test_repeat_mode_stop_conditions_and_final_candle_no_reopen() -> None:
    result = _run(
        [
            _kline(0, open_price="100", high="104", low="100", close="103"),
            _kline(1, open_price="103", high="107", low="103", close="106"),
            _kline(2, open_price="106", high="110", low="106", close="109"),
        ],
        _strategy(cycle_mode="repeat", max_cycles=2),
    )
    assert result.metrics["total_cycles"] == 2
    assert result.metrics["account_stop_reason"] == "max_cycles"

    no_reopen = _run(
        [
            _kline(0, open_price="100", high="104", low="100", close="103"),
            _kline(1, open_price="103", high="107", low="103", close="106"),
        ],
        _strategy(cycle_mode="repeat"),
    )
    assert no_reopen.metrics["total_cycles"] == 1


def test_account_stop_loss_and_take_profit_stop_new_cycles() -> None:
    take_profit = _run(
        [
            _kline(0, open_price="100", high="104", low="100", close="103"),
            _kline(1, open_price="103", high="107", low="103", close="106"),
        ],
        _strategy(cycle_mode="repeat", account_take_profit_percent=0.001),
    )
    assert take_profit.metrics["total_cycles"] == 1
    assert take_profit.metrics["account_stop_reason"] == "account_take_profit"

    stop_loss = _run(
        [
            _kline(0, open_price="100", high="100", low="98", close="99"),
            _kline(1, open_price="99", high="103", low="99", close="102"),
        ],
        _strategy(cycle_mode="repeat", stop_loss_percent=1, account_stop_loss_percent=0.001),
    )
    assert stop_loss.metrics["total_cycles"] == 1
    assert stop_loss.metrics["account_stop_reason"] == "account_stop_loss"


def test_repeat_cycle_metadata_and_level_ids_reset_per_cycle() -> None:
    result = _run(
            [
                _kline(0, open_price="100", high="104", low="98.5", close="103"),
                _kline(1, open_price="100", high="104", low="98.5", close="103"),
                _kline(2, open_price="100", high="100", low="100", close="100"),
            ],
        _strategy(cycle_mode="repeat"),
    )

    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert buys[0].metadata["cycle_id"] == 1
    assert buys[0].metadata["global_trade_sequence"] == 1
    assert buys[1].metadata["unique_level_id"] == buys[3].metadata["unique_level_id"]
    assert buys[1].metadata["cycle_id"] != buys[3].metadata["cycle_id"]
    assert result.metrics["total_fees"] == pytest.approx(sum(trade.fee for trade in result.trades))
    assert result.final_equity == pytest.approx(result.metrics["capital_remaining"])
