from __future__ import annotations

import json
import sqlite3
from io import StringIO

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from brokkr import cli
from brokkr.approvals.store import (
    ApprovalStore,
    LibraryValidationError,
    TemplateConstraint,
)
from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings
from brokkr.llm.client import CommandProposal, ProposalResult
from brokkr.permissions.policy import check_prohibited
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


def _result(argv: list[str], stdout: str = "ok\n") -> SandboxExecutionResult:
    return SandboxExecutionResult(
        command=argv,
        exit_code=0,
        timed_out=False,
        truncated=False,
        stdout=stdout,
        stderr="",
        duration_ms=1.0,
        container_id="container",
        image_id="image",
    )


def _proposal(task: str, argv: list[str]) -> ProposalResult:
    return ProposalResult(
        task_description=task,
        model="test-model",
        raw_content="raw",
        latency_ms=1.0,
        proposal=CommandProposal(reasoning="test", argv=argv),
    )


def _record_reviewed_execution(settings: Settings, argv: list[str]) -> None:
    audit = AuditStore(settings)
    command_id = audit.new_command_id()
    audit.record_proposal(command_id, _proposal("reviewed task", argv))
    audit.record_decision(command_id, "approved", argv)
    audit.record_execution(command_id, _result(argv), source="llm_approved")


def test_fresh_store_seeds_ten_safe_entries_once(settings):
    store = ApprovalStore(settings)

    entries = store.list_library_entries()

    assert len(entries) == 10
    assert {entry.name for entry in entries} == {
        "archive-workspace",
        "count-todo-lines",
        "extract-pdf-text",
        "extract-tar-archive",
        "extract-zip-archive",
        "find-large-files",
        "find-recent-files",
        "git-worktree-status",
        "ocr-scanned-image",
        "workspace-disk-usage",
    }
    assert all(check_prohibited(entry.argv) is None for entry in entries)
    assert store.delete_library_entry("archive-workspace") is True
    assert ApprovalStore(settings).get_library_entry("archive-workspace") is None


def test_library_store_supports_single_variable_existing_template(settings):
    store = ApprovalStore(settings)
    template = store.create_template(
        ["cat", "/workspace/example.txt"],
        {1: TemplateConstraint("path_under_workdir")},
    )
    entry = store.create_library_entry(
        "show-file",
        "Show the contents of a selected workspace file",
        ["cat", "/workspace/example.txt"],
        template.id,
    )

    assert store.resolve_library_entry(entry, "/workspace/other.txt") == [
        "cat",
        "/workspace/other.txt",
    ]
    assert "<path under /workspace>" in store.format_library_command(entry)
    with pytest.raises(LibraryValidationError, match="does not satisfy"):
        store.resolve_library_entry(entry, "/etc/passwd")


def test_keyword_search_is_simple_ranked_overlap(settings):
    store = ApprovalStore(settings)
    store.create_library_entry(
        "slovak-lines",
        "Spočítaj riadky obsahujúce chybu v súbore",
        ["grep", "-c", "chyba", "/workspace/input.txt"],
    )

    matches = store.search_library("please extract readable text from this PDF document")

    assert matches[0].entry.name == "extract-pdf-text"
    assert matches[0].score >= 0.5
    assert store.search_library("spočítaj riadky s chybou")[0].entry.name == "slovak-lines"
    assert store.search_library("calculate a checksum for one file") == []


