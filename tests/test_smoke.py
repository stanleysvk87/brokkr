"""Stage 0 smoke test -- just proves the package imports and the CLI's
typer app is wired up correctly. Real behavior tests start in Stage 1."""

from __future__ import annotations

from typer.testing import CliRunner

from brokkr import __version__
from brokkr.cli import app


def test_version_is_a_string():
    assert isinstance(__version__, str)
    assert __version__


def test_cli_version_command_runs():
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
