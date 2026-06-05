from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any, Sequence
import csv
import json

from src.backtest_config import BacktestConfig, BacktestResult, EquityPoint, TradeRecord


DEFAULT_MODEL_LIMITATIONS = [
    "本地報告只反映已輸入的回測結果，不重新計算策略。",
    "最後一根 K 線是否賣出取決於策略結果，不由報告層假設。",
    "圖表與 CSV 使用同一批 BacktestResult 資料生成。",
]


@dataclass(slots=True)
class LocalReportPaths:
    run_id: str
    report_dir: Path
    config_snapshot: Path
    performance_summary_csv: Path
    equity_curve_csv: Path
    trade_log_csv: Path
    html_report: Path
    equity_curve_chart: Path
    return_comparison_chart: Path
    max_drawdown_comparison_chart: Path
    warnings_and_limitations: Path


def generate_local_report(
    *,
    config: BacktestConfig,
    results: Sequence[BacktestResult],
    output_dir: str | Path = Path("reports"),
    run_id: str | None = None,
    warnings: Sequence[str] | None = None,
    model_limitations: Sequence[str] | None = None,
    created_at: datetime | None = None,
    overwrite: bool = False,
) -> LocalReportPaths:
    if not results:
        raise ValueError("results 不可為空。")

    created_at = _ensure_utc(created_at or datetime.now(UTC))
    run_id = run_id or created_at.strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(output_dir) / run_id
    if report_dir.exists() and not overwrite:
        raise FileExistsError(f"報告已存在: {report_dir}")
    report_dir.mkdir(parents=True, exist_ok=True)

    paths = LocalReportPaths(
        run_id=run_id,
        report_dir=report_dir,
        config_snapshot=report_dir / f"config_snapshot_{run_id}.json",
        performance_summary_csv=report_dir / f"performance_summary_{run_id}.csv",
        equity_curve_csv=report_dir / f"equity_curve_{run_id}.csv",
        trade_log_csv=report_dir / f"trade_log_{run_id}.csv",
        html_report=report_dir / f"backtest_report_{run_id}.html",
        equity_curve_chart=report_dir / f"equity_curve_{run_id}.svg",
        return_comparison_chart=report_dir / f"return_comparison_{run_id}.svg",
        max_drawdown_comparison_chart=report_dir / f"max_drawdown_comparison_{run_id}.svg",
        warnings_and_limitations=report_dir / f"warnings_limitations_{run_id}.txt",
    )

    summary_rows = [_summary_row(result) for result in results]
    _write_json(paths.config_snapshot, _to_jsonable(config))
    _write_performance_summary(paths.performance_summary_csv, summary_rows)
    _write_equity_curve(paths.equity_curve_csv, results)
    _write_trade_log(paths.trade_log_csv, results)
    _write_warnings_and_limitations(paths.warnings_and_limitations, warnings or [], model_limitations or DEFAULT_MODEL_LIMITATIONS)
    _write_equity_curve_chart(paths.equity_curve_chart, results)
    _write_comparison_chart(paths.return_comparison_chart, "Total Return", [(row["strategy_id"], row["total_return"]) for row in summary_rows])
    _write_comparison_chart(paths.max_drawdown_comparison_chart, "Max Drawdown", [(row["strategy_id"], row["max_drawdown"]) for row in summary_rows])
    _write_html_report(
        paths.html_report,
        config=config,
        results=results,
        paths=paths,
        summary_rows=summary_rows,
        warnings=warnings or [],
        model_limitations=model_limitations or DEFAULT_MODEL_LIMITATIONS,
    )
    return paths


def _summary_row(result: BacktestResult) -> dict[str, Any]:
    performance = result.metrics.get("performance_summary", {})
    strategy_id = _strategy_id(result)
    return {
        "strategy_id": strategy_id,
        "strategy_type": result.metrics.get("strategy_type", ""),
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "net_profit": performance.get("net_profit"),
        "total_return": result.total_return,
        "annualized_return": performance.get("annualized_return"),
        "max_drawdown": result.max_drawdown,
        "minimum_equity": performance.get("minimum_equity"),
        "total_fees": performance.get("total_fees", result.metrics.get("total_fees")),
        "trade_count": performance.get("trade_count", len(result.trades)),
        "btc_quantity": result.metrics.get("btc_quantity"),
        "estimated_exit_fee": result.metrics.get("estimated_exit_fee"),
        "estimated_exit_net_equity": result.metrics.get("estimated_exit_net_equity"),
    }


