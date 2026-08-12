"""brokkr CLI entry point.

`brokkr sandbox ...` (Stage 1) is direct, no-LLM sandbox control -- also
the tool used to manually verify the sandbox's own safety guarantees
(see the Stage 1 verification checklist in
~/.claude/plans/partitioned-fluttering-sonnet.md) -- rm -rf /, a hung
process, a network call -- and it deliberately has no policy blocklist
gate of its own, since that verification depends on catastrophic
commands actually reaching the sandbox mechanism.

`brokkr propose` (Stage 2+3) is where a model gets involved: it proposes
a command, a human approves/edits/rejects it, and only *then* -- right
before anything runs -- does permissions/policy.py's static blocklist get
checked, as defense in depth on top of the Docker boundary that Stage 1
already proved. If a human has previously chosen to remember an *exact*
argv (see approvals/store.py -- never a fuzzy/semantic match), a later
identical proposal skips the confirmation prompt entirely; anything else
is reviewed fresh every time.

`brokkr approvals ...` lists and revokes remembered commands.
`brokkr memory ...` explicitly manages human-curated workspace context.
"""

from __future__ import annotations

import shlex

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from brokkr import __version__
from brokkr.approvals.store import ApprovalStore
from brokkr.audit.store import AuditStore
from brokkr.config import load_settings
from brokkr.llm.client import OllamaClient
from brokkr.memory.store import MemoryStore
from brokkr.permissions.policy import check_prohibited
from brokkr.sandbox.docker_sandbox import DockerSandbox, SandboxError

