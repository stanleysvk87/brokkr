from __future__ import annotations

from types import SimpleNamespace

import typer
from typer.testing import CliRunner

from brokkr import cli


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
