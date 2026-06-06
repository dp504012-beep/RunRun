from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import csv
import json
import os

from src.exceptions import BacktesterError

SHEETS_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
CONFIG_SHEET = "回測設定"
SUMMARY_SHEET = "績效摘要"
EQUITY_SHEET = "淨值資料"
TRADES_SHEET = "網格交易紀錄"
DASHBOARD_SHEET = "儀表板"
DATA_SHEETS = (CONFIG_SHEET, SUMMARY_SHEET, EQUITY_SHEET, TRADES_SHEET)
MANAGED_SHEETS = (*DATA_SHEETS, DASHBOARD_SHEET)
MANAGED_CHART_TITLES = (
    "策略淨值曲線",
    "最終權益比較",
    "總報酬率比較",
    "最大回撤比較",
)


@dataclass(slots=True)
class GoogleSheetsExportResult:
    spreadsheet_id: str
    spreadsheet_url: str
    run_id: str
    updated_sheets: list[str]
    row_counts: dict[str, int]
    equity_sampling: str
    trade_log_truncated: bool
    dashboard_status: str
    chart_count: int
    chart_summaries: list[dict[str, str]]


class GoogleSheetsExportError(BacktesterError):
    """Raised when Google Sheets export fails."""


def export_local_report_to_google_sheets(
    *,
    run_dir: str | Path,
    spreadsheet_id: str,
    equity_sampling: str = "6h",
    max_trade_rows: int = 5000,
    create_charts: bool = True,
    credentials_path: str | None = None,
    service: Any | None = None,
) -> GoogleSheetsExportResult:
    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise GoogleSheetsExportError(f"run_dir does not exist: {run_dir}")
    if not run_dir.is_dir():
        raise GoogleSheetsExportError(f"run_dir is not a directory: {run_dir}")
    if max_trade_rows <= 0:
        raise GoogleSheetsExportError("max_trade_rows must be greater than 0")
    if equity_sampling not in {"1h", "6h", "1d"}:
        raise GoogleSheetsExportError(f"unsupported equity sampling: {equity_sampling}")

    report = _load_local_report(run_dir)
    sheets_service = service or _build_sheets_service(credentials_path=credentials_path)
    existing_sheets = _sheet_properties(_get_spreadsheet(sheets_service, spreadsheet_id))

    sheet_names = MANAGED_SHEETS if create_charts else DATA_SHEETS
    _ensure_sheets_exist(sheets_service, spreadsheet_id, sheet_names, existing_sheets)
    spreadsheet = _get_spreadsheet(sheets_service, spreadsheet_id)
    existing_sheets = _sheet_properties(spreadsheet)

    config_rows = _build_config_rows(report["config"])
    performance_rows, strategy_order, grid_strategy_ids = _build_performance_rows(report["performance_summary"])
    equity_rows = _build_equity_rows(report["equity_curve"], strategy_order, equity_sampling)
    trade_rows, trade_truncated = _build_trade_rows(report["trade_log"], grid_strategy_ids, max_trade_rows)
    dashboard_rows = _build_dashboard_rows(report, equity_sampling) if create_charts else []

    payloads = {
        CONFIG_SHEET: config_rows,
        SUMMARY_SHEET: performance_rows,
        EQUITY_SHEET: equity_rows,
        TRADES_SHEET: trade_rows,
    }
    if create_charts:
        payloads[DASHBOARD_SHEET] = dashboard_rows

    _clear_managed_sheets(sheets_service, spreadsheet_id, existing_sheets, payloads)
    _write_managed_sheets(sheets_service, spreadsheet_id, payloads)

    chart_summaries: list[dict[str, str]] = []
    if create_charts:
        chart_summaries = _update_dashboard_charts(
            sheets_service,
            spreadsheet_id,
            spreadsheet,
            existing_sheets,
            equity_rows,
            performance_rows,
        )

    updated_sheets = list(payloads)
    return GoogleSheetsExportResult(
        spreadsheet_id=spreadsheet_id,
        spreadsheet_url=_spreadsheet_url(spreadsheet_id),
        run_id=report["run_id"],
        updated_sheets=updated_sheets,
        row_counts={
            CONFIG_SHEET: max(len(config_rows) - 1, 0),
            SUMMARY_SHEET: max(len(performance_rows) - 1, 0),
            EQUITY_SHEET: max(len(equity_rows) - 1, 0),
            TRADES_SHEET: max(len(trade_rows) - 4, 0),
            DASHBOARD_SHEET: max(len(dashboard_rows), 0) if create_charts else 0,
        },
        equity_sampling=equity_sampling,
        trade_log_truncated=trade_truncated,
        dashboard_status="updated" if create_charts else "disabled",
        chart_count=len(chart_summaries),
        chart_summaries=chart_summaries,
    )


