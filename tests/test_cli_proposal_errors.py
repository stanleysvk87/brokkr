from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from brokkr import cli
from brokkr.llm.client import ProposalResult


def test_propose_displays_short_user_error_and_keeps_technical_detail(monkeypatch):
    result = ProposalResult(
        task_description="fetch a URL",
        model="test-model",
        raw_content="raw model output",
        latency_ms=1.0,
        error="model returned an invalid proposal: long technical detail",
        user_error="The model proposed an invalid command. Try rephrasing the task.",
    )
    output = []

    class FakeAudit:
        def __init__(self, settings):
            pass

        def new_command_id(self):
            return "command-id"

        def record_proposal(self, command_id, proposal_result):
            assert proposal_result is result

    class FakeMemory:
        def __init__(self, settings):
            pass

        def recent(self, limit):
            return []

    class FakeClient:
        def __init__(self, settings):
            pass

        def propose(self, task, model=None, notes=None):
            return result

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(memory_max_notes=20))
    monkeypatch.setattr(cli, "AuditStore", FakeAudit)
    monkeypatch.setattr(
        cli,
        "ApprovalStore",
        lambda settings: SimpleNamespace(search_library=lambda task: []),
    )
    monkeypatch.setattr(cli, "MemoryStore", FakeMemory)
    monkeypatch.setattr(cli, "OllamaClient", FakeClient)
    monkeypatch.setattr(cli, "console", SimpleNamespace(print=output.append))

    with pytest.raises(typer.Exit) as exc_info:
        cli.propose("fetch a URL")

    assert exc_info.value.exit_code == 1
    assert output == [
        (
            "[red]proposal failed:[/red] "
            "The model proposed an invalid command. Try rephrasing the task."
        )
    ]
    assert "technical detail" not in output[0]
