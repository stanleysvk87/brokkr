from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from docker.errors import APIError, NotFound

from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import DockerSandbox, SandboxError


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(network="none"),
        approval_template_matching=False,
    )


def _sandbox_with_fakes(monkeypatch, settings, exec_run):
    events: list[str] = []

    class FakeBridge:
        def connect(self, container):
            events.append("connect")

        def disconnect(self, container):
            events.append("disconnect")

    container = SimpleNamespace(
        id="container-id",
        image=SimpleNamespace(id="image-id"),
        exec_run=exec_run,
    )
    sandbox = DockerSandbox(settings)
    monkeypatch.setattr(sandbox, "ensure_running", lambda: container)
    monkeypatch.setattr(sandbox, "_touch_last_used_if_current", lambda container_id: None)
    sandbox._client = SimpleNamespace(
        networks=SimpleNamespace(
            get=lambda name: events.append(f"get:{name}") or FakeBridge()
        )
    )
    return sandbox, events


def test_network_approved_exec_connects_runs_and_disconnects(monkeypatch, settings):
    events: list[str] = []

    def exec_run(*args, **kwargs):
        events.append("exec")
        return 0, (b"ok\n", b"")

    sandbox, network_events = _sandbox_with_fakes(monkeypatch, settings, exec_run)

    result = sandbox.exec(["curl", "https://example.com"], network=True)

    assert network_events == ["get:bridge", "connect", "disconnect"]
    assert events == ["exec"]
    assert result.network_enabled is True


def test_default_exec_never_touches_docker_network(monkeypatch, settings):
    sandbox, events = _sandbox_with_fakes(
        monkeypatch,
        settings,
        lambda *args, **kwargs: (0, (b"ok\n", b"")),
    )

    result = sandbox.exec(["ls", "/workspace"])

    assert events == []
    assert result.network_enabled is False


def test_network_disconnects_when_exec_raises(monkeypatch, settings):
    def exec_run(*args, **kwargs):
        raise APIError("forced exec failure")

    sandbox, events = _sandbox_with_fakes(monkeypatch, settings, exec_run)

    with pytest.raises(SandboxError, match="docker exec failed"):
        sandbox.exec(["curl", "https://example.com"], network=True)

    assert events == ["get:bridge", "connect", "disconnect"]


def test_network_disconnects_after_timeout(monkeypatch, settings):
    sandbox, events = _sandbox_with_fakes(
        monkeypatch,
        settings,
        lambda *args, **kwargs: (124, (b"", b"timed out")),
    )

    result = sandbox.exec(["sleep", "10"], timeout=0.1, network=True)

    assert result.timed_out is True
    assert result.network_enabled is True
    assert events == ["get:bridge", "connect", "disconnect"]


def test_persistent_bridge_skips_temporary_connect_and_disconnect(monkeypatch, settings):
    settings = settings.model_copy(
        update={"sandbox": settings.sandbox.model_copy(update={"network": "bridge"})}
    )
    sandbox, events = _sandbox_with_fakes(
        monkeypatch,
        settings,
        lambda *args, **kwargs: (0, (b"ok\n", b"")),
    )

    result = sandbox.exec(["curl", "https://example.com"], network=True)

    assert events == []
    assert result.network_enabled is True


def test_legacy_none_container_migrates_to_internal_network(monkeypatch, settings):
    events: list[str] = []
    container = SimpleNamespace(
        attrs={"NetworkSettings": {"Networks": {"none": {}}}},
        reload=lambda: events.append("reload"),
    )
    isolated = SimpleNamespace(
        connect=lambda target: events.append("connect:internal"),
        reload=lambda: None,
        attrs={
            "Internal": True,
            "Driver": "bridge",
            "Labels": {"org.brokkr.network": "isolated"},
        },
    )
    none_network = SimpleNamespace(
        disconnect=lambda target: events.append("disconnect:none")
    )
    sandbox = DockerSandbox(settings)
    sandbox._client = SimpleNamespace(
        networks=SimpleNamespace(
            get=lambda name: isolated if name == "brokkr-sandbox-internal" else none_network
        )
    )

    sandbox._migrate_legacy_none_network(container)

    assert events == ["disconnect:none", "connect:internal", "reload"]


def test_rejects_same_named_network_without_internal_safety_properties(
    monkeypatch, settings
):
    unsafe = SimpleNamespace(
        reload=lambda: None,
        attrs={"Internal": False, "Driver": "bridge", "Labels": {}},
    )
    sandbox = DockerSandbox(settings)
    sandbox._client = SimpleNamespace(networks=SimpleNamespace(get=lambda name: unsafe))

    with pytest.raises(SandboxError, match="not brokkr's internal-only network"):
        sandbox._ensure_isolated_network()


def test_creates_labeled_internal_network_when_missing(settings):
    captured: dict = {}
    created = SimpleNamespace(
        reload=lambda: None,
        attrs={
            "Internal": True,
            "Driver": "bridge",
            "Labels": {"org.brokkr.network": "isolated"},
        },
    )

    def create(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return created

    sandbox = DockerSandbox(settings)
    sandbox._client = SimpleNamespace(
        networks=SimpleNamespace(
            get=lambda name: (_ for _ in ()).throw(NotFound("missing")),
            create=create,
        )
    )

    assert sandbox._ensure_isolated_network() is created
    assert captured == {
        "name": "brokkr-sandbox-internal",
        "driver": "bridge",
        "internal": True,
        "check_duplicate": True,
        "labels": {"org.brokkr.network": "isolated"},
    }


def test_ordinary_exec_cannot_overlap_temporary_network_attachment(monkeypatch, settings):
    network_sandbox = DockerSandbox(settings)
    ordinary_sandbox = DockerSandbox(settings)
    network_started = threading.Event()
    ordinary_started = threading.Event()
    release_network = threading.Event()

    def network_exec(*args, **kwargs):
        network_started.set()
        assert release_network.wait(timeout=2)
        return 0, (b"network\n", b"")

    def ordinary_exec(*args, **kwargs):
        ordinary_started.set()
        return 0, (b"ordinary\n", b"")

    bridge = SimpleNamespace(connect=lambda container: None, disconnect=lambda container: None)
    network_container = SimpleNamespace(
        id="network-container",
        image=SimpleNamespace(id="image"),
        exec_run=network_exec,
    )
    ordinary_container = SimpleNamespace(
        id="ordinary-container",
        image=SimpleNamespace(id="image"),
        exec_run=ordinary_exec,
    )
    monkeypatch.setattr(network_sandbox, "ensure_running", lambda: network_container)
    monkeypatch.setattr(ordinary_sandbox, "ensure_running", lambda: ordinary_container)
    monkeypatch.setattr(network_sandbox, "_touch_last_used_if_current", lambda value: None)
    monkeypatch.setattr(ordinary_sandbox, "_touch_last_used_if_current", lambda value: None)
    network_sandbox._client = SimpleNamespace(
        networks=SimpleNamespace(get=lambda name: bridge)
    )

    network_thread = threading.Thread(
        target=lambda: network_sandbox.exec(["curl", "https://example.com"], network=True)
    )
    ordinary_thread = threading.Thread(target=lambda: ordinary_sandbox.exec(["true"]))
    network_thread.start()
    assert network_started.wait(timeout=2)
    ordinary_thread.start()
    time.sleep(0.05)

    assert ordinary_started.is_set() is False

    release_network.set()
    network_thread.join(timeout=2)
    ordinary_thread.join(timeout=2)
    assert not network_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert ordinary_started.is_set() is True
