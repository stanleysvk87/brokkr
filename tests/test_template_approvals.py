from __future__ import annotations

import sqlite3
from io import StringIO

import pytest
import typer
from rich.console import Console

from brokkr import cli
from brokkr.approvals.store import ApprovalStore, TemplateConstraint
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
        approval_template_matching=True,
    )


def _with_template_matching(settings: Settings, enabled: bool) -> Settings:
    return settings.model_copy(update={"approval_template_matching": enabled})


def _install_propose_fakes(monkeypatch, settings, argv, prompt_answers=()):
    audit = AuditStore(settings)
    approvals = ApprovalStore(settings)
    responses = iter(prompt_answers)
    prompt_calls: list[str] = []
    output = StringIO()

    result = ProposalResult(
        task_description="template task",
        model="test-model",
        raw_content="raw",
        latency_ms=1.0,
        proposal=CommandProposal(reasoning="because", argv=argv),
    )

    class FakeClient:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def propose(self, task, model=None, notes=None):
            return result

    def prompt_ask(message, *args, **kwargs):
        prompt_calls.append(message)
        return next(responses)

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "AuditStore", lambda loaded_settings: audit)
    monkeypatch.setattr(cli, "ApprovalStore", lambda loaded_settings: approvals)
    monkeypatch.setattr(cli, "MemoryStore", lambda loaded_settings: type("M", (), {"recent": lambda self, limit: []})())
    monkeypatch.setattr(cli, "OllamaClient", FakeClient)
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(prompt_ask))
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, color_system=None))
    return audit, approvals, prompt_calls, output


def _decision_rows(settings):
    with sqlite3.connect(settings.audit_db_path) as conn:
        return conn.execute(
            "SELECT decision, final_argv_json, reason FROM decisions"
        ).fetchall()


def _command_rows(settings):
    with sqlite3.connect(settings.audit_db_path) as conn:
        return conn.execute("SELECT source, argv_json FROM commands").fetchall()


def test_matching_template_skips_prompt_and_has_distinct_audit_decision(monkeypatch, settings):
    argv = ["find", "/workspace/archive", "-name", "*.dat"]
    _audit, approvals, prompt_calls, output = _install_propose_fakes(
        monkeypatch, settings, argv
    )
    template = approvals.create_template(
        ["find", "/workspace/reports", "-name", "*.dat"],
        {1: TemplateConstraint("path_under_workdir")},
    )

    class FakeSandbox:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def exec(self, command, timeout=None):
            return SandboxExecutionResult(
                command=command,
                exit_code=0,
                timed_out=False,
                truncated=False,
                stdout="matched\n",
                stderr="",
                duration_ms=1.0,
                container_id="container",
                image_id="image",
            )

    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("find data")

    assert exc_info.value.exit_code == 0
    assert prompt_calls == []
    assert f"matched template {template.id}" in output.getvalue()
    assert _decision_rows(settings) == [("template_matched", '["find", "/workspace/archive", "-name", "*.dat"]', None)]
    assert _command_rows(settings) == [("llm_template_matched", '["find", "/workspace/archive", "-name", "*.dat"]')]
    assert approvals.list_templates()[0].use_count == 1


def test_exact_match_keeps_priority_and_auto_approved_audit_value(monkeypatch, settings):
    argv = ["find", "/workspace/reports", "-name", "*.dat"]
    _audit, approvals, prompt_calls, output = _install_propose_fakes(
        monkeypatch, settings, argv
    )
    exact = approvals.remember(argv)
    approvals.create_template(
        argv,
        {1: TemplateConstraint("path_under_workdir")},
    )

    class FakeSandbox:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def exec(self, command, timeout=None):
            return SandboxExecutionResult(
                command=command,
                exit_code=0,
                timed_out=False,
                truncated=False,
                stdout="exact\n",
                stderr="",
                duration_ms=1.0,
                container_id="container",
                image_id="image",
            )

    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)

    with pytest.raises(typer.Exit):
        cli.propose("find data")

    assert prompt_calls == []
    assert "remembered -- running without asking" in output.getvalue()
    assert "matched template" not in output.getvalue()
    assert _decision_rows(settings)[0][0] == "auto_approved"
    assert approvals.find(argv).id == exact.id
    assert approvals.find(argv).use_count == 1
    assert approvals.list_templates()[0].use_count == 0


