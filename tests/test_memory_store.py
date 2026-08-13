"""Locks in explicit note storage and bounded chronological retrieval."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from brokkr import cli
from brokkr.config import SandboxConfig, Settings
from brokkr.memory.store import MemoryStore, MemoryValidationError


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )


def test_empty_store(settings):
    store = MemoryStore(settings)

    assert store.list_all() == []
    assert store.recent(20) == []
    assert store.forget(1) is False


def test_add_and_list_most_recent_first(settings):
    store = MemoryStore(settings)
    first = store.add("first note")
    second = store.add("second note")

    assert first.note == "first note"
    assert [entry.id for entry in store.list_all()] == [second.id, first.id]


@pytest.mark.parametrize("note", ["", " ", "\t\n"])
def test_empty_or_whitespace_note_is_rejected_without_storage(settings, note):
    store = MemoryStore(settings)

    with pytest.raises(MemoryValidationError, match="memory note must not be empty"):
        store.add(note)

    assert store.list_all() == []


def test_empty_note_cli_error_is_clear_and_stores_nothing(settings, monkeypatch):
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)

    result = CliRunner().invoke(cli.app, ["memory", "add", "   "])

    assert result.exit_code == 1
    assert "memory not added: memory note must not be empty" in result.output
    assert "Traceback" not in result.output
    assert MemoryStore(settings).list_all() == []


def test_recent_is_bounded_and_chronological(settings):
    store = MemoryStore(settings)
    store.add("first note")
    store.add("second note")
    store.add("third note")

    assert [entry.note for entry in store.recent(2)] == ["second note", "third note"]
    assert store.recent(0) == []
    assert store.recent(-1) == []


def test_forget_removes_note(settings):
    store = MemoryStore(settings)
    entry = store.add("temporary note")

    assert store.forget(entry.id) is True
    assert store.list_all() == []
    assert store.forget(entry.id) is False