def _load_local_report(run_dir: Path) -> dict[str, Any]:
    config_path = _find_single_file(run_dir, "config_snapshot_*.json")
    performance_path = _find_single_file(run_dir, "performance_summary_*.csv")
    equity_path = _find_single_file(run_dir, "equity_curve_*.csv")
    trade_path = _find_single_file(run_dir, "trade_log_*.csv")
    warnings_path = next(iter(sorted(run_dir.glob("warnings_limitations_*.txt"))), None)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "run_id": str(config.get("_run_id") or config.get("run_id") or run_dir.name),
        "config": config,
        "performance_summary": _read_csv_rows(performance_path),
        "equity_curve": _read_csv_rows(equity_path),
        "trade_log": _read_csv_rows(trade_path),
        "warnings": warnings_path.read_text(encoding="utf-8").splitlines() if warnings_path else [],
    }


def _find_single_file(run_dir: Path, pattern: str) -> Path:
    matches = sorted(run_dir.glob(pattern))
    if not matches:
        raise GoogleSheetsExportError(f"missing required report file: {pattern}")
    if len(matches) > 1:
        raise GoogleSheetsExportError(f"multiple files match pattern {pattern}: {', '.join(str(path) for path in matches)}")
    return matches[0]


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def _build_config_rows(config: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = [["field", "value"]]
    for key, value in _flatten_items(config):
        rows.append([key, value])
    return rows


def _build_performance_rows(rows: list[dict[str, str]]) -> tuple[list[list[Any]], list[str], set[str]]:
    headers = list(rows[0].keys()) if rows else []
    matrix: list[list[Any]] = [headers]
    strategy_order = [row.get("strategy_id", "") for row in rows if row.get("strategy_id")]
    grid_ids = {row["strategy_id"] for row in rows if "grid" in row.get("strategy_type", "").lower() and row.get("strategy_id")}
    for row in rows:
        matrix.append([row.get(header, "") for header in headers])
    return matrix, strategy_order, grid_ids


def _build_equity_rows(rows: list[dict[str, str]], strategy_order: list[str], sampling: str) -> list[list[Any]]:
    if not rows:
        return [["timestamp", *strategy_order]]

    parsed: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        strategy_id = row.get("strategy_id", "")
        if not strategy_id:
            continue
        timestamp = _parse_iso_timestamp(row.get("timestamp", ""))
        equity = float(row.get("equity") or 0.0)
        parsed.setdefault(strategy_id, []).append((timestamp, equity))

    sampled: dict[str, dict[datetime, float]] = {}
    all_timestamps: set[datetime] = set()
    for strategy_id, points in parsed.items():
        selected = _sample_points(points, sampling)
        sampled[strategy_id] = {timestamp: equity for timestamp, equity in selected}
        all_timestamps.update(timestamp for timestamp, _ in selected)

    matrix: list[list[Any]] = [["timestamp", *strategy_order]]
    for timestamp in sorted(all_timestamps):
        matrix.append([_iso(timestamp), *[sampled.get(strategy_id, {}).get(timestamp, "") for strategy_id in strategy_order]])
    return matrix


def _build_trade_rows(rows: list[dict[str, str]], grid_strategy_ids: set[str], max_trade_rows: int) -> tuple[list[list[Any]], bool]:
    headers = list(rows[0].keys()) if rows else []
    filtered = [row for row in rows if row.get("strategy_id", "") in grid_strategy_ids] if grid_strategy_ids else []
    truncated = len(filtered) > max_trade_rows
    matrix: list[list[Any]] = [
        ["truncated", str(truncated)],
        ["max_trade_rows", max_trade_rows],
        ["status", "truncated" if truncated else "complete"],
        headers,
    ]
    for row in filtered[:max_trade_rows]:
        matrix.append([row.get(header, "") for header in headers])
    return matrix, truncated


def _build_dashboard_rows(report: dict[str, Any], equity_sampling: str) -> list[list[Any]]:
    config = report["config"]
    market = config.get("market", {})
    rows: list[list[Any]] = [
        ["field", "value"],
        ["run_id", report["run_id"]],
        ["period", f"{market.get('start', '')} -> {market.get('end', '')}"],
        ["symbol", market.get("symbol", "")],
        ["interval", market.get("interval", "")],
        ["data_source", market.get("source", "")],
        ["equity_sampling", equity_sampling],
        ["created_at", config.get("_created_at", "")],
        ["warnings_or_limitations", " | ".join(item for item in report.get("warnings", []) if item.strip())],
        [],
        ["strategy_id", "initial_capital", "status", "liquidated"],
    ]
    for row in report["performance_summary"]:
        rows.append([
            row.get("strategy_id", ""),
            row.get("initial_capital", ""),
            row.get("status", ""),
            row.get("liquidated", ""),
        ])
    return rows


def _update_dashboard_charts(
    service: Any,
    spreadsheet_id: str,
    spreadsheet: dict[str, Any],
    sheets: dict[str, dict[str, Any]],
    equity_rows: list[list[Any]],
    performance_rows: list[list[Any]],
) -> list[dict[str, str]]:
    dashboard_id = int(sheets[DASHBOARD_SHEET]["sheetId"])
    equity_id = int(sheets[EQUITY_SHEET]["sheetId"])
    summary_id = int(sheets[SUMMARY_SHEET]["sheetId"])
    delete_requests = _managed_chart_delete_requests(spreadsheet)
    chart_requests, chart_summaries = _chart_requests(dashboard_id, equity_id, summary_id, equity_rows, performance_rows)
    requests = [*delete_requests, *chart_requests]
    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    return chart_summaries


def _managed_chart_delete_requests(spreadsheet: dict[str, Any]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for sheet in spreadsheet.get("sheets", []):
        for chart in sheet.get("charts", []):
            spec = chart.get("spec", {})
            title = spec.get("title")
            chart_id = chart.get("chartId")
            if title in MANAGED_CHART_TITLES and chart_id is not None:
                requests.append({"deleteEmbeddedObject": {"objectId": chart_id}})
    return requests


def _chart_requests(
    dashboard_id: int,
    equity_id: int,
    summary_id: int,
    equity_rows: list[list[Any]],
    performance_rows: list[list[Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    requests: list[dict[str, Any]] = []
    summaries: list[dict[str, str]] = []
    equity_cols = _non_empty_equity_columns(equity_rows)
    equity_row_count = len(equity_rows)
    if equity_row_count > 1 and equity_cols:
        ranges = [_source_range(equity_id, 0, equity_row_count, 0, 1)]
        ranges.extend(_source_range(equity_id, 0, equity_row_count, col, col + 1) for col in equity_cols)
        requests.append(_add_chart_request("策略淨值曲線", "LINE", dashboard_id, 7, 0, ranges))
        summaries.append({"name": "策略淨值曲線", "type": "LINE"})

    success_rows = _successful_summary_row_indexes(performance_rows)
    for title, column, chart_type, row_anchor, number_format in [
        ("最終權益比較", "final_equity", "COLUMN", 7, None),
        ("總報酬率比較", "total_return", "COLUMN", 24, "PERCENT"),
        ("最大回撤比較", "max_drawdown", "COLUMN", 41, "PERCENT"),
    ]:
        col_index = _header_index(performance_rows, column)
        strategy_col = _header_index(performance_rows, "strategy_id")
        if col_index is None or strategy_col is None or not success_rows:
            continue
        ranges = [
            _contiguous_range_or_full(summary_id, performance_rows, success_rows, strategy_col),
            _contiguous_range_or_full(summary_id, performance_rows, success_rows, col_index),
        ]
        requests.append(_add_chart_request(title, chart_type, dashboard_id, row_anchor, 9, ranges, number_format=number_format))
        summaries.append({"name": title, "type": chart_type})
    return requests, summaries


def _non_empty_equity_columns(equity_rows: list[list[Any]]) -> list[int]:
    if not equity_rows:
        return []
    result = []
    for col in range(1, len(equity_rows[0])):
        if any(len(row) > col and row[col] not in ("", None) for row in equity_rows[1:]):
            result.append(col)
    return result


def _successful_summary_row_indexes(rows: list[list[Any]]) -> list[int]:
    status_col = _header_index(rows, "status")
    if status_col is None:
        return []
    return [idx for idx, row in enumerate(rows[1:], start=1) if len(row) > status_col and str(row[status_col]).lower() != "failed"]


def _header_index(rows: list[list[Any]], header: str) -> int | None:
    if not rows:
        return None
    try:
        return [str(item) for item in rows[0]].index(header)
    except ValueError:
        return None


def _contiguous_range_or_full(sheet_id: int, rows: list[list[Any]], row_indexes: list[int], col: int) -> dict[str, Any]:
    start = min(row_indexes)
    end = max(row_indexes) + 1
    return _source_range(sheet_id, 0, end, col, col + 1) if start == 1 else _source_range(sheet_id, start, end, col, col + 1)


def _source_range(sheet_id: int, start_row: int, end_row: int, start_col: int, end_col: int) -> dict[str, Any]:
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "endRowIndex": end_row,
        "startColumnIndex": start_col,
        "endColumnIndex": end_col,
    }


def _add_chart_request(
    title: str,
    chart_type: str,
    dashboard_id: int,
    row_anchor: int,
    col_anchor: int,
    ranges: list[dict[str, Any]],
    *,
    number_format: str | None = None,
) -> dict[str, Any]:
    axis = [
        {"position": "BOTTOM_AXIS", "title": "timestamp" if chart_type == "LINE" else "strategy_id"},
        {"position": "LEFT_AXIS", "title": title},
    ]
    if number_format:
        axis[1]["format"] = {"numberFormat": {"type": number_format, "pattern": "0.00%"}}
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "basicChart": {
                        "chartType": chart_type,
                        "legendPosition": "RIGHT_LEGEND",
                        "axis": axis,
                        "domains": [{"domain": {"sourceRange": {"sources": [ranges[0]]}}}],
                        "series": [{"series": {"sourceRange": {"sources": [item]}}, "targetAxis": "LEFT_AXIS"} for item in ranges[1:]],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": dashboard_id, "rowIndex": row_anchor, "columnIndex": col_anchor},
                        "offsetXPixels": 0,
                        "offsetYPixels": 0,
                        "widthPixels": 640 if chart_type == "LINE" else 480,
                        "heightPixels": 300,
                    }
                },
            }
        }
    }