def _write_performance_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_equity_curve(path: Path, results: Sequence[BacktestResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["strategy_id", "timestamp", "equity"])
        writer.writeheader()
        for result in results:
            strategy_id = _strategy_id(result)
            for point in result.equity_curve:
                writer.writerow({"strategy_id": strategy_id, "timestamp": _iso(point.timestamp), "equity": point.equity})


def _write_trade_log(path: Path, results: Sequence[BacktestResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["strategy_id", "timestamp", "symbol", "side", "quantity", "price", "fee", "pnl", "notes"])
        writer.writeheader()
        for result in results:
            for trade in result.trades:
                writer.writerow(_trade_row(trade))


def _write_warnings_and_limitations(path: Path, warnings: Sequence[str], model_limitations: Sequence[str]) -> None:
    lines = ["Warnings:"]
    lines.extend(f"- {item}" for item in warnings)
    lines.append("Model limitations:")
    lines.extend(f"- {item}" for item in model_limitations)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_html_report(
    path: Path,
    *,
    config: BacktestConfig,
    results: Sequence[BacktestResult],
    paths: LocalReportPaths,
    summary_rows: list[dict[str, Any]],
    warnings: Sequence[str],
    model_limitations: Sequence[str],
) -> None:
    rows_html = "".join(
        "<tr>"
        + "".join(f"<td>{escape(_format_cell(row[field]))}</td>" for field in summary_rows[0])
        + "</tr>"
        for row in summary_rows
    )
    headers_html = "".join(f"<th>{escape(field)}</th>" for field in summary_rows[0])
    warnings_html = _list_html(warnings)
    limitations_html = _list_html(model_limitations)
    assumptions_html = _list_html(_collect_assumptions(results))

    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>Backtest Report {escape(paths.run_id)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    img {{ display: block; max-width: 100%; margin: 16px 0; border: 1px solid #d1d5db; }}
    code {{ background: #f3f4f6; padding: 2px 4px; }}
  </style>
</head>
<body>
  <h1>Backtest Report {escape(paths.run_id)}</h1>
  <p>Data source: <strong>{escape(config.market.source)}</strong></p>
  <p>Data period: <strong>{escape(_iso(config.market.start))}</strong> to <strong>{escape(_iso(config.market.end))}</strong></p>
  <p>Symbol / interval: <strong>{escape(config.market.symbol)} / {escape(config.market.interval)}</strong></p>

  <h2>Performance Summary</h2>
  <table><thead><tr>{headers_html}</tr></thead><tbody>{rows_html}</tbody></table>

  <h2>Charts</h2>
  <img src="{escape(paths.equity_curve_chart.name)}" alt="Equity curve">
  <img src="{escape(paths.return_comparison_chart.name)}" alt="Return comparison">
  <img src="{escape(paths.max_drawdown_comparison_chart.name)}" alt="Max drawdown comparison">

  <h2>Files</h2>
  <ul>
    <li><code>{escape(paths.config_snapshot.name)}</code></li>
    <li><code>{escape(paths.performance_summary_csv.name)}</code></li>
    <li><code>{escape(paths.equity_curve_csv.name)}</code></li>
    <li><code>{escape(paths.trade_log_csv.name)}</code></li>
  </ul>

  <h2>Warnings</h2>
  {warnings_html}
  <h2>Model Limitations</h2>
  {limitations_html}
  <h2>Strategy Assumptions</h2>
  {assumptions_html}
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def _write_equity_curve_chart(path: Path, results: Sequence[BacktestResult]) -> None:
    series = [(_strategy_id(result), result.equity_curve) for result in results]
    values = [point.equity for _, points in series for point in points]
    path.write_text(_line_chart_svg("Equity Curve", series, values), encoding="utf-8")


def _write_comparison_chart(path: Path, title: str, values: list[tuple[str, Any]]) -> None:
    numeric_values = [(label, float(value or 0.0)) for label, value in values]
    path.write_text(_bar_chart_svg(title, numeric_values), encoding="utf-8")


def _line_chart_svg(title: str, series: list[tuple[str, list[EquityPoint]]], values: list[float]) -> str:
    width, height, pad = 840, 360, 48
    min_value, max_value = min(values), max(values)
    span = max(max_value - min_value, 1.0)
    colors = ["#2563eb", "#dc2626", "#059669", "#9333ea"]
    polylines = []
    for index, (label, points) in enumerate(series):
        if len(points) == 1:
            x_values = [pad]
        else:
            x_values = [pad + i * (width - 2 * pad) / (len(points) - 1) for i in range(len(points))]
        coordinates = [
            f"{x:.2f},{height - pad - ((point.equity - min_value) / span) * (height - 2 * pad):.2f}"
            for x, point in zip(x_values, points)
        ]
        polylines.append(f'<polyline fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2" points="{" ".join(coordinates)}"/>')
        polylines.append(f'<text x="{pad}" y="{24 + index * 18}" font-size="12" fill="{colors[index % len(colors)]}">{escape(label)}</text>')
    return _svg_shell(width, height, title, "".join(polylines))


def _bar_chart_svg(title: str, values: list[tuple[str, float]]) -> str:
    width, height, pad = 840, 360, 48
    raw_values = [value for _, value in values] or [0.0]
    min_value = min(min(raw_values), 0.0)
    max_value = max(max(raw_values), 0.0)
    span = max(max_value - min_value, 1.0)
    bar_width = (width - 2 * pad) / max(len(values), 1)
    chart_height = height - 2 * pad
    zero_y = height - pad - ((0.0 - min_value) / span) * chart_height
    bars = []
    for index, (label, value) in enumerate(values):
        x = pad + index * bar_width + 8
        value_y = height - pad - ((value - min_value) / span) * chart_height
        y = min(value_y, zero_y)
        bar_height = abs(value_y - zero_y)
        color = "#059669" if value >= 0 else "#dc2626"
        bars.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width - 16:.2f}" height="{bar_height:.2f}" fill="{color}"/>')
        bars.append(f'<text x="{x:.2f}" y="{height - 18}" font-size="12">{escape(label)}</text>')
        bars.append(f'<text x="{x:.2f}" y="{max(y - 6, 16):.2f}" font-size="12">{value:.4f}</text>')
    return _svg_shell(width, height, title, f'<line x1="{pad}" y1="{zero_y:.2f}" x2="{width - pad}" y2="{zero_y:.2f}" stroke="#9ca3af"/>' + "".join(bars))


def _svg_shell(width: int, height: int, title: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#ffffff"/>
<text x="24" y="24" font-size="18" font-family="Arial" fill="#111827">{escape(title)}</text>
<line x1="48" y1="{height - 48}" x2="{width - 24}" y2="{height - 48}" stroke="#9ca3af"/>
{body}
</svg>
"""


def _list_html(items: Sequence[str]) -> str:
    if not items:
        return "<p>None</p>"
    return "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _collect_assumptions(results: Sequence[BacktestResult]) -> list[str]:
    assumptions: list[str] = []
    for result in results:
        strategy_id = _strategy_id(result)
        for item in result.metrics.get("assumptions", []):
            assumptions.append(f"{strategy_id}: {item}")
    return assumptions


def _strategy_id(result: BacktestResult) -> str:
    if result.trades:
        return result.trades[0].strategy_id
    return str(result.metrics.get("strategy_id", "unknown"))


def _trade_row(trade: TradeRecord) -> dict[str, Any]:
    return {
        "strategy_id": trade.strategy_id,
        "timestamp": _iso(trade.timestamp),
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "price": trade.price,
        "fee": trade.fee,
        "pnl": trade.pnl,
        "notes": trade.notes or "",
    }


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if is_dataclass(value):
        return {key: _to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def _iso(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