def test_constraint_failure_falls_back_to_human_review(monkeypatch, settings):
    _audit, approvals, prompt_calls, _output = _install_propose_fakes(
        monkeypatch,
        settings,
        ["find", "/etc", "-name", "*.dat"],
        prompt_answers=["n"],
    )
    approvals.create_template(
        ["find", "/workspace/reports", "-name", "*.dat"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("find data")

    assert exc_info.value.exit_code == 0
    assert prompt_calls == [r"Run this? \[y]es / \[e]dit / \[n]o / \[m]anual"]
    assert _decision_rows(settings)[0][0] == "rejected"


def test_default_off_does_not_consult_existing_template(monkeypatch, settings):
    settings = _with_template_matching(settings, False)
    _audit, approvals, prompt_calls, _output = _install_propose_fakes(
        monkeypatch,
        settings,
        ["find", "/workspace/archive", "-name", "*.dat"],
        prompt_answers=["n"],
    )
    approvals.create_template(
        ["find", "/workspace/reports", "-name", "*.dat"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    monkeypatch.setattr(
        approvals,
        "find_template",
        lambda argv: pytest.fail("default-off flow must not consult templates"),
    )

    with pytest.raises(typer.Exit):
        cli.propose("find data")

    assert len(prompt_calls) == 1
    assert _decision_rows(settings)[0][0] == "rejected"


def test_policy_still_blocks_template_matched_command(monkeypatch, settings):
    _audit, approvals, prompt_calls, output = _install_propose_fakes(
        monkeypatch, settings, ["rm", "-rf", "/workspace"]
    )
    template = approvals.create_template(
        ["rm", "-rf", "/workspace"],
        {2: TemplateConstraint("regex", ".*")},
    )

    class ForbiddenSandbox:
        def __init__(self, loaded_settings):
            raise AssertionError("blocked template matches must not construct a sandbox")

    monkeypatch.setattr(cli, "DockerSandbox", ForbiddenSandbox)

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("remove workspace")

    assert exc_info.value.exit_code == 1
    assert prompt_calls == []
    assert "blocked by policy" in output.getvalue()
    assert _decision_rows(settings)[0][0] == "blocked"
    assert _command_rows(settings) == []
    assert approvals.list_templates()[0].id == template.id
    assert approvals.list_templates()[0].use_count == 0


def test_interactive_creation_uses_only_human_entered_constraints(monkeypatch, settings):
    approvals = ApprovalStore(settings)
    responses = iter(["1,2", "path_under_workdir", "enum", "json,text"])
    output = StringIO()
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: next(responses)))
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, color_system=None))

    cli._create_template_interactively(
        approvals,
        ["convert", "/workspace/input.png", "json"],
    )

    template = approvals.list_templates()[0]
    assert template.parts[1].variable == TemplateConstraint("path_under_workdir")
    assert template.parts[2].variable == TemplateConstraint("enum", ["json", "text"])
    assert f"template {template.id} saved" in output.getvalue()


def test_interactive_creation_rejects_origin_constraint_mismatch(monkeypatch, settings):
    approvals = ApprovalStore(settings)
    responses = iter(["1", "path_under_workdir"])
    output = StringIO()
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: next(responses)))
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, color_system=None))

    cli._create_template_interactively(approvals, ["cat", "/etc/passwd"])

    assert approvals.list_templates() == []
    assert "template not saved" in output.getvalue()
    assert "does not satisfy" in output.getvalue()


def test_approvals_list_labels_template_and_revoke_accepts_template_id(
    monkeypatch, settings
):
    approvals = ApprovalStore(settings)
    template = approvals.create_template(
        ["find", "/workspace/reports", "-name", "*.dat"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    output = StringIO()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=False, color_system=None, width=180))

    cli.approvals_list()

    rendered = output.getvalue()
    assert "template" in rendered
    assert template.id in rendered
    assert "path_under_workdir" in rendered

    cli.approvals_revoke(template.id)
    assert approvals.list_templates() == []
    assert f"revoked approval {template.id}" in output.getvalue()
