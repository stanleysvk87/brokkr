"""Locks in cross-process serialization of sandbox container lifecycle."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox


class _FakeContainer:
    id = "container-id"
    status = "running"

    def reload(self) -> None:
        pass


def test_concurrent_first_use_creates_named_container_once(tmp_path, monkeypatch):
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )
    sandboxes = [DockerSandbox(settings), DockerSandbox(settings)]
    created = None
    create_count = 0
    state_lock = threading.Lock()
    start = threading.Barrier(2)

    def get_container():
        with state_lock:
            return created

    def run(*args, **kwargs):
        nonlocal created, create_count
        with state_lock:
            create_count += 1
        time.sleep(0.05)
        with state_lock:
            created = _FakeContainer()
            return created

    for sandbox in sandboxes:
        monkeypatch.setattr(sandbox, "_get_container", get_container)
        monkeypatch.setattr(sandbox, "build_image", lambda: "image-id")
        sandbox._client = SimpleNamespace(containers=SimpleNamespace(run=run))

    results = []

    def ensure(sandbox):
        start.wait()
        results.append(sandbox.ensure_running())

    threads = [threading.Thread(target=ensure, args=(sandbox,)) for sandbox in sandboxes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert create_count == 1
    assert results == [created, created]


def test_completed_exec_cannot_restore_marker_after_container_was_removed(
    tmp_path, monkeypatch
):
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )
    sandbox = DockerSandbox(settings)
    monkeypatch.setattr(sandbox, "_get_container", lambda: None)

    sandbox._touch_last_used_if_current("removed-container-id")

    assert not settings.sandbox_last_used_path.exists()
