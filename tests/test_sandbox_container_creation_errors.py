"""Locks in that a Docker API error during container creation surfaces as
a clean SandboxError, not a raw traceback -- found by deploying brokkr
into a resource-constrained VM where the default BROKKR_SANDBOX_CPU_LIMIT
exceeded what the host actually had available."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from docker.errors import APIError

from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox, SandboxError


@pytest.fixture
def sandbox(tmp_path) -> DockerSandbox:
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )
    return DockerSandbox(settings)


def test_container_creation_api_error_becomes_sandbox_error(sandbox, monkeypatch):
    def _raise_api_error(*args, **kwargs):
        raise APIError("400 Client Error: Bad Request (range of CPUs is from 0.01 to 2.00)")

    monkeypatch.setattr(sandbox, "_get_container", lambda: None)
    monkeypatch.setattr(sandbox, "build_image", lambda: "image-id")
    isolated = SimpleNamespace(
        name="brokkr-sandbox-internal",
        reload=lambda: None,
        attrs={
            "Internal": True,
            "Driver": "bridge",
            "Labels": {"org.brokkr.network": "isolated"},
        },
    )
    sandbox._client = SimpleNamespace(
        containers=SimpleNamespace(run=_raise_api_error),
        networks=SimpleNamespace(get=lambda name: isolated),
    )

    with pytest.raises(SandboxError, match="failed to create sandbox container"):
        sandbox.ensure_running()


def test_container_creation_enables_init_reaper(sandbox, monkeypatch):
    captured = {}
    container = SimpleNamespace(id="container-id")

    def _run(*args, **kwargs):
        captured.update(kwargs)
        return container

    monkeypatch.setattr(sandbox, "_get_container", lambda: None)
    monkeypatch.setattr(sandbox, "build_image", lambda: "image-id")
    isolated = SimpleNamespace(
        name="brokkr-sandbox-internal",
        reload=lambda: None,
        attrs={
            "Internal": True,
            "Driver": "bridge",
            "Labels": {"org.brokkr.network": "isolated"},
        },
    )
    sandbox._client = SimpleNamespace(
        containers=SimpleNamespace(run=_run),
        networks=SimpleNamespace(get=lambda name: isolated),
    )

    assert sandbox.ensure_running() is container
    assert captured["init"] is True
    assert captured["network_mode"] == "brokkr-sandbox-internal"
