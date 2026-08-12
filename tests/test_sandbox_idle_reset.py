"""Locks in the idle-reset marker logic (_read_last_used/_touch_last_used/
_is_idle_expired) in isolation from Docker itself -- constructing a
DockerSandbox doesn't require a live daemon (docker.from_env() only
connects lazily on first API call), so these pure filesystem/time checks
can be tested without one."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox


@pytest.fixture
def sandbox(tmp_path) -> DockerSandbox:
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(idle_reset_minutes=30.0),
        approval_template_matching=False,
    )
    return DockerSandbox(settings)


def test_not_expired_when_never_used(sandbox):
    assert sandbox._is_idle_expired() is False


def test_not_expired_within_window(sandbox):
    sandbox._touch_last_used()
    assert sandbox._is_idle_expired() is False


def test_expired_past_window(sandbox):
    stale = datetime.now(timezone.utc) - timedelta(minutes=31)
    sandbox._settings.sandbox_last_used_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox._settings.sandbox_last_used_path.write_text(stale.isoformat())
    assert sandbox._is_idle_expired() is True


def test_disabled_when_idle_reset_minutes_not_positive(tmp_path):
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(idle_reset_minutes=0.0),
        approval_template_matching=False,
    )
    sandbox = DockerSandbox(settings)
    stale = datetime.now(timezone.utc) - timedelta(days=1)
    sandbox._settings.sandbox_last_used_path.parent.mkdir(parents=True, exist_ok=True)
    sandbox._settings.sandbox_last_used_path.write_text(stale.isoformat())
    assert sandbox._is_idle_expired() is False


def test_corrupt_marker_file_treated_as_not_expired(sandbox):
    path = sandbox._settings.sandbox_last_used_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a timestamp")
    assert sandbox._is_idle_expired() is False
