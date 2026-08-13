from __future__ import annotations

import pytest
from typer.testing import CliRunner

from brokkr import cli


@pytest.mark.parametrize(
    ("name", "expected_message"),
    [
        ("BROKKR_SANDBOX_CPU_LIMIT", "must be a number"),
        ("BROKKR_SANDBOX_PIDS_LIMIT", "must be an integer"),
        ("BROKKR_SANDBOX_COMMAND_TIMEOUT_SECONDS", "must be a number"),
        ("BROKKR_SANDBOX_IDLE_RESET_MINUTES", "must be a number"),
        ("BROKKR_MEMORY_MAX_NOTES", "must be an integer"),
    ],
)
def test_invalid_numeric_setting_is_a_clean_cli_error(monkeypatch, name, expected_message):
    monkeypatch.setenv(name, "not-a-number")

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "configuration error" in result.output
    assert f"{name} {expected_message}" in result.output
    assert "'not-a-number'" in result.output
    assert "Traceback" not in result.output


def test_uncreatable_workspace_is_a_clean_cli_error(monkeypatch, tmp_path):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("occupied")
    workspace = blocking_file / "workspace"
    monkeypatch.setenv("BROKKR_SANDBOX_WORKDIR_HOST", str(workspace))

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "configuration error" in result.output
    assert "BROKKR_SANDBOX_WORKDIR_HOST" in result.output
    assert str(workspace) in "".join(result.output.split())
    assert "Traceback" not in result.output
