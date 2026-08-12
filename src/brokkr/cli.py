"""brokkr CLI entry point.

Stage 1: direct sandbox control (`brokkr sandbox ...`), no LLM yet. Every
sandbox execution goes through AuditStore, so `brokkr sandbox exec` is
also the tool used to manually verify the sandbox's own safety guarantees
(see the Stage 1 verification checklist in
~/.claude/plans/partitioned-fluttering-sonnet.md) -- rm -rf /, a hung
process, a network call -- before any LLM is ever wired in to propose
commands on its own.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from brokkr import __version__
from brokkr.audit.store import AuditStore
from brokkr.config import load_settings
from brokkr.sandbox.docker_sandbox import DockerSandbox, SandboxError

app = typer.Typer(help="brokkr -- a local, sandboxed, tool-augmented LLM agent.")
sandbox_app = typer.Typer(help="Direct control of the Docker sandbox (Stage 1, no LLM).")
app.add_typer(sandbox_app, name="sandbox")

console = Console()


@app.command()
def version() -> None:
    """Print the installed brokkr version."""
    typer.echo(__version__)


@sandbox_app.command("exec")
def sandbox_exec(
    argv: list[str] = typer.Argument(
        ...,
        help="Command and arguments to run inside the sandbox, "
        "e.g.: brokkr sandbox exec -- ls -la /workspace",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Override the configured per-command timeout (seconds)."
    ),
) -> None:
    """Runs a command directly inside the sandbox container. No LLM, no
    approval flow -- you're typing the exact command yourself. Every run
    is fully recorded (logs/audit.db, logs/blobs/, logs/audit.jsonl)."""
    settings = load_settings()
    sandbox = DockerSandbox(settings)
    audit = AuditStore(settings)

    try:
        result = sandbox.exec(argv, timeout=timeout)
    except SandboxError as exc:
        console.print(f"[red]sandbox error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    command_id = audit.record_execution(result, source="manual")

    if result.stdout:
        console.print(result.stdout, end="")
    if result.stderr:
        console.print(f"[red]{result.stderr}[/red]", end="")

    status = "timed out" if result.timed_out else f"exit {result.exit_code}"
    console.print(f"\n[dim]-- {status}, {result.duration_ms:.0f}ms, command_id={command_id}[/dim]")
    raise typer.Exit(code=124 if result.timed_out else result.exit_code)


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Stops and removes the sandbox container. The next command creates
    it fresh -- new rootfs, nothing carried over."""
    settings = load_settings()
    sandbox = DockerSandbox(settings)
    sandbox.reset()
    console.print("[green]sandbox container reset[/green]")


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Shows whether the sandbox container exists and its current state."""
    settings = load_settings()
    sandbox = DockerSandbox(settings)
    container = sandbox.existing_container()
    if container is None:
        console.print("[yellow]no sandbox container (not created yet)[/yellow]")
        return
    container.reload()
    table = Table(show_header=False)
    table.add_row("container", container.name)
    table.add_row("id", container.id[:12])
    table.add_row("status", container.status)
    table.add_row("image", settings.sandbox.image)
    console.print(table)


if __name__ == "__main__":
    app()