def test_save_direct_command_and_cli_inspection(settings, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    runner = CliRunner()

    saved = runner.invoke(
        cli.app,
        [
            "library",
            "save",
            "hello",
            "--description",
            "Print a friendly greeting",
            "--command",
            "printf 'hello world\\n'",
        ],
    )
    shown = runner.invoke(cli.app, ["library", "show", "hello"])
    listed = runner.invoke(cli.app, ["library", "list"])

    assert saved.exit_code == 0
    assert ApprovalStore(settings).get_library_entry("hello").argv == [
        "printf",
        "hello world\\n",
    ]
    assert shown.exit_code == 0
    assert "Print a friendly greeting" in shown.output
    assert "printf" in shown.output
    assert listed.exit_code == 0
    assert "hello" in listed.output

    deleted = runner.invoke(cli.app, ["library", "delete", "hello"])
    assert deleted.exit_code == 0
    assert ApprovalStore(settings).get_library_entry("hello") is None


def test_save_from_last_approved_captures_exact_argv(settings, monkeypatch):
    argv = ["find", "/workspace", "-name", "*.txt", "-print"]
    _record_reviewed_execution(settings, argv)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    result = CliRunner().invoke(
        cli.app,
        [
            "library",
            "save",
            "reviewed-find",
            "--description",
            "Find text files in the workspace",
            "--from-last-approved",
        ],
    )

    assert result.exit_code == 0
    assert ApprovalStore(settings).get_library_entry("reviewed-find").argv == argv


@pytest.mark.parametrize("source", ["direct", "audit"])
def test_library_save_rejects_policy_blocked_command(settings, monkeypatch, source):
    argv = ["rm", "-rf", "/workspace"]
    if source == "audit":
        _record_reviewed_execution(settings, argv)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    args = [
        "library",
        "save",
        f"blocked-{source}",
        "--description",
        "Unsafe command",
    ]
    args += ["--command", "rm -rf /workspace"] if source == "direct" else ["--from-last-approved"]

    result = CliRunner().invoke(cli.app, args)

    assert result.exit_code == 1
    assert "blocked by policy" in result.output
    assert ApprovalStore(settings).get_library_entry(f"blocked-{source}") is None


def test_malformed_direct_command_fails_without_traceback(settings, monkeypatch):
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    result = CliRunner().invoke(
        cli.app,
        [
            "library",
            "save",
            "broken",
            "--description",
            "Broken quoting",
            "--command",
            "printf 'unterminated",
        ],
    )

    assert result.exit_code == 1
    assert "command could not be parsed" in result.output
    assert "Traceback" not in result.output


def _proposal_services(settings, client):
    return cli.ProposalServices(
        settings=settings,
        audit=AuditStore(settings),
        approvals=ApprovalStore(settings),
        memory=type("Memory", (), {"recent": lambda self, limit: []})(),
        client=client,
    )


def test_accepting_library_match_runs_before_model_and_has_distinct_audit(settings, monkeypatch):
    class ForbiddenClient:
        def propose(self, *args, **kwargs):
            raise AssertionError("accepted library match must not call the model")

    calls = []

    class FakeSandbox:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def exec(self, argv, timeout=None, network=False):
            calls.append(argv)
            return _result(argv, "extracted\n")

    output = StringIO()
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: "use"))
    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        cli, "console", Console(file=output, force_terminal=False, color_system=None)
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli._run_proposal(
            "extract readable text from the PDF document",
            _proposal_services(settings, ForbiddenClient()),
        )

    assert exc_info.value.exit_code == 0
    assert calls == [["pdftotext", "/workspace/document.pdf", "/workspace/document.txt"]]
    with sqlite3.connect(settings.audit_db_path) as conn:
        decision = conn.execute("SELECT decision, library_name FROM decisions").fetchone()
        execution = conn.execute("SELECT source, library_name FROM commands").fetchone()
    assert decision == ("library", "extract-pdf-text")
    assert execution == ("library", "extract-pdf-text")
    assert ApprovalStore(settings).get_library_entry("extract-pdf-text").use_count == 1
    history = AuditStore(settings).list_history(decision="library")
    assert len(history) == 1
    assert history[0].task_description == "library extract-pdf-text"
    assert "Library match found before asking the model" in output.getvalue()


@pytest.mark.parametrize(
    ("task", "answers"),
    [
        ("calculate a checksum for one file", ["n"]),
        ("extract readable text from the PDF document", ["model", "n"]),
    ],
)
def test_no_match_or_declined_match_falls_through_to_model_unchanged(
    settings, monkeypatch, task, answers
):
    calls = []

    class FakeClient:
        def propose(self, received, model=None, notes=None):
            calls.append(received)
            return _proposal(received, ["sha256sum", "/workspace/input.txt"])

    responses = iter(answers)
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: next(responses)))

    with pytest.raises(typer.Exit) as exc_info:
        cli._run_proposal(task, _proposal_services(settings, FakeClient()))

    assert exc_info.value.exit_code == 0
    assert calls == [task]


def test_library_run_rechecks_policy_after_database_tampering(settings, monkeypatch):
    ApprovalStore(settings)
    with sqlite3.connect(settings.approvals_db_path) as conn:
        conn.execute(
            "UPDATE library_entries SET argv_json = ? WHERE name = ?",
            (json.dumps(["rm", "-rf", "/workspace"]), "workspace-disk-usage"),
        )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "DockerSandbox",
        lambda loaded: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    result = CliRunner().invoke(cli.app, ["library", "run", "workspace-disk-usage"])

    assert result.exit_code == 1
    assert "blocked by policy" in result.output
    with sqlite3.connect(settings.audit_db_path) as conn:
        assert conn.execute("SELECT decision, library_name FROM decisions").fetchone() == (
            "blocked",
            "workspace-disk-usage",
        )
        assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0


def test_explicit_library_run_uses_named_entry_without_model(settings, monkeypatch):
    calls = []

    class FakeSandbox:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def exec(self, argv, timeout=None, network=False):
            calls.append(argv)
            return _result(argv, "12K /workspace\n")

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        cli,
        "OllamaClient",
        lambda loaded: (_ for _ in ()).throw(AssertionError("must not construct model client")),
    )

    result = CliRunner().invoke(cli.app, ["library", "run", "workspace-disk-usage"])

    assert result.exit_code == 0
    assert calls == [["du", "-sh", "/workspace"]]
    assert "12K /workspace" in result.output


def test_existing_audit_tables_gain_library_columns(settings):
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
                container_id TEXT NOT NULL, image_id TEXT NOT NULL
            )
            """
        )

    AuditStore(settings)

    with sqlite3.connect(settings.audit_db_path) as conn:
        decision_columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
        command_columns = {row[1] for row in conn.execute("PRAGMA table_info(commands)")}
    assert "library_name" in decision_columns
    assert "library_name" in command_columns
