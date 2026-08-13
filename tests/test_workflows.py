from __future__ import annotations

import json
import sqlite3
from io import StringIO

import pytest
from rich.console import Console
from typer.testing import CliRunner

from brokkr import cli
from brokkr.approvals.store import (
    ApprovalStore,
    TemplateConstraint,
    WorkflowStep,
    WorkflowValidationError,
)
from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import SandboxExecutionResult


def _settings(tmp_path) -> Settings:
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


def _result(
    argv: list[str],
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    stdout: str = "",
) -> SandboxExecutionResult:
    return SandboxExecutionResult(
        command=argv,
        exit_code=exit_code,
        timed_out=timed_out,
        truncated=False,
        stdout=stdout,
        stderr="failure\n" if exit_code else "",
        duration_ms=1.0,
        container_id="container",
        image_id="image",
        network_enabled=False,
    )


def _install_workflow_cli(monkeypatch, settings, results):
    calls: list[list[str]] = []
    queued = iter(results)
    output = StringIO()

    class FakeSandbox:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def exec(self, argv, timeout=None, network=False):
            assert network is False
            calls.append(argv)
            planned = next(queued)
            return _result(argv, **planned)

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        cli, "console", Console(file=output, force_terminal=False, color_system=None)
    )
    return calls, output


def test_workflow_store_preserves_steps_order_and_usage(tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)
    steps = store.prepare_workflow_steps([["printf", "one"], ["printf", "two"]])

    created = store.create_workflow("daily", steps)

    assert [step.argv for step in created.steps] == [
        ["printf", "one"],
        ["printf", "two"],
    ]
    assert store.list_workflows()[0].use_count == 0
    store.mark_workflow_used("daily")
    assert store.get_workflow("daily").use_count == 1
    assert store.delete_workflow("daily") is True
    assert store.get_workflow("daily") is None


def test_workflow_store_rejects_invalid_name_and_unvalidated_template_step(tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)

    with pytest.raises(WorkflowValidationError, match="workflow name"):
        store.create_workflow("bad[name]", [WorkflowStep(1, ["true"])])
    with pytest.raises(WorkflowValidationError, match="no approval template"):
        store.create_workflow(
            "bad-template",
            [
                WorkflowStep(1, ["echo", "value"]),
                WorkflowStep(2, ["cat", "value"], "tpl_missing", True),
            ],
        )


def test_last_reviewed_commands_excludes_auto_approved_and_keeps_order(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditStore(settings)
    with sqlite3.connect(settings.audit_db_path) as conn:
        for index, decision in enumerate(["approved", "auto_approved", "edited"]):
            command_id = f"command-{index}"
            created = f"2026-08-13T10:00:0{index}+00:00"
            conn.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, 'model', 'reason', ?, 1, NULL)",
                (command_id, created, f"task {index}", json.dumps(["echo", str(index)])),
            )
            conn.execute(
                """
                INSERT INTO decisions (
                    command_id, created_at, decision, final_argv_json, reason
                ) VALUES (?, ?, ?, ?, NULL)
                """,
                (command_id, created, decision, json.dumps(["echo", str(index)])),
            )
            conn.execute(
                """
                INSERT INTO commands (
                    command_id, created_at, source, argv_json, exit_code,
                    timed_out, truncated, duration_ms, container_id, image_id
                ) VALUES (?, ?, 'llm', ?, 0, 0, 0, 1, 'c', 'i')
                """,
                (command_id, created, json.dumps(["echo", str(index)])),
            )

    reviewed = audit.last_reviewed_commands(2)

    assert [entry.argv for entry in reviewed] == [["echo", "0"], ["echo", "2"]]


