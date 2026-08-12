"""Locks in ApprovalStore's exact-match behavior -- the one guarantee
that matters here is that lookup never matches anything but a
byte-for-byte identical argv."""

from __future__ import annotations

import pytest

from brokkr.approvals.store import ApprovalStore
from brokkr.config import SandboxConfig, Settings


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


def test_find_returns_none_when_never_remembered(settings):
    store = ApprovalStore(settings)
    assert store.find(["ls", "-la"]) is None


def test_remember_then_find_matches_exactly(settings):
    store = ApprovalStore(settings)
    store.remember(["ls", "-la", "/workspace"], task_description="list files")

    assert store.find(["ls", "-la", "/workspace"]) is not None
    assert store.find(["ls", "-la"]) is None
    assert store.find(["ls", "-la", "/workspace", "extra"]) is None


def test_remember_is_idempotent(settings):
    store = ApprovalStore(settings)
    first = store.remember(["git", "status"])
    second = store.remember(["git", "status"])
    assert first.id == second.id
    assert len(store.list_all()) == 1


def test_mark_used_increments_use_count(settings):
    store = ApprovalStore(settings)
    entry = store.remember(["git", "status"])
    assert entry.use_count == 0

    store.mark_used(entry.command_hash)
    store.mark_used(entry.command_hash)

    updated = store.find(["git", "status"])
    assert updated.use_count == 2
    assert updated.last_used_at is not None


def test_revoke_removes_entry(settings):
    store = ApprovalStore(settings)
    entry = store.remember(["git", "status"])

    assert store.revoke(entry.id) is True
    assert store.find(["git", "status"]) is None
    assert store.revoke(entry.id) is False
