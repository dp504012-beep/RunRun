from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from src.logging_config import configure_logging

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crypto-backtester")
    subparsers = parser.add_subparsers(dest="command")

    for command in ("update-data", "run", "export-sheets", "validate-data"):
        subparsers.add_parser(command, help="尚未實作")

    return parser


def _handle_command(command: str) -> int:
    logger.info("%s 尚未實作", command)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    return _handle_command(args.command)


if __name__ == "__main__":
    raise SystemExit(main())