def _sample_points(points: list[tuple[datetime, float]], sampling: str) -> list[tuple[datetime, float]]:
    if sampling == "1h":
        return _dedupe_points(points, timedelta(hours=1))
    if sampling == "6h":
        return _dedupe_points(points, timedelta(hours=6))
    if sampling == "1d":
        return _dedupe_points(points, timedelta(days=1))
    raise GoogleSheetsExportError(f"unsupported equity sampling: {sampling}")


def _dedupe_points(points: list[tuple[datetime, float]], bucket: timedelta) -> list[tuple[datetime, float]]:
    sorted_points = sorted(points, key=lambda item: item[0])
    selected: dict[datetime, tuple[datetime, float]] = {}
    for timestamp, equity in sorted_points:
        selected[_bucket_start(timestamp, bucket)] = (timestamp, equity)
    return [selected[key] for key in sorted(selected)]


def _bucket_start(timestamp: datetime, bucket: timedelta) -> datetime:
    timestamp = _ensure_utc(timestamp)
    if bucket == timedelta(hours=1):
        return timestamp.replace(minute=0, second=0, microsecond=0)
    if bucket == timedelta(hours=6):
        return timestamp.replace(hour=(timestamp.hour // 6) * 6, minute=0, second=0, microsecond=0)
    if bucket == timedelta(days=1):
        return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return timestamp


def _ensure_sheets_exist(service: Any, spreadsheet_id: str, sheet_names: tuple[str, ...], existing_sheets: dict[str, dict[str, Any]]) -> None:
    requests = [{"addSheet": {"properties": {"title": name}}} for name in sheet_names if name not in existing_sheets]
    if requests:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


def _clear_managed_sheets(service: Any, spreadsheet_id: str, sheets: dict[str, dict[str, Any]], payloads: dict[str, list[list[Any]]]) -> None:
    ranges = []
    for sheet_name, values in payloads.items():
        grid = sheets.get(sheet_name, {}).get("gridProperties", {})
        row_count = int(grid.get("rowCount") or max(len(values) + 10, 1000))
        col_count = int(grid.get("columnCount") or max(len(values[0]) if values else 1, 26))
        ranges.append(f"{_quote_sheet_name(sheet_name)}!A1:{_column_letter(col_count)}{row_count}")
    service.spreadsheets().values().batchClear(spreadsheetId=spreadsheet_id, body={"ranges": ranges}).execute()


def _write_managed_sheets(service: Any, spreadsheet_id: str, payloads: dict[str, list[list[Any]]]) -> None:
    data = [{"range": f"{_quote_sheet_name(name)}!A1", "majorDimension": "ROWS", "values": values} for name, values in payloads.items()]
    service.spreadsheets().values().batchUpdate(spreadsheetId=spreadsheet_id, body={"valueInputOption": "RAW", "data": data}).execute()


def _get_spreadsheet(service: Any, spreadsheet_id: str) -> dict[str, Any]:
    return service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="spreadsheetId,properties.title,sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),charts(chartId,spec(title)))",
    ).execute()


def _sheet_properties(spreadsheet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(sheet["properties"]["title"]): sheet["properties"] for sheet in spreadsheet.get("sheets", []) if sheet.get("properties", {}).get("title")}


def _build_sheets_service(*, credentials_path: str | None = None) -> Any:
    resolved = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not resolved:
        raise GoogleSheetsExportError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GoogleSheetsExportError("google sheets client dependencies are not installed") from exc
    credentials = service_account.Credentials.from_service_account_file(resolved, scopes=SHEETS_SCOPES)
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _flatten_items(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [item for key, child in value.items() for item in _flatten_items(child, f"{prefix}.{key}" if prefix else str(key))]
    if isinstance(value, list):
        return [item for index, child in enumerate(value) for item in _flatten_items(child, f"{prefix}[{index}]")]
    return [(prefix, _format_value(value))]


def _format_value(value: Any) -> Any:
    return _iso(value) if isinstance(value, datetime) else value


def _parse_iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _quote_sheet_name(name: str) -> str:
    return f"'{name.replace(chr(39), chr(39) * 2)}'"


def _column_letter(index: int) -> str:
    if index < 1:
        return "A"
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _spreadsheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
