"""Locks in exact and explicitly constrained template approval matching."""

from __future__ import annotations

import pytest

from brokkr.approvals.store import (
    ApprovalStore,
    TemplateConstraint,
    TemplateValidationError,
    command_hash,
    constraint_matches,
    format_template,
)
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


def test_revoke_accepts_exact_command_hash(settings):
    store = ApprovalStore(settings)
    store.remember(["git", "status"])

    assert store.revoke(command_hash(["git", "status"])) is True
    assert store.find(["git", "status"]) is None


@pytest.mark.parametrize(
    "value",
    [
        "/workspace",
        "/workspace/reports/result.txt",
        "reports/result.txt",
        "./reports/result.txt",
    ],
)
def test_path_under_workdir_accepts_paths_that_stay_in_workspace(value):
    constraint = TemplateConstraint("path_under_workdir")
    assert constraint_matches(constraint, value) is True


@pytest.mark.parametrize(
    "value",
    [
        "../etc/passwd",
        "reports/../../etc/passwd",
        "/etc/passwd",
        "/workspace/../etc/passwd",
        "/workspace-other/file.txt",
        "",
    ],
)
def test_path_under_workdir_rejects_escape_or_outside_paths(value):
    constraint = TemplateConstraint("path_under_workdir")
    assert constraint_matches(constraint, value) is False


def test_enum_constraint_requires_exact_list_membership():
    constraint = TemplateConstraint("enum", ["json", "text"])

    assert constraint_matches(constraint, "json") is True
    assert constraint_matches(constraint, "JSON") is False
    assert constraint_matches(constraint, "csv") is False


def test_regex_constraint_uses_fullmatch_not_search():
    constraint = TemplateConstraint("regex", "foo")

    assert constraint_matches(constraint, "foo") is True
    assert constraint_matches(constraint, "foobar") is False


def test_create_template_rejects_origin_that_fails_constraint(settings):
    store = ApprovalStore(settings)

    with pytest.raises(TemplateValidationError, match="does not satisfy"):
        store.create_template(
            ["cat", "/etc/passwd"],
            {1: TemplateConstraint("path_under_workdir")},
        )

    assert store.list_templates() == []


@pytest.mark.parametrize(
    "constraint",
    [
        TemplateConstraint("enum", ["json", "text"]),
        TemplateConstraint("regex", r"report-\d+"),
    ],
)
def test_create_template_rejects_enum_or_regex_origin_mismatch(settings, constraint):
    store = ApprovalStore(settings)

    with pytest.raises(TemplateValidationError, match="does not satisfy"):
        store.create_template(["printf", "csv"], {1: constraint})

    assert store.list_templates() == []


def test_create_template_rejects_invalid_regex(settings):
    store = ApprovalStore(settings)

    with pytest.raises(TemplateValidationError, match="invalid regex"):
        store.create_template(
            ["printf", "foo"],
            {1: TemplateConstraint("regex", "[")},
        )

    assert store.list_templates() == []


def test_find_template_matches_literals_length_and_constraints(settings):
    store = ApprovalStore(settings)
    template = store.create_template(
        ["find", "/workspace/reports", "-name", "*.dat"],
        {1: TemplateConstraint("path_under_workdir")},
    )

    assert store.find_template(["find", "/workspace/archive", "-name", "*.dat"]).id == template.id
    assert store.find_template(["find", "../archive", "-name", "*.dat"]) is None
    assert store.find_template(["find", "/workspace/archive", "-type", "*.dat"]) is None
    assert store.find_template(["find", "/workspace/archive", "-name"]) is None
    assert store.find_template(
        ["find", "/workspace/archive", "-name", "*.dat", "extra"]
    ) is None


def test_find_template_applies_enum_and_regex_constraints(settings):
    store = ApprovalStore(settings)
    template = store.create_template(
        ["render", "json", "report-123"],
        {
            1: TemplateConstraint("enum", ["json", "text"]),
            2: TemplateConstraint("regex", r"report-\d+"),
        },
    )

    assert store.find_template(["render", "text", "report-456"]).id == template.id
    assert store.find_template(["render", "csv", "report-456"]) is None
    assert store.find_template(["render", "text", "prefix-report-456"]) is None


def test_template_usage_listing_format_and_revoke(settings):
    store = ApprovalStore(settings)
    template = store.create_template(
        ["convert", "/workspace/input.png", "json"],
        {
            1: TemplateConstraint("path_under_workdir"),
            2: TemplateConstraint("enum", ["json", "text"]),
        },
    )

    store.mark_template_used(template.id)

    listed = store.list_templates()[0]
    assert listed.use_count == 1
    assert listed.last_used_at is not None
    assert format_template(listed) == (
        "convert <path under /workspace> <enum: json | text>"
    )
    assert store.revoke_template(template.id) is True
    assert store.find_template(["convert", "/workspace/other.png", "text"]) is None
    assert store.revoke_template(template.id) is False
