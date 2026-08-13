from __future__ import annotations

import sqlite3
from io import StringIO
from types import SimpleNamespace

import pytest
import typer
from rich.console import Console

from brokkr import cli
from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings
from brokkr.llm.client import CommandProposal, ProposalResult
from brokkr.sandbox.docker_sandbox import SandboxExecutionResult


@pytest.fixture
def settings(tmp_path) -> Settings:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(workdir_host=workspace),
        approval_template_matching=False,
    )


def _install_propose(monkeypatch, settings, *, allow_network, needs_network):
    calls: list[bool] = []
    output = StringIO()
    result = ProposalResult(
        task_description="network task",
        model="test-model",
        raw_content="raw",
        latency_ms=1.0,
        proposal=CommandProposal(
            reasoning="fetch",
            argv=["curl", "https://example.com"],
            needs_network=needs_network,
        ),
    )

    class FakeClient:
        def __init__(self, loaded_settings):
            pass

        def propose(self, task, model=None, notes=None):
            return result

    class FakeSandbox:
        def __init__(self, loaded_settings):
            pass

        def exec(self, argv, timeout=None, network=False):
            calls.append(network)
            return SandboxExecutionResult(
                command=argv,
                exit_code=0,
                timed_out=False,
                truncated=False,
                stdout="ok\n",
                stderr="",
                duration_ms=1.0,
                container_id="container",
                image_id="image",
                network_enabled=network,
            )

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "AuditStore", lambda loaded: AuditStore(settings))
    monkeypatch.setattr(
        cli,
        "ApprovalStore",
        lambda loaded: SimpleNamespace(find=lambda argv: None),
    )
    monkeypatch.setattr(
        cli,
        "MemoryStore",
        lambda loaded: SimpleNamespace(recent=lambda limit: []),
    )
    monkeypatch.setattr(cli, "OllamaClient", FakeClient)
    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: "y"))
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *args, **kwargs: False))
    monkeypatch.setattr(
        cli, "console", Console(file=output, force_terminal=False, color_system=None)
    )

    with pytest.raises(typer.Exit):
        cli.propose("check a URL", allow_network=allow_network)
    return calls, output.getvalue()


def test_model_network_flag_is_display_only(monkeypatch, settings):
    calls, output = _install_propose(
        monkeypatch,
        settings,
        allow_network=False,
        needs_network=True,
    )

    normalized = " ".join(output.split())
    assert "model reports this command may need network access" in normalized
    assert "rerun with --allow-network" in normalized
    assert calls == [False]


def test_explicit_cli_flag_enables_network_and_is_displayed(monkeypatch, settings):
    calls, output = _install_propose(
        monkeypatch,
        settings,
        allow_network=True,
        needs_network=False,
    )

    assert "network access enabled for this execution" in output
    assert calls == [True]


def test_sandbox_exec_threads_explicit_network_flag_and_audits_it(monkeypatch, settings):
    calls: list[bool] = []
    output = StringIO()

    class FakeSandbox:
        def __init__(self, loaded_settings):
            pass

        def exec(self, argv, timeout=None, network=False):
            calls.append(network)
            return SandboxExecutionResult(
                command=argv,
                exit_code=0,
                timed_out=False,
                truncated=False,
                stdout="ok\n",
                stderr="",
                duration_ms=1.0,
                container_id="container",
                image_id="image",
                network_enabled=network,
            )

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False))

    with pytest.raises(typer.Exit) as exc_info:
        cli.sandbox_exec(["curl", "https://example.com"], allow_network=True)

    assert exc_info.value.exit_code == 0
    assert calls == [True]
    assert "network access enabled for this execution" in output.getvalue()
    with sqlite3.connect(settings.audit_db_path) as conn:
        assert conn.execute("SELECT network_enabled FROM commands").fetchone() == (1,)


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected_color"),
    [(0, False, "\x1b[33m"), (1, False, "\x1b[31m"), (124, True, "\x1b[31m")],
)
def test_execution_stderr_style_reflects_outcome(
    monkeypatch, exit_code, timed_out, expected_color
):
    output = StringIO()
    monkeypatch.setattr(
        cli,
        "console",
        Console(file=output, force_terminal=True, color_system="standard"),
    )
    result = SandboxExecutionResult(
        command=["test-command"],
        exit_code=exit_code,
        timed_out=timed_out,
        truncated=False,
        stdout="",
        stderr="informational or error output\n",
        duration_ms=1.0,
        container_id="container",
        image_id="image",
        network_enabled=False,
    )

    cli._print_execution_output(result)

    rendered = output.getvalue()
    assert expected_color in rendered
    if exit_code == 0 and not timed_out:
        assert "\x1b[31m" not in rendered
