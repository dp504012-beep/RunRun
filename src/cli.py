from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from src.backtest_runner import execute_backtest_run
from src.exceptions import BacktesterError
from src.logging_config import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crypto-backtester")
    subparsers = parser.add_subparsers(dest="command")

    for command in ("update-data", "export-sheets", "validate-data"):
        subparsers.add_parser(command, help="not implemented")

    run_parser = subparsers.add_parser("run", help="run a backtest config")
    run_parser.add_argument("config_path", nargs="?", default="config/backtest.yaml")

    return parser


def _handle_command(command: str) -> int:
    logger.info("%s not implemented", command)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        try:
            execution = execute_backtest_run(args.config_path)
        except BacktesterError as exc:
            logger.error("run failed: %s", exc)
            return 1

        if execution.report_paths is not None:
            logger.info("report_dir=%s", execution.report_paths.report_dir)
        if execution.failed_strategy_count:
            logger.error("failed_strategies=%s", execution.failed_strategy_count)
            return 1
        return 0

    return _handle_command(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