def test_workflow_save_cli_captures_recent_reviewed_steps(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    AuditStore(settings)
    with sqlite3.connect(settings.audit_db_path) as conn:
        for index in range(2):
            command_id = f"save-{index}"
            created = f"2026-08-13T11:00:0{index}+00:00"
            argv = ["echo", str(index)]
            conn.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, 'model', 'reason', ?, 1, NULL)",
                (command_id, created, f"task {index}", json.dumps(argv)),
            )
            conn.execute(
                """
                INSERT INTO decisions (
                    command_id, created_at, decision, final_argv_json, reason
                ) VALUES (?, ?, 'approved', ?, NULL)
                """,
                (command_id, created, json.dumps(argv)),
            )
            conn.execute(
                """
                INSERT INTO commands (
                    command_id, created_at, source, argv_json, exit_code,
                    timed_out, truncated, duration_ms, container_id, image_id
                ) VALUES (?, ?, 'llm_approved', ?, 0, 0, 0, 1, 'c', 'i')
                """,
                (command_id, created, json.dumps(argv)),
            )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *args, **kwargs: True))

    result = CliRunner().invoke(
        cli.app, ["workflow", "save", "saved", "--steps", "2"]
    )

    assert result.exit_code == 0
    workflow = ApprovalStore(settings).get_workflow("saved")
    assert workflow is not None
    assert [step.argv for step in workflow.steps] == [["echo", "0"], ["echo", "1"]]


def test_workflow_run_executes_in_order_and_audits_shared_run_id(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)
    store.create_workflow(
        "ordered",
        [WorkflowStep(1, ["echo", "one"]), WorkflowStep(2, ["echo", "two"])],
    )
    calls, output = _install_workflow_cli(
        monkeypatch,
        settings,
        [{"stdout": "one\n"}, {"stdout": "two\n"}],
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "ordered"])

    assert result.exit_code == 0
    assert calls == [["echo", "one"], ["echo", "two"]]
    assert "Step 1/2" in output.getvalue()
    assert "workflow ordered completed" in output.getvalue()
    with sqlite3.connect(settings.audit_db_path) as conn:
        rows = conn.execute(
            """
            SELECT c.source, c.workflow_run_id, d.workflow_run_id,
                   c.workflow_name, c.workflow_step, d.decision
            FROM commands AS c JOIN decisions AS d USING(command_id)
            ORDER BY c.workflow_step
            """
        ).fetchall()
    assert [row[0] for row in rows] == ["workflow", "workflow"]
    assert len({row[1] for row in rows}) == 1
    assert all(row[1] == row[2] for row in rows)
    assert [(row[3], row[4], row[5]) for row in rows] == [
        ("ordered", 1, "workflow"),
        ("ordered", 2, "workflow"),
    ]
    history = AuditStore(settings).list_history(workflow="ordered")
    assert [(entry.workflow_step, entry.outcome) for entry in history] == [
        (2, "exit 0"),
        (1, "exit 0"),
    ]

    history_output = CliRunner().invoke(
        cli.app, ["history", "--workflow", "ordered"]
    )
    assert history_output.exit_code == 0
    assert rows[0][1][:8] in output.getvalue()


def test_workflow_stops_after_nonzero_exit(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    ApprovalStore(settings).create_workflow(
        "stop",
        [
            WorkflowStep(1, ["true"]),
            WorkflowStep(2, ["false"]),
            WorkflowStep(3, ["echo", "must-not-run"]),
        ],
    )
    calls, output = _install_workflow_cli(
        monkeypatch,
        settings,
        [{}, {"exit_code": 7}],
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "stop"])

    assert result.exit_code == 7
    assert calls == [["true"], ["false"]]
    assert "later steps did not run" in output.getvalue()


def test_workflow_stops_after_timeout(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    ApprovalStore(settings).create_workflow(
        "timeout",
        [WorkflowStep(1, ["sleep", "5"]), WorkflowStep(2, ["echo", "no"])],
    )
    calls, _output = _install_workflow_cli(
        monkeypatch,
        settings,
        [{"exit_code": 124, "timed_out": True}],
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "timeout"])

    assert result.exit_code == 124
    assert calls == [["sleep", "5"]]


