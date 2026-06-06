from __future__ import annotations

import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src import cli
from src.outputs import google_sheets as gs


class FakeRequest:
    def __init__(self, callback):
        self._callback = callback

    def execute(self):
        return self._callback()


class FakeSheetsService:
    def __init__(self, sheet_titles: list[str] | None = None, charts: list[dict[str, object]] | None = None):
        self.calls: list[tuple[str, object]] = []
        self.sheet_titles = sheet_titles or []
        self.sheet_grid = {
            title: {"title": title, "sheetId": index + 1, "gridProperties": {"rowCount": 1000, "columnCount": 26}}
            for index, title in enumerate(self.sheet_titles)
        }
        self.charts = charts or []
        self.written_values: dict[str, list[list[object]]] = {}
        self.added_titles: list[str] = []
        self.deleted_chart_ids: list[int] = []
        self.added_charts: list[dict[str, object]] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))

        def callback():
            sheets = [{"properties": props} for props in self.sheet_grid.values()]
            if gs.DASHBOARD_SHEET in self.sheet_grid:
                sheets.append({"properties": self.sheet_grid[gs.DASHBOARD_SHEET], "charts": self.charts})
            return {"spreadsheetId": kwargs["spreadsheetId"], "properties": {"title": "Export"}, "sheets": sheets}

        return FakeRequest(callback)

    def batchClear(self, **kwargs):
        self.calls.append(("batchClear", kwargs))
        return FakeRequest(lambda: {"clearedRanges": kwargs.get("body", {}).get("ranges", [])})

    def batchUpdate(self, **kwargs):
        self.calls.append(("batchUpdate", kwargs))
        body = kwargs.get("body", {})
        requests = body.get("requests")
        if requests is None:
            for item in body.get("data", []):
                self.written_values[item["range"]] = item["values"]
            return FakeRequest(lambda: {"totalUpdatedRanges": len(body.get("data", []))})

        for request in requests:
            if "addSheet" in request:
                title = request["addSheet"]["properties"]["title"]
                if title not in self.sheet_grid:
                    self.added_titles.append(title)
                    self.sheet_titles.append(title)
                    self.sheet_grid[title] = {
                        "title": title,
                        "sheetId": len(self.sheet_grid) + 1,
                        "gridProperties": {"rowCount": 1000, "columnCount": 26},
                    }
            if "deleteEmbeddedObject" in request:
                chart_id = request["deleteEmbeddedObject"]["objectId"]
                self.deleted_chart_ids.append(chart_id)
                self.charts = [chart for chart in self.charts if chart.get("chartId") != chart_id]
            if "addChart" in request:
                chart = request["addChart"]["chart"]
                chart_id = 1000 + len(self.added_charts)
                stored = {"chartId": chart_id, "spec": chart["spec"]}
                self.added_charts.append(chart)
                self.charts.append(stored)
        return FakeRequest(lambda: {"replies": [{} for _ in requests]})


class FailingChartService(FakeSheetsService):
    def batchUpdate(self, **kwargs):
        requests = kwargs.get("body", {}).get("requests")
        if requests and any("addChart" in request for request in requests):
            return FakeRequest(lambda: (_ for _ in ()).throw(gs.GoogleSheetsExportError("chart api failed")))
        return super().batchUpdate(**kwargs)


