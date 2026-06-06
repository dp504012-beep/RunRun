---
name: run-local-crypto-backtest
description: Turn natural-language crypto backtest requests into the existing local BTC workflow, update config/backtest.yaml, and execute the approved CLI pipeline (update-data, validate-data, run, export-sheets) without changing core Python code. Use when the user asks to run, repeat, or export a local crypto backtest with specific symbols, dates, strategies, capitals, or Google Sheets output.
---

# Run Local Crypto Backtest

## Boundary

- Keep work local, deterministic, and observation-only.
- Do not change strategy formulas, liquidation models, performance metrics, storage layout, or report semantics.
- Do not add live trading, auto-ordering, browser automation, or new dependencies.
- Treat Google Sheets as export-only; never write raw K lines.

## Workflow

1. Extract `symbol`, `interval`, `start/end`, strategy list, capital, leverage, grid limits/count, fees, funding assumptions, and optional `spreadsheet_id`.
2. Convert relative dates to explicit UTC timestamps and stop on ambiguous inputs.
3. Update only `config/backtest.yaml` unless the user explicitly requests a different config path.
4. Run in order:
   - `python -m pytest`
   - `python -m src.cli update-data ...`
   - `python -m src.cli validate-data ...`
   - `python -m src.cli run config/backtest.yaml`
   - `python -m src.cli export-sheets ...` only when Sheets export is requested and credentials are available
5. Stop at the first failure. Do not reuse an old `run_id` or an old report directory.
6. Report exact inputs, outputs, `run_id`, report path, and any model assumptions or limits.

## Local-Report Summary

When the request is local-report only, Google Sheets is not requested, and the backtest finishes successfully with at least one successful strategy, add a natural-language summary before the structured result.

Include:

- market and period: symbol, interval, start date, end date
- strategy names and initial capital
- for each strategy: final equity, net profit or loss, total return, max drawdown, liquidation state, trade count, and fees
- cross-strategy comparison: highest final equity, largest loss, highest drawdown, whether any strategy liquidated, and whether leverage amplified gain or loss
- a short plain-language conclusion that identifies the result as historical backtest output and avoids future-performance claims

Formatting rules:

- Show percentages as human-readable values, for example `-27.14%` and `35.69% max drawdown`
- Round `final_equity` and `net_profit` to 2 decimals
- Round fees to 2 to 4 decimals as needed
- Show dates as `YYYY-MM-DD`
- Do not dump raw floating-point fields when a readable summary is possible
- If a strategy failed, state the error clearly and do not replace missing values with zero
- If a strategy liquidated, state when it liquidated and that this is a strategy outcome, not a program error

Suggested output order:

1. Natural-language summary
2. Structured performance table
3. `run_id`
4. Local report directory
5. Model assumptions and limits
6. PASS or FAIL

## Ambiguity Rules

- If date ranges are relative, resolve them to exact UTC dates before editing YAML.
- If grid bounds are reversed, correct them only after confirming intent.
- If a leverage or funding assumption is missing and changes results materially, ask before guessing.
- If `GOOGLE_APPLICATION_CREDENTIALS` or spreadsheet ID is missing, stop before Sheets export.

## Self-Improvement Loop

- After each completed use, note any repeated friction, slow step, or failure source.
- Prefer tightening this skill or its reference notes before changing project code.
- If the same workflow issue repeats, update this skill with the smaller safer path.

## Workflow Optimization

- Prefer the shortest safe path that reaches the requested result.
- Reuse the existing local report, YAML, and CLI sequence instead of inventing a new route.
- If a step keeps failing or costing time, capture the cause and add a smaller rule to this skill before changing code.
- If a default assumption is used, write it down explicitly so later runs can reuse it or remove it.
- Keep future runs stable by narrowing ambiguity up front, not by adding more automation.