def test_previous_stdout_fills_template_variable(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)
    template = store.create_template(
        ["cat", "/workspace/original.txt"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    steps = store.prepare_workflow_steps(
        [["printf", "/workspace/next.txt"], ["cat", "/workspace/original.txt"]],
        {2: template.id},
    )
    store.create_workflow("passing", steps)
    calls, _output = _install_workflow_cli(
        monkeypatch,
        settings,
        [{"stdout": "/workspace/next.txt\n"}, {"stdout": "contents\n"}],
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "passing"])

    assert result.exit_code == 0
    assert calls == [["printf", "/workspace/next.txt"], ["cat", "/workspace/next.txt"]]


def test_invalid_previous_stdout_stops_before_template_step(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)
    template = store.create_template(
        ["cat", "/workspace/original.txt"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    steps = store.prepare_workflow_steps(
        [["printf", "/etc/passwd"], ["cat", "/workspace/original.txt"]],
        {2: template.id},
    )
    store.create_workflow("invalid", steps)
    calls, output = _install_workflow_cli(
        monkeypatch, settings, [{"stdout": "/etc/passwd\n"}]
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "invalid"])

    assert result.exit_code == 1
    assert calls == [["printf", "/etc/passwd"]]
    assert "does not satisfy path_under_workdir" in output.getvalue()
    assert "later steps did not run" in output.getvalue()


def test_policy_checks_resolved_template_and_stops_before_execution(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    store = ApprovalStore(settings)
    template = store.create_template(
        ["rm", "-rf", "/workspace/safe"],
        {2: TemplateConstraint("regex", ".*")},
    )
    steps = store.prepare_workflow_steps(
        [
            ["printf", "/"],
            ["rm", "-rf", "/workspace/safe"],
            ["echo", "must-not-run"],
        ],
        {2: template.id},
    )
    store.create_workflow("policy", steps)
    calls, output = _install_workflow_cli(monkeypatch, settings, [{"stdout": "/\n"}])

    result = CliRunner().invoke(cli.app, ["workflow", "run", "policy"])

    assert result.exit_code == 1
    assert calls == [["printf", "/"]]
    assert "blocked by policy" in output.getvalue()
    with sqlite3.connect(settings.audit_db_path) as conn:
        blocked = conn.execute(
            "SELECT decision, final_argv_json FROM decisions WHERE workflow_step = 2"
        ).fetchone()
        commands = conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    assert blocked == ("blocked", '["rm", "-rf", "/"]')
    assert commands == 1


def test_missing_workflow_fails_clearly_without_sandbox(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    output = StringIO()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "DockerSandbox",
        lambda _: (_ for _ in ()).throw(AssertionError("must not construct sandbox")),
    )
    monkeypatch.setattr(
        cli, "console", Console(file=output, force_terminal=False, color_system=None)
    )

    result = CliRunner().invoke(cli.app, ["workflow", "run", "missing"])

    assert result.exit_code == 1
    assert "no workflow named missing" in output.getvalue()


def test_workflow_list_show_and_delete_cli(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    ApprovalStore(settings).create_workflow(
        "inspectable", [WorkflowStep(1, ["echo", "visible"])]
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    listed = CliRunner().invoke(cli.app, ["workflow", "list"])
    shown = CliRunner().invoke(cli.app, ["workflow", "show", "inspectable"])
    deleted = CliRunner().invoke(cli.app, ["workflow", "delete", "inspectable"])

    assert listed.exit_code == 0
    assert "inspectable" in listed.output
    assert shown.exit_code == 0
    assert "echo visible" in shown.output
    assert deleted.exit_code == 0
    assert ApprovalStore(settings).get_workflow("inspectable") is None


def test_existing_audit_tables_gain_workflow_columns(tmp_path):
    settings = _settings(tmp_path)
    settings.log_dir.mkdir(parents=True)
    with sqlite3.connect(settings.audit_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE decisions (
                command_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                decision TEXT NOT NULL, final_argv_json TEXT, reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE commands (
                command_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                source TEXT NOT NULL, argv_json TEXT NOT NULL,
                exit_code INTEGER NOT NULL, timed_out INTEGER NOT NULL,
                truncated INTEGER NOT NULL, duration_ms REAL NOT NULL,
                container_id TEXT NOT NULL, image_id TEXT NOT NULL,
                network_enabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )

    AuditStore(settings)

    with sqlite3.connect(settings.audit_db_path) as conn:
        command_columns = {row[1] for row in conn.execute("PRAGMA table_info(commands)")}
        decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    expected = {"workflow_run_id", "workflow_name", "workflow_step"}
    assert expected <= command_columns
    assert expected <= decision_columns
