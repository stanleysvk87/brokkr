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
from brokkr.llm.client import _SYSTEM_PROMPT, OllamaClient


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
    assert result.proposal.needs_network is False


def test_propose_parses_optional_needs_network_flag(monkeypatch, settings):
    valid_content = json.dumps(
        {"reasoning": "fetch it", "argv": ["curl", "https://example.com"], "needs_network": True}
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(valid_content))

    result = OllamaClient(settings).propose("check a URL")

    assert result.error is None
    assert result.proposal is not None
    assert result.proposal.needs_network is True


@pytest.mark.parametrize("notes", [None, []])
def test_propose_without_notes_preserves_existing_payload(monkeypatch, settings, notes):
    valid_content = json.dumps({"reasoning": "because", "argv": ["ls"]})
    captured = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs["json"])
        return _fake_response(valid_content)

    monkeypatch.setattr(httpx, "post", _capture)

    OllamaClient(settings).propose("list files", notes=notes)

    assert captured["messages"] == [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "list files"},
    ]


def test_propose_includes_notes_in_chronological_order(monkeypatch, settings):
    valid_content = json.dumps({"reasoning": "because", "argv": ["ls"]})
    captured = {}

    def _capture(*args, **kwargs):
        captured.update(kwargs["json"])
        return _fake_response(valid_content)

    monkeypatch.setattr(httpx, "post", _capture)

    OllamaClient(settings).propose(
        "list files", notes=["uses Python", "tests run with pytest"]
    )

    assert captured["messages"][1]["role"] == "system"
    assert captured["messages"][1]["content"].endswith(
        "- uses Python\n- tests run with pytest"
    )
    assert captured["messages"][2] == {"role": "user", "content": "list files"}


def test_propose_errors_on_malformed_json(monkeypatch, settings):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response("not json at all"))

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert result.error is not None
    assert result.user_error == (
        "The model returned a response brokkr could not parse. Try rephrasing the task."
    )


def test_propose_errors_on_empty_argv(monkeypatch, settings):
    invalid_content = json.dumps({"reasoning": "because", "argv": []})
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(invalid_content))

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert result.error is not None
    assert result.user_error == "The model did not propose a command. Try rephrasing the task."


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
    assert result.user_error == (
        "The model proposed a shell operator outside a shell, so brokkr rejected it. "
        "Try rephrasing the task."
    )


def test_propose_accepts_separate_find_exec_terminator(monkeypatch, settings):
    valid_content = json.dumps(
        {
            "reasoning": "show file sizes",
            "argv": [
                "find",
                "/workspace",
                "-type",
                "f",
                "-exec",
                "stat",
                "-c",
                "%s %n",
                "{}",
                ";",
            ],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(valid_content))

    result = OllamaClient(settings).propose("show file sizes")

    assert result.error is None
    assert result.proposal is not None
    assert result.proposal.argv[-2:] == ["{}", ";"]


def test_propose_rejects_extra_shell_operator_after_find_exec(monkeypatch, settings):
    invalid_content = json.dumps(
        {
            "reasoning": "invalid extra command separator",
            "argv": ["find", "/workspace", "-exec", "echo", "{}", ";", ";", "id"],
        }
    )
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _fake_response(invalid_content))

    result = OllamaClient(settings).propose("find files")

    assert result.proposal is None
    assert "bare shell operator" in result.error


def test_propose_errors_on_connection_failure(monkeypatch, settings):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)

    result = OllamaClient(settings).propose("list files")

    assert result.proposal is None
    assert "Ollama request failed" in result.error
    assert result.user_error is not None
    assert "Check that Ollama is running" in result.user_error


def test_system_prompt_steers_network_checks_away_from_ping():
    assert "use curl instead of ping" in _SYSTEM_PROMPT
    assert "no raw-socket capability" in _SYSTEM_PROMPT


def test_system_prompt_explains_that_direct_argv_does_not_expand_globs():
    assert "Argv elements are never expanded by a shell" in _SYSTEM_PROMPT
    assert "a literal string, not a file match" in _SYSTEM_PROMPT
    assert '["du", "-sh", "/workspace/reports/*"]' in _SYSTEM_PROMPT
    assert '"-exec", "du", "-sh", "{}", ";"' in _SYSTEM_PROMPT


def test_system_prompt_steers_away_from_dmesg():
    # Same underlying cause as ping (CAP_SYSLOG dropped, same as
    # CAP_NET_RAW) -- found by dogfooding a "check kernel messages" task
    # right after the ping fix, same category not a new one.
    assert "dmesg" in _SYSTEM_PROMPT
    assert "CAP_SYSLOG" in _SYSTEM_PROMPT


def test_system_prompt_recommends_installed_pdf_and_ocr_tools():
    assert "Use pdftotext to extract" in _SYSTEM_PROMPT
    assert "pdftoppm" in _SYSTEM_PROMPT
    assert "tesseract for OCR" in _SYSTEM_PROMPT


def test_system_prompt_prefers_discovery_over_guessing_file_paths():
    assert "Never infer spaces, underscores, or an extension" in _SYSTEM_PROMPT
    assert "propose only a read-only ls or find without -exec or -delete" in _SYSTEM_PROMPT
    assert "Never include rm, mv" in _SYSTEM_PROMPT


def test_system_prompt_forbids_guessing_destructive_directory_paths():
    assert "plausible-sounding directory name" in _SYSTEM_PROMPT
    assert "Hard rule" in _SYSTEM_PROMPT
    assert "even when your reasoning acknowledges" in _SYSTEM_PROMPT
    assert "Do not turn a description into an invented candidate path" in _SYSTEM_PROMPT
    assert '"-iname", "*reports*", "-print"' in _SYSTEM_PROMPT


def test_system_prompt_explains_find_exec_argv_shape():
    assert "'{}' and its terminator (';' or '+') must be separate argv elements" in _SYSTEM_PROMPT
    assert '"{}", ";"' in _SYSTEM_PROMPT


def test_system_prompt_uses_targeted_read_only_deletion_previews():
    assert "deletion preview or dry run" in _SYSTEM_PROMPT
    assert "find <path>" in _SYSTEM_PROMPT
    assert "du -sh <path>" in _SYSTEM_PROMPT
    assert "do not substitute a generic listing" in _SYSTEM_PROMPT