def _write_report_files(run_dir: Path, *, grid_trade_count: int = 5, include_failed: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "market": {
            "source": "binance",
            "symbol": "BTCUSDT",
            "interval": "1h",
            "start": "2024-06-05T00:00:00Z",
            "end": "2024-06-06T00:00:00Z",
        },
        "common": {"fee_rate": 0.001, "slippage_rate": 0.0},
        "strategies": [
            {"id": "spot", "type": "buy_and_hold", "initial_capital": 1000},
            {"id": "grid", "type": "long_grid", "initial_capital": 1000},
        ],
        "output": {"local_report": True},
    }
    (run_dir / "config_snapshot_run-001.json").write_text(
        json.dumps({**config, "_run_id": "run-001", "_created_at": "2024-06-06T00:00:00Z"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "warnings_limitations_run-001.txt").write_text("Warnings:\n- checked\nModel limitations:\n- simplified\n", encoding="utf-8")
    fields = [
        "strategy_id",
        "status",
        "error",
        "strategy_type",
        "initial_capital",
        "final_equity",
        "net_profit",
        "total_return",
        "annualized_return",
        "max_drawdown",
        "total_fees",
        "trade_count",
        "liquidated",
        "liquidation_time",
    ]
    with (run_dir / "performance_summary_run-001.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"strategy_id": "spot", "status": "success", "error": "", "strategy_type": "buy_and_hold", "initial_capital": 1000, "final_equity": 1200, "net_profit": 200, "total_return": 0.2, "annualized_return": 0.3, "max_drawdown": 0.1, "total_fees": 2, "trade_count": 1, "liquidated": False, "liquidation_time": ""})
        writer.writerow({"strategy_id": "grid", "status": "success", "error": "", "strategy_type": "long_grid", "initial_capital": 1000, "final_equity": 1100, "net_profit": 100, "total_return": 0.1, "annualized_return": 0.15, "max_drawdown": 0.2, "total_fees": 3, "trade_count": grid_trade_count, "liquidated": False, "liquidation_time": ""})
        if include_failed:
            writer.writerow({"strategy_id": "failed", "status": "failed", "error": "boom", "strategy_type": "long_grid", "initial_capital": 1000, "final_equity": "", "net_profit": "", "total_return": "", "annualized_return": "", "max_drawdown": "", "total_fees": "", "trade_count": "", "liquidated": "", "liquidation_time": ""})
    with (run_dir / "equity_curve_run-001.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["strategy_id", "timestamp", "equity"])
        for hour in range(8):
            writer.writerow(["spot", f"2024-06-05T{hour:02d}:00:00Z", 1000 + hour * 10])
            writer.writerow(["grid", f"2024-06-05T{hour:02d}:00:00Z", 1000 + hour * 5])
    with (run_dir / "trade_log_run-001.csv").open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["strategy_id", "timestamp", "symbol", "side", "quantity", "price", "fee", "pnl", "notes"])
        for index in range(grid_trade_count):
            writer.writerow(["grid", f"2024-06-05T{index:02d}:00:00Z", "BTCUSDT", "buy", 0.1, 100 + index, 0.1, 0.0, "grid trade"])
        writer.writerow(["spot", "2024-06-05T00:00:00Z", "BTCUSDT", "buy", 1.0, 100, 0.1, 0.0, "spot trade"])


def test_export_sheets_help_executes() -> None:
    result = subprocess.run([sys.executable, "-m", "src.cli", "export-sheets", "--help"], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "--run-dir" in result.stdout
    assert "--spreadsheet-id" in result.stdout
    assert "--no-create-charts" in result.stdout


def test_export_sheets_creates_dashboard_and_four_charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir, grid_trade_count=5, include_failed=True)
    service = FakeSheetsService(list(gs.DATA_SHEETS) + ["user sheet"])
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: service)

    exit_code = cli.main(["export-sheets", "--run-dir", str(run_dir), "--spreadsheet-id", "sheet-001", "--max-trade-rows", "3"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert gs.DASHBOARD_SHEET in service.added_titles
    assert len(service.added_charts) == 4
    assert "dashboard: updated" in captured.out
    assert "chart_count: 4" in captured.out
    assert "chart: 策略淨值曲線 (LINE)" in captured.out
    assert "user sheet" in service.sheet_titles
    assert service.written_values[f"'{gs.DASHBOARD_SHEET}'!A1"][1] == ["run_id", "run-001"]
    assert service.written_values[f"'{gs.TRADES_SHEET}'!A1"][2] == ["status", "truncated"]


def test_export_sheets_existing_dashboard_replaces_managed_charts_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir)
    charts = [
        {"chartId": 10, "spec": {"title": "策略淨值曲線"}},
        {"chartId": 11, "spec": {"title": "user chart"}},
    ]
    service = FakeSheetsService(list(gs.MANAGED_SHEETS), charts=charts)
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: service)

    first = gs.export_local_report_to_google_sheets(run_dir=run_dir, spreadsheet_id="sheet-001", service=service)
    second = gs.export_local_report_to_google_sheets(run_dir=run_dir, spreadsheet_id="sheet-001", service=service)

    assert first.chart_count == 4
    assert second.chart_count == 4
    assert 10 in service.deleted_chart_ids
    assert 11 not in service.deleted_chart_ids
    assert any(chart.get("spec", {}).get("title") == "user chart" for chart in service.charts)


def test_chart_ranges_skip_blank_tail_and_failed_strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir, include_failed=True)
    service = FakeSheetsService(list(gs.MANAGED_SHEETS))
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: service)

    gs.export_local_report_to_google_sheets(run_dir=run_dir, spreadsheet_id="sheet-001", service=service)

    equity_chart = next(chart for chart in service.added_charts if chart["spec"]["title"] == "策略淨值曲線")
    equity_range = equity_chart["spec"]["basicChart"]["domains"][0]["domain"]["sourceRange"]["sources"][0]
    assert equity_range["endRowIndex"] == 3

    final_chart = next(chart for chart in service.added_charts if chart["spec"]["title"] == "最終權益比較")
    final_ranges = final_chart["spec"]["basicChart"]["series"][0]["series"]["sourceRange"]["sources"]
    assert final_ranges[0]["endRowIndex"] == 3


