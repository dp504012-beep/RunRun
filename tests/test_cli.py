from __future__ import annotations

import subprocess
import sys


def test_cli_help_executes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "update-data" in result.stdout


def test_cli_command_reports_not_implemented() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "run"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "尚未實作" in result.stderr

