from __future__ import annotations

import sqlite3

from typer.testing import CliRunner

from brokkr import cli
from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings


def _settings(tmp_path) -> Settings:
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(workdir_host=tmp_path / "workspace"),
        approval_template_matching=False,
    )


def _proposal(conn, command_id: str, created_at: str, task: str, error=None):
    conn.execute(
        """
        INSERT INTO proposals (
            command_id, created_at, task_description, model,
            reasoning, proposed_argv_json, latency_ms, error
        ) VALUES (?, ?, ?, 'test-model', 'reason', '["true"]', 1.0, ?)
        """,
        (command_id, created_at, task, error),
    )


def _decision(conn, command_id: str, created_at: str, decision: str, reason=None):
    conn.execute(
        """
        INSERT INTO decisions (
            command_id, created_at, decision, final_argv_json, reason
        ) VALUES (?, ?, ?, '["true"]', ?)
        """,
        (command_id, created_at, decision, reason),
    )


def _command(conn, command_id: str, created_at: str, exit_code: int, timed_out=0):
    conn.execute(
        """
        INSERT INTO commands (
            command_id, created_at, source, argv_json, exit_code,
            timed_out, truncated, duration_ms, container_id, image_id,
            network_enabled
        ) VALUES (?, ?, 'llm_approved', '["true"]', ?, ?, 0, 1.0,
                  'container', 'image', 0)
        """,
        (command_id, created_at, exit_code, timed_out),
    )


def _seed_history(settings: Settings) -> AuditStore:
    store = AuditStore(settings)
    with sqlite3.connect(settings.audit_db_path) as conn:
        _proposal(conn, "approved-id", "2026-08-13T10:00:00+00:00", "approved task")
        _decision(conn, "approved-id", "2026-08-13T10:00:01+00:00", "approved")
        _command(conn, "approved-id", "2026-08-13T10:00:02+00:00", 0)

        _proposal(conn, "blocked-id", "2026-08-13T11:00:00+00:00", "blocked task")
        _decision(
            conn,
            "blocked-id",
            "2026-08-13T11:00:01+00:00",
            "blocked",
            "prohibited command",
        )

        _proposal(conn, "manual-id", "2026-08-13T12:00:00+00:00", "manual task")
        _decision(conn, "manual-id", "2026-08-13T12:00:01+00:00", "manual")

        _proposal(conn, "rejected-id", "2026-08-13T13:00:00+00:00", "rejected task")
        _decision(conn, "rejected-id", "2026-08-13T13:00:01+00:00", "rejected")
    return store


def test_history_lists_newest_first_with_non_execution_outcomes(tmp_path):
    settings = _settings(tmp_path)
    store = _seed_history(settings)

    entries = store.list_history()

    assert [entry.command_id for entry in entries] == [
        "rejected-id",
        "manual-id",
        "blocked-id",
        "approved-id",
    ]
    assert [entry.outcome for entry in entries] == [
        "rejected",
        "manual",
        "prohibited command",
        "exit 0",
    ]


def test_history_limit_actually_limits_newest_rows(tmp_path):
    store = _seed_history(_settings(tmp_path))

    assert [entry.command_id for entry in store.list_history(limit=2)] == [
        "rejected-id",
        "manual-id",
    ]


def test_history_decision_filter_keeps_rows_without_commands(tmp_path):
    store = _seed_history(_settings(tmp_path))

    blocked = store.list_history(decision="blocked")
    rejected = store.list_history(decision="rejected")
    manual = store.list_history(decision="manual")

    assert [(entry.command_id, entry.outcome) for entry in blocked] == [
        ("blocked-id", "prohibited command")
    ]
    assert [(entry.command_id, entry.outcome) for entry in rejected] == [
        ("rejected-id", "rejected")
    ]
    assert [(entry.command_id, entry.outcome) for entry in manual] == [
        ("manual-id", "manual")
    ]


def test_history_cli_applies_limit_and_decision_filter(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _seed_history(settings)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    result = CliRunner().invoke(
        cli.app,
        ["history", "--limit", "1", "--decision", "blocked"],
    )

    assert result.exit_code == 0
    assert "blocked task" in result.output
    assert "prohibited command" in result.output
    assert "approved task" not in result.output
    assert "rejected task" not in result.output


def test_history_cli_reports_empty_audit_trail(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["history"])

    assert result.exit_code == 0
    assert "no history yet" in result.output


def test_history_cli_reports_empty_filter_result(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    _seed_history(settings)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["history", "--decision", "edited"])

    assert result.exit_code == 0
    assert "no matching history" in result.output