app = typer.Typer(help="brokkr -- a local, sandboxed, tool-augmented LLM agent.")
sandbox_app = typer.Typer(help="Direct control of the Docker sandbox (Stage 1, no LLM).")
approvals_app = typer.Typer(help="List and revoke remembered (auto-approved) commands.")
memory_app = typer.Typer(help="Manage human-curated context for future proposals.")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(approvals_app, name="approvals")
app.add_typer(memory_app, name="memory")

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

    command_id = audit.new_command_id()
    try:
        result = sandbox.exec(argv, timeout=timeout)
    except SandboxError as exc:
        console.print(f"[red]sandbox error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    audit.record_execution(command_id, result, source="manual")

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


@app.command()
def propose(
    task: str = typer.Argument(..., help="Plain-language description of what to do."),
    model: str | None = typer.Option(
        None, "--model", help="Override the configured default Ollama model."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Override the configured per-command sandbox timeout (seconds)."
    ),
) -> None:
    """Asks the model to propose a command for TASK, shows you its
    reasoning, and lets you run it as-is, edit it, or reject it -- unless
    that exact command was already remembered from an earlier session, in
    which case it runs without asking again. Nothing else runs without
    confirmation, and the proposal, your decision, and (if anything ran)
    the execution are all recorded under one shared command_id -- see
    logs/audit.db."""
    settings = load_settings()
    audit = AuditStore(settings)
    approvals = ApprovalStore(settings)
    memory = MemoryStore(settings)
    command_id = audit.new_command_id()

    client = OllamaClient(settings)
    notes = memory.recent(settings.memory_max_notes)
    result = client.propose(task, model=model, notes=[entry.note for entry in notes])
    audit.record_proposal(command_id, result)

    if result.error:
        console.print(f"[red]proposal failed:[/red] {result.error}")
        raise typer.Exit(code=1)

    proposal = result.proposal
    assert proposal is not None  # result.error is None, so propose() guarantees this
    console.print(f"[dim]reasoning:[/dim] {proposal.reasoning}")
    console.print(f"[bold]command:[/bold] {shlex.join(proposal.argv)}")

    remembered = approvals.find(proposal.argv)
    if remembered is not None:
        console.print("[cyan](remembered -- running without asking)[/cyan]")
        final_argv = proposal.argv
        decision = "auto_approved"
    else:
        choice = Prompt.ask(
            "Run this? [y]es / [e]dit / [n]o", choices=["y", "e", "n"], default="n"
        )

        if choice == "n":
            audit.record_decision(command_id, "rejected", None)
            console.print("[yellow]rejected, nothing ran[/yellow]")
            raise typer.Exit(code=0)

        if choice == "e":
            edited = Prompt.ask("Edit command", default=shlex.join(proposal.argv))
            final_argv = shlex.split(edited)
            decision = "edited"
        else:
            final_argv = proposal.argv
            decision = "approved"

    # Always checked, even for a remembered command -- policy.py can gain
    # new rules after a command was remembered under an older version.
    blocked_reason = check_prohibited(final_argv)
    if blocked_reason is not None:
        audit.record_decision(command_id, "blocked", final_argv, reason=blocked_reason)
        console.print(f"[red]blocked by policy:[/red] {blocked_reason}")
        raise typer.Exit(code=1)

    audit.record_decision(command_id, decision, final_argv)

    if remembered is not None:
        approvals.mark_used(remembered.command_hash)

    # Execution happens before the "remember?" follow-up question,
    # deliberately -- a human dogfooding this surfaced a real bug where
    # the opposite order meant an interrupted/EOF'd answer to "remember?"
    # (Ctrl-D, Ctrl-C, or stdin simply closing) aborted the whole command
    # with a bare "Aborted.", silently discarding an ALREADY-APPROVED
    # command that had a recorded "approved" decision but never actually
    # ran. Once a command is approved, nothing optional should be able to
    # prevent it from running.
    sandbox = DockerSandbox(settings)
    try:
        exec_result = sandbox.exec(final_argv, timeout=timeout)
    except SandboxError as exc:
        console.print(f"[red]sandbox error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    audit.record_execution(command_id, exec_result, source=f"llm_{decision}")

    if exec_result.stdout:
        console.print(exec_result.stdout, end="")
    if exec_result.stderr:
        console.print(f"[red]{exec_result.stderr}[/red]", end="")

    status = "timed out" if exec_result.timed_out else f"exit {exec_result.exit_code}"
    console.print(
        f"\n[dim]-- {status}, {exec_result.duration_ms:.0f}ms, command_id={command_id}[/dim]"
    )

    if remembered is None:
        # Broad catch is deliberate: this question is purely optional
        # follow-up after the real work already happened and was already
        # reported above -- nothing raised here should change this
        # command's outcome or exit code.
        try:
            if Confirm.ask(
                "Remember this exact command so it skips confirmation next time?",
                default=False,
            ):
                approvals.remember(final_argv, task_description=task)
        except Exception:  # noqa: BLE001, S110 -- deliberately blind, see comment above
            pass

    raise typer.Exit(code=124 if exec_result.timed_out else exec_result.exit_code)


@approvals_app.command("list")
def approvals_list() -> None:
    """Lists every remembered (auto-approved) exact command."""
    settings = load_settings()
    approvals = ApprovalStore(settings)
    remembered = approvals.list_all()
    if not remembered:
        console.print("[yellow]no remembered commands[/yellow]")
        return

    table = Table("id", "command", "task", "used", "created")
    for entry in remembered:
        table.add_row(
            str(entry.id),
            shlex.join(entry.argv),
            entry.task_description or "",
            str(entry.use_count),
            entry.created_at,
        )
    console.print(table)


@approvals_app.command("revoke")
def approvals_revoke(
    approval_id: int = typer.Argument(..., help="id shown by `brokkr approvals list`."),
) -> None:
    """Forgets a remembered command -- it goes back to asking for
    confirmation every time."""
    settings = load_settings()
    approvals = ApprovalStore(settings)
    if approvals.revoke(approval_id):
        console.print(f"[green]revoked approval {approval_id}[/green]")
    else:
        console.print(f"[yellow]no approval with id {approval_id}[/yellow]")
        raise typer.Exit(code=1)


@memory_app.command("add")
def memory_add(note: str = typer.Argument(..., help="Workspace context to remember.")) -> None:
    """Adds an explicit human-authored note for future proposals."""
    settings = load_settings()
    entry = MemoryStore(settings).add(note)
    console.print(f"[green]added memory {entry.id}[/green]")


@memory_app.command("list")
def memory_list() -> None:
    """Lists workspace notes, most recent first."""
    settings = load_settings()
    notes = MemoryStore(settings).list_all()
    if not notes:
        console.print("[yellow]no memory notes[/yellow]")
        return

    table = Table("id", "note", "created")
    for entry in notes:
        table.add_row(str(entry.id), entry.note, entry.created_at)
    console.print(table)


@memory_app.command("forget")
def memory_forget(
    note_id: int = typer.Argument(..., help="id shown by `brokkr memory list`."),
) -> None:
    """Removes a workspace note from future proposal context."""
    settings = load_settings()
    if MemoryStore(settings).forget(note_id):
        console.print(f"[green]forgot memory {note_id}[/green]")
    else:
        console.print(f"[yellow]no memory with id {note_id}[/yellow]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