def test_percent_charts_use_percent_axis_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir)
    service = FakeSheetsService(list(gs.MANAGED_SHEETS))
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: service)

    gs.export_local_report_to_google_sheets(run_dir=run_dir, spreadsheet_id="sheet-001", service=service)

    for title in ("總報酬率比較", "最大回撤比較"):
        chart = next(item for item in service.added_charts if item["spec"]["title"] == title)
        axis = chart["spec"]["basicChart"]["axis"][1]
        assert axis["format"]["numberFormat"]["type"] == "PERCENT"
        assert axis["format"]["numberFormat"]["pattern"] == "0.00%"


def test_export_sheets_no_create_charts_skips_dashboard_and_charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir)
    service = FakeSheetsService(list(gs.DATA_SHEETS))
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: service)

    exit_code = cli.main(["export-sheets", "--run-dir", str(run_dir), "--spreadsheet-id", "sheet-001", "--no-create-charts"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert gs.DASHBOARD_SHEET not in service.added_titles
    assert service.added_charts == []
    assert "dashboard: disabled" in captured.out
    assert "chart_count: 0" in captured.out


def test_export_sheets_chart_api_failure_returns_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir)
    monkeypatch.setattr(gs, "_build_sheets_service", lambda **kwargs: FailingChartService(list(gs.MANAGED_SHEETS)))

    exit_code = cli.main(["export-sheets", "--run-dir", str(run_dir), "--spreadsheet-id", "sheet-001"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "chart api failed" in captured.err


def test_export_sheets_missing_run_dir_returns_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["export-sheets", "--run-dir", "C:/no/such/run", "--spreadsheet-id", "sheet-001"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "run_dir does not exist" in captured.err


def test_export_sheets_missing_credentials_returns_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    _write_report_files(run_dir)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    exit_code = cli.main(["export-sheets", "--run-dir", str(run_dir), "--spreadsheet-id", "sheet-001"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "GOOGLE_APPLICATION_CREDENTIALS is not set" in captured.err


def test_export_sheets_missing_report_file_returns_non_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = tmp_path / "reports" / "run-001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config_snapshot_run-001.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    exit_code = cli.main(["export-sheets", "--run-dir", str(run_dir), "--spreadsheet-id", "sheet-001"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "missing required report file" in captured.err
