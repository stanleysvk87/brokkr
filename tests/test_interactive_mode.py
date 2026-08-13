from __future__ import annotations

from types import SimpleNamespace

import typer
from docker.errors import APIError
from typer.testing import CliRunner

from brokkr import cli
from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import (
    DockerSandbox,
    SandboxExecutionResult,
)


def test_propose_command_and_repl_route_through_same_shared_function(monkeypatch):
    services = object()
    calls = []
    monkeypatch.setattr(cli, "_proposal_services", lambda: services)
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda task, loaded, **options: calls.append((task, loaded, options)),
    )

    cli.propose("single task", model="model-a", timeout=7.0, allow_network=True)
    result = CliRunner().invoke(cli.app, [], input="interactive task\nexit\n")

    assert result.exit_code == 0
    assert calls == [
        (
            "single task",
            services,
            {"model": "model-a", "timeout": 7.0, "allow_network": True},
        ),
        (
            "interactive task",
            services,
            {"model": None, "timeout": None, "allow_network": False},
        ),
    ]


def test_repl_preserves_shell_special_characters_verbatim(monkeypatch):
    task = 'find the file named "notes"; don\'t run x && y \\ literally'
    received = []
    monkeypatch.setattr(cli, "_proposal_services", lambda: object())
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda value, services, **options: received.append(value),
    )

    result = CliRunner().invoke(cli.app, [], input=f"{task}\nquit\n")

    assert result.exit_code == 0
    assert received == [task]


def test_repl_session_flags_apply_to_every_task_and_services_are_built_once(monkeypatch):
    services = object()
    service_builds = []
    calls = []

    def build_services():
        service_builds.append(True)
        return services

    monkeypatch.setattr(cli, "_proposal_services", build_services)
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda task, loaded, **options: calls.append((task, loaded, options)),
    )

    result = CliRunner().invoke(
        cli.app,
        ["--model", "session-model", "--timeout", "12", "--allow-network"],
        input="first task\nsecond task\nexit\n",
    )

    assert result.exit_code == 0
    assert service_builds == [True]
    assert calls == [
        (
            "first task",
            services,
            {"model": "session-model", "timeout": 12.0, "allow_network": True},
        ),
        (
            "second task",
            services,
            {"model": "session-model", "timeout": 12.0, "allow_network": True},
        ),
    ]


def test_repl_exit_and_quit_end_without_proposals(monkeypatch):
    monkeypatch.setattr(cli, "_proposal_services", lambda: object())
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    for command in ("exit", "quit"):
        result = CliRunner().invoke(cli.app, [], input=f"{command}\n")
        assert result.exit_code == 0


def test_repl_eof_ends_cleanly(monkeypatch):
    monkeypatch.setattr(cli, "_proposal_services", lambda: object())
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = CliRunner().invoke(cli.app, [], input="")

    assert result.exit_code == 0
    assert "brokkr interactive mode" in result.output


def test_repl_help_and_blank_lines_do_not_become_tasks(monkeypatch):
    received = []
    monkeypatch.setattr(cli, "_proposal_services", lambda: SimpleNamespace())
    monkeypatch.setattr(
        cli,
        "_run_proposal",
        lambda task, services, **options: received.append(task),
    )

    result = CliRunner().invoke(cli.app, [], input="\nhelp\nreal task\nexit\n")

    assert result.exit_code == 0
    assert received == ["real task"]
    assert "Type one task per line" in result.output


def test_task_exit_status_ends_only_that_repl_turn(monkeypatch):
    received = []
    monkeypatch.setattr(cli, "_proposal_services", lambda: object())

    def run(task, services, **options):
        received.append(task)
        raise typer.Exit(code=1)

    monkeypatch.setattr(cli, "_run_proposal", run)

    result = CliRunner().invoke(cli.app, [], input="first task\nsecond task\nexit\n")

    assert result.exit_code == 0
    assert received == ["first task", "second task"]


def test_invalid_image_error_ends_only_that_repl_turn(monkeypatch, tmp_path):
    settings = Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(image="INVALID IMAGE"),
        approval_template_matching=False,
    )
    bad_sandbox = DockerSandbox.__new__(DockerSandbox)
    bad_sandbox._settings = settings

    def _raise_api_error(image_name):
        raise APIError("400 Client Error: invalid reference format")

    bad_sandbox._client = SimpleNamespace(images=SimpleNamespace(get=_raise_api_error))

    class FirstSandbox:
        def exec(self, argv, timeout=None, network=False):
            bad_sandbox.build_image()

    successful_result = SandboxExecutionResult(
        command=["printf", "ok"],
        exit_code=0,
        timed_out=False,
        truncated=False,
        stdout="second task ran\n",
        stderr="",
        duration_ms=1.0,
        container_id="container-id",
        image_id="image-id",
    )
    sandboxes = iter([FirstSandbox(), SimpleNamespace(exec=lambda *args, **kwargs: successful_result)])
    monkeypatch.setattr(cli, "DockerSandbox", lambda loaded_settings: next(sandboxes))

    tasks = []

    class FakeClient:
        def propose(self, task, model=None, notes=None):
            tasks.append(task)
            return SimpleNamespace(
                error=None,
                proposal=SimpleNamespace(
                    reasoning="test",
                    argv=["printf", "ok"],
                    needs_network=False,
                ),
            )

    services = cli.ProposalServices(
        settings=settings,
        audit=SimpleNamespace(
            new_command_id=lambda: "a" * 32,
            record_proposal=lambda *args: None,
            record_decision=lambda *args, **kwargs: None,
            record_execution=lambda *args, **kwargs: None,
        ),
        approvals=SimpleNamespace(
            search_library=lambda task: [],
            find=lambda argv: SimpleNamespace(command_hash="remembered"),
            mark_used=lambda command_hash: None,
        ),
        memory=SimpleNamespace(recent=lambda limit: []),
        client=FakeClient(),
    )
    monkeypatch.setattr(cli, "_proposal_services", lambda: services)

    result = CliRunner().invoke(cli.app, [], input="first task\nsecond task\nexit\n")

    assert result.exit_code == 0
    assert tasks == ["first task", "second task"]
    assert "sandbox error" in result.output
    assert "invalid reference format" in result.output
    assert "second task ran" in result.output


def test_eof_inside_a_sub_prompt_ends_only_that_turn(monkeypatch):
    # Reproduces a real bug found by dogfooding: stdin running out exactly
    # at _run_proposal's own "Run this? [y/e/n/m]" prompt raised EOFError
    # (Rich's Prompt.ask, not typer.Exit), which the REPL loop didn't catch
    # -- it propagated past the loop and crashed the whole session instead
    # of just cancelling that one task.
    received = []
    monkeypatch.setattr(cli, "_proposal_services", lambda: object())

    def run(task, services, **options):
        received.append(task)
        raise EOFError

    monkeypatch.setattr(cli, "_run_proposal", run)

    result = CliRunner().invoke(cli.app, [], input="first task\nsecond task\nexit\n")

    assert result.exit_code == 0
    assert received == ["first task", "second task"]
