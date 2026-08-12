"""Locks in OllamaClient.propose()'s error handling -- it must never
raise, only return a ProposalResult with `error` set, for every failure
mode: unreachable server, and a response that's syntactically valid JSON
but doesn't satisfy the proposal schema (constrained decoding guarantees
the former, never the latter)."""

from __future__ import annotations

import json

import httpx
import pytest

from brokkr.config import SandboxConfig, Settings
from brokkr.llm.client import OllamaClient


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir="/tmp/brokkr-test-data",
        log_dir="/tmp/brokkr-test-logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )


def _fake_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"message": {"content": content}},
        request=httpx.Request("POST", "http://127.0.0.1:11434/api/chat"),
    )


def test_propose_returns_proposal_on_valid_response(monkeypatch, settings):
    valid_content = json.dumps({"reasoning": "because", "argv": ["ls", "-la"]})
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(valid_content))

    result = OllamaClient(settings).propose("list files")

    assert result.error is None
    assert result.proposal is not None
    assert result.proposal.argv == ["ls", "-la"]


def test_propose_errors_on_malformed_json(monkeypatch, settings):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response("not json at all"))

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert result.error is not None


def test_propose_errors_on_empty_argv(monkeypatch, settings):
    invalid_content = json.dumps({"reasoning": "because", "argv": []})
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(invalid_content))

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert result.error is not None


def test_propose_errors_on_bare_shell_operator_token(monkeypatch, settings):
    # Reproduces a real qwen2.5-coder:7b response found by dogfooding:
    # asked to count files, it proposed find ... | wc -l as flat argv
    # instead of wrapping it in ["bash", "-c", "..."].
    invalid_content = json.dumps(
        {
            "reasoning": "because",
            "argv": ["find", "/workspace", "-type", "f", "|", "wc", "-l"],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(invalid_content))

    result = OllamaClient(settings).propose("count files")

    assert result.proposal is None
    assert "bare shell operator" in result.error


def test_propose_errors_on_connection_failure(monkeypatch, settings):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert "Ollama request failed" in result.error
