from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
import typer

from brokkr import cli
from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings
from brokkr.llm.client import CommandProposal, ProposalResult
from brokkr.memory.store import MemoryStore


class CapturingConsole:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, *objects, **kwargs) -> None:
        self.messages.append(" ".join(str(item) for item in objects))


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


def _install_propose_fakes(monkeypatch, settings, argv, answers):
    audit = AuditStore(settings)
    result = ProposalResult(
        task_description="manual task",
        model="test-model",
        raw_content="raw",
        latency_ms=1.0,
        proposal=CommandProposal(reasoning="because", argv=argv),
    )
    output = CapturingConsole()
    responses = iter(answers)

    class FakeClient:
        def __init__(self, loaded_settings):
            assert loaded_settings is settings

        def propose(self, task, model=None, notes=None):
            return result

    class ForbiddenSandbox:
        def __init__(self, loaded_settings):
            raise AssertionError("manual mode must not construct a sandbox")

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "AuditStore", lambda loaded_settings: audit)
    monkeypatch.setattr(
        cli,
        "ApprovalStore",
        lambda loaded_settings: SimpleNamespace(find=lambda proposed: None),
    )
    monkeypatch.setattr(
        cli,
        "MemoryStore",
        lambda loaded_settings: SimpleNamespace(recent=lambda limit: []),
    )
    monkeypatch.setattr(cli, "OllamaClient", FakeClient)
    monkeypatch.setattr(cli, "DockerSandbox", ForbiddenSandbox)
    monkeypatch.setattr(cli.Prompt, "ask", staticmethod(lambda *args, **kwargs: next(responses)))
    monkeypatch.setattr(cli, "console", output)
    return audit, output


def _decision_rows(settings):
    with sqlite3.connect(settings.audit_db_path) as conn:
        return conn.execute(
            "SELECT command_id, decision, final_argv_json FROM decisions"
        ).fetchall()


def _command_count(settings):
    with sqlite3.connect(settings.audit_db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]


def test_manual_choice_records_decision_without_sandbox_execution(monkeypatch, settings):
    audit, output = _install_propose_fakes(
        monkeypatch, settings, ["du", "-sh", "/var/log"], ["m"]
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("inspect host logs")

    assert exc_info.value.exit_code == 0
    command_id, decision, final_argv_json = _decision_rows(settings)[0]
    assert decision == "manual"
    assert final_argv_json == '["du", "-sh", "/var/log"]'
    assert _command_count(settings) == 0
    assert audit.find_manual_decisions(command_id[:6])[0].command_id == command_id
    rendered = "\n".join(output.messages)
    assert "To run this yourself:" in rendered
    assert f"manual-{command_id[:8]}.txt" in rendered
    assert f"brokkr manual show {command_id[:8]}" in rendered


def test_edited_command_can_be_chosen_for_manual_mode(monkeypatch, settings):
    _audit, _output = _install_propose_fakes(
        monkeypatch,
        settings,
        ["du", "-sh", "/tmp"],
        ["e", "du -sh /var/log", "m"],
    )

    with pytest.raises(typer.Exit):
        cli.propose("inspect host logs")

    _command_id, decision, final_argv_json = _decision_rows(settings)[0]
    assert decision == "manual"
    assert final_argv_json == '["du", "-sh", "/var/log"]'
    assert _command_count(settings) == 0


def test_malformed_edited_command_is_cleanly_rejected(monkeypatch, settings):
    _audit, output = _install_propose_fakes(
        monkeypatch,
        settings,
        ["printf", "two lines"],
        ["e", "printf 'ABC"],
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("print two lines")

    assert exc_info.value.exit_code == 0
    with sqlite3.connect(settings.audit_db_path) as conn:
        decision = conn.execute(
            "SELECT decision, final_argv_json, reason FROM decisions"
        ).fetchone()
        proposals_without_decisions = conn.execute(
            """
            SELECT COUNT(*)
            FROM proposals AS p
            LEFT JOIN decisions AS d USING(command_id)
            WHERE d.command_id IS NULL
            """
        ).fetchone()[0]
    assert decision == (
        "rejected",
        None,
        "edited command could not be parsed: No closing quotation",
    )
    assert proposals_without_decisions == 0
    assert _command_count(settings) == 0
    rendered = "\n".join(output.messages)
    assert "could not parse that as a command -- check your quoting" in rendered
    assert "rejected, nothing ran" in rendered


def test_blocklist_applies_before_manual_instructions(monkeypatch, settings):
    _audit, output = _install_propose_fakes(
        monkeypatch, settings, ["rm", "-rf", "/workspace"], ["m"]
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("remove everything")

    assert exc_info.value.exit_code == 1
    _command_id, decision, _final_argv_json = _decision_rows(settings)[0]
    assert decision == "blocked"
    assert _command_count(settings) == 0
    rendered = "\n".join(output.messages)
    assert "blocked by policy" in rendered
    assert "To run this yourself" not in rendered


def test_manual_show_resolves_short_prefix_and_can_save_memory(monkeypatch, settings):
    command_id = "a1b2c3d4e5f678901234567890abcdef"
    AuditStore(settings).record_decision(command_id, "manual", ["du", "-sh", "/var/log"])
    result_path = settings.sandbox.workdir_host / "manual-a1b2c3d4.txt"
    result_path.write_text("12M /var/log\n", encoding="utf-8")
    output = CapturingConsole()

    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", output)
    monkeypatch.setattr(cli.Confirm, "ask", staticmethod(lambda *args, **kwargs: True))

    cli.manual_show("a1b2c3")

    rendered = "\n".join(output.messages)
    assert "Manual result for a1b2c3d4" in rendered
    assert "12M /var/log" in rendered
    note = MemoryStore(settings).list_all()[0]
    assert note.note == "Manual result for du -sh /var/log:\n12M /var/log\n"


def test_manual_show_reports_missing_result_file(monkeypatch, settings):
    command_id = "b1b2c3d4e5f678901234567890abcdef"
    AuditStore(settings).record_decision(command_id, "manual", ["uname", "-a"])
    output = CapturingConsole()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", output)

    with pytest.raises(typer.Exit) as exc_info:
        cli.manual_show(command_id)

    assert exc_info.value.exit_code == 1
    assert "no result found at" in output.messages[0]
    assert "manual-b1b2c3d4.txt yet" in output.messages[0]


def test_manual_show_rejects_ambiguous_prefix(monkeypatch, settings):
    audit = AuditStore(settings)
    audit.record_decision("d1b2c3d4e5f678901234567890abcdef", "manual", ["uname"])
    audit.record_decision("d1ffeeddccbbaa009988776655443322", "manual", ["id"])
    output = CapturingConsole()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", output)

    with pytest.raises(typer.Exit):
        cli.manual_show("d1")

    assert "ambiguous" in output.messages[0]


def test_manual_show_rejects_symlink_result(monkeypatch, settings, tmp_path):
    command_id = "c1b2c3d4e5f678901234567890abcdef"
    AuditStore(settings).record_decision(command_id, "manual", ["cat", "/etc/os-release"])
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (settings.sandbox.workdir_host / "manual-c1b2c3d4.txt").symlink_to(outside)
    output = CapturingConsole()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(cli, "console", output)

    with pytest.raises(typer.Exit):
        cli.manual_show("c1b2c3d4")

    assert "refusing result path" in output.messages[0]
    assert "outside" not in "\n".join(output.messages)
