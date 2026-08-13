"""brokkr CLI entry point.

`brokkr sandbox ...` (Stage 1) is direct, no-LLM sandbox control -- also
the tool used to manually verify the sandbox's own safety guarantees --
rm -rf /, a hung process, a network call -- and it deliberately has no
policy blocklist gate of its own, since that verification depends on
catastrophic commands actually reaching the sandbox mechanism.

`brokkr propose` (Stage 2+) is where a model gets involved: it proposes a
command, a human approves/edits/rejects it, and only *then* -- right before
anything runs -- does permissions/policy.py's static blocklist get checked,
as defense in depth on top of the Docker boundary that Stage 1 already
proved. Exact remembered argv can skip review. When explicitly enabled,
human-authored templates can do the same for constrained variable positions;
the model never selects positions or constraints.

Bare `brokkr` runs repeated independent tasks through that exact same proposal
pipeline; it adds an input loop, not a second approval/execution implementation.

`brokkr approvals ...` lists and revokes remembered commands.
`brokkr memory ...` explicitly manages human-curated workspace context.
`brokkr manual ...` reads results the human redirected into that workspace.
"""

from __future__ import annotations

import shlex
import string
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from brokkr import __version__
from brokkr.approvals.store import (
    ApprovalStore,
    ApprovalTemplate,
    TemplateConstraint,
    TemplateValidationError,
    format_template,
)
from brokkr.audit.store import AuditStore, ManualDecision
from brokkr.config import Settings, load_settings
from brokkr.doctor import DoctorCheck, run_doctor
from brokkr.llm.client import OllamaClient
from brokkr.memory.store import MemoryStore
from brokkr.permissions.policy import check_prohibited
from brokkr.sandbox.docker_sandbox import DockerSandbox, SandboxError

app = typer.Typer(
    help="brokkr -- a local, sandboxed, tool-augmented LLM agent.",
    invoke_without_command=True,
    no_args_is_help=False,
)
sandbox_app = typer.Typer(help="Direct control of the Docker sandbox (Stage 1, no LLM).")
approvals_app = typer.Typer(help="List and revoke remembered (auto-approved) commands.")
memory_app = typer.Typer(help="Manage human-curated context for future proposals.")
manual_app = typer.Typer(help="Inspect results from commands you ran manually.")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(approvals_app, name="approvals")
app.add_typer(memory_app, name="memory")
app.add_typer(manual_app, name="manual")

console = Console()
_MANUAL_ID_LENGTH = 8


@dataclass(frozen=True)
class ProposalServices:
    settings: Settings
    audit: AuditStore
    approvals: ApprovalStore
    memory: MemoryStore
    client: OllamaClient


def _proposal_services(settings: Settings | None = None) -> ProposalServices:
    loaded_settings = settings or load_settings()
    return ProposalServices(
        settings=loaded_settings,
        audit=AuditStore(loaded_settings),
        approvals=ApprovalStore(loaded_settings),
        memory=MemoryStore(loaded_settings),
        client=OllamaClient(loaded_settings),
    )


def _print_doctor_check(check: DoctorCheck) -> None:
    labels = {
        "pass": ("green", "PASS"),
        "warn": ("yellow", "WARN"),
        "fail": ("red", "FAIL"),
    }
    color, label = labels[check.status]
    console.print(f"[{color}]{label}[/{color}] {check.name}: ", end="")
    console.print(check.message, markup=False, highlight=False)


def _format_model_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"
    return f"{size_bytes / 1_000_000_000:.1f} GB"


def _compact_text(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _manual_result_path(settings, command_id: str) -> Path:
    return settings.sandbox.workdir_host / f"manual-{command_id[:_MANUAL_ID_LENGTH]}.txt"


def _display_path(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def _shell_path(path: Path) -> str:
    try:
        relative = path.relative_to(Path.home())
        return f"~/{shlex.quote(str(relative))}"
    except ValueError:
        return shlex.quote(str(path))


def _show_manual_instructions(settings, command_id: str, argv: list[str]) -> None:
    short_id = command_id[:_MANUAL_ID_LENGTH]
    result_path = _manual_result_path(settings, command_id)
    command = shlex.join(argv)
    console.print("\n[bold]To run this yourself:[/bold]")
    console.print(f"  {command}", markup=False, highlight=False)
    console.print(
        "\nIf you want brokkr to see the result, redirect the output into your workspace, e.g.:"
    )
    console.print(
        f"  {command} > {_shell_path(result_path)}", markup=False, highlight=False
    )
    console.print(
        f"\nThen check it with: brokkr manual show {short_id}",
        markup=False,
        highlight=False,
    )


def _template_constraint_summary(template: ApprovalTemplate) -> str:
    descriptions: list[str] = []
    for position, part in enumerate(template.parts):
        constraint = part.variable
        if constraint is None:
            continue
        if constraint.constraint_type == "path_under_workdir":
            detail = "path_under_workdir"
        elif constraint.constraint_type == "enum":
            assert isinstance(constraint.value, list)
            detail = f"enum({', '.join(constraint.value)})"
        else:
            detail = f"regex({constraint.value})"
        descriptions.append(f"{position}: {detail}")
    return "; ".join(descriptions)


def _create_template_interactively(approvals: ApprovalStore, argv: list[str]) -> None:
    console.print("\n[bold]Command positions:[/bold]")
    for position, value in enumerate(argv):
        console.print(f"  {position}: {value}", markup=False, highlight=False)

    raw_positions = Prompt.ask("Variable position numbers (comma-separated)")
    try:
        positions = sorted({int(value.strip()) for value in raw_positions.split(",")})
    except ValueError:
        console.print("[red]template not saved: positions must be comma-separated integers[/red]")
        return

    variables: dict[int, TemplateConstraint] = {}
    for position in positions:
        if position < 0 or position >= len(argv):
            console.print(f"[red]template not saved: position {position} is outside this argv[/red]")
            return
        constraint_type = Prompt.ask(
            f"Constraint for position {position}",
            choices=["path_under_workdir", "enum", "regex"],
        )
        if constraint_type == "path_under_workdir":
            constraint = TemplateConstraint(constraint_type)
        elif constraint_type == "enum":
            raw_values = Prompt.ask("Allowed values (comma-separated)")
            allowed_values = [value.strip() for value in raw_values.split(",") if value.strip()]
            constraint = TemplateConstraint(constraint_type, allowed_values)
        else:
            constraint = TemplateConstraint(constraint_type, Prompt.ask("Regular expression"))
        variables[position] = constraint

    try:
        template = approvals.create_template(argv, variables)
    except TemplateValidationError as exc:
        console.print(f"[red]template not saved:[/red] {exc}")
        return

    console.print(
        f"template {template.id} saved: {format_template(template)}",
        markup=False,
        highlight=False,
    )


def _resolve_manual_decision(audit: AuditStore, command_id_prefix: str) -> ManualDecision:
    prefix = command_id_prefix.strip().lower()
    if not prefix or any(character not in string.hexdigits for character in prefix):
        console.print("[red]manual id must be a hexadecimal command-id prefix[/red]")
        raise typer.Exit(code=1)

    matches = audit.find_manual_decisions(prefix)
    if not matches:
        console.print(f"[yellow]no manual decision matches {prefix}[/yellow]")
        raise typer.Exit(code=1)
    if len(matches) > 1:
        console.print(f"[yellow]manual id {prefix} is ambiguous; use more characters[/yellow]")
        raise typer.Exit(code=1)
    return matches[0]


@app.command()
def version() -> None:
    """Print the installed brokkr version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check local setup health without changing or executing anything."""
    report = run_doctor(load_settings())
    for check in report.checks:
        _print_doctor_check(check)

    if report.models:
        table = Table(title="Local Ollama models")
        table.add_column("Name")
        table.add_column("Size", justify="right")
        for model in report.models:
            table.add_row(model.name, _format_model_size(model.size_bytes))
        console.print(table)
    else:
        console.print("Local Ollama models: none available to list")

    console.print("\nTypical model size guidance:")
    console.print("  3B-4B models: roughly 3-5 GB VRAM")
    console.print("  7B-8B models: roughly 5-8 GB VRAM")
    console.print("  13B-14B models: roughly 10-16 GB VRAM")
    console.print("  Smaller quantizations or CPU inference can work, but will be slower.")

    passed = sum(check.status == "pass" for check in report.checks)
    warned = sum(check.status == "warn" for check in report.checks)
    failed = sum(check.status == "fail" for check in report.checks)
    warning_label = "warning" if warned == 1 else "warnings"
    console.print(f"\nSummary: {passed} passed, {warned} {warning_label}, {failed} failed")
    if report.failed:
        raise typer.Exit(code=1)


@app.command()
def history(
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        help="Maximum number of recent proposal entries to show.",
    ),
    decision: str | None = typer.Option(
        None,
        "--decision",
        help="Show only one exact decision type, such as blocked or rejected.",
    ),
) -> None:
    """List recent proposal decisions and outcomes from the audit trail."""
    entries = AuditStore(load_settings()).list_history(limit=limit, decision=decision)
    if not entries:
        message = "no matching history" if decision is not None else "no history yet"
        console.print(f"[yellow]{message}[/yellow]")
        return

    table = Table()
    table.add_column("id", width=_MANUAL_ID_LENGTH, no_wrap=True)
    table.add_column("task", max_width=24, no_wrap=True, overflow="ellipsis")
    table.add_column("decision", max_width=15, no_wrap=True, overflow="ellipsis")
    table.add_column("outcome", max_width=20, no_wrap=True, overflow="ellipsis")
    for entry in entries:
        table.add_row(
            entry.command_id[:_MANUAL_ID_LENGTH],
            _compact_text(entry.task_description, 48),
            entry.displayed_decision,
            _compact_text(entry.outcome, 44),
        )
    console.print(table)


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
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Temporarily attach network access for this execution only.",
    ),
) -> None:
    """Runs a command directly inside the sandbox container. No LLM, no
    approval flow -- you're typing the exact command yourself. Every run
    is fully recorded (logs/audit.db, logs/blobs/, logs/audit.jsonl)."""
    settings = load_settings()
    sandbox = DockerSandbox(settings)
    audit = AuditStore(settings)

    command_id = audit.new_command_id()
    if allow_network:
        console.print("[yellow]network access enabled for this execution[/yellow]")
    try:
        result = sandbox.exec(argv, timeout=timeout, network=allow_network)
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


def _run_proposal(
    task: str,
    services: ProposalServices,
    *,
    model: str | None = None,
    timeout: float | None = None,
    allow_network: bool = False,
) -> None:
    """Run one task through the shared proposal, approval, and execution pipeline."""
    settings = services.settings
    audit = services.audit
    approvals = services.approvals
    memory = services.memory
    client = services.client
    command_id = audit.new_command_id()

    notes = memory.recent(settings.memory_max_notes)
    result = client.propose(task, model=model, notes=[entry.note for entry in notes])
    audit.record_proposal(command_id, result)

    if result.error:
        console.print(f"[red]proposal failed:[/red] {result.user_error or result.error}")
        raise typer.Exit(code=1)

    proposal = result.proposal
    assert proposal is not None  # result.error is None, so propose() guarantees this
    console.print(f"[dim]reasoning:[/dim] {proposal.reasoning}")
    console.print(f"[bold]command:[/bold] {shlex.join(proposal.argv)}")
    if allow_network:
        console.print("[yellow]network access enabled for this execution[/yellow]")
    elif proposal.needs_network:
        console.print(
            "[yellow]the model reports this command may need network access; "
            "rerun with --allow-network to grant it[/yellow]"
        )

    remembered = approvals.find(proposal.argv)
    matched_template = None
    if remembered is None and settings.approval_template_matching:
        matched_template = approvals.find_template(proposal.argv)

    if remembered is not None:
        console.print("[cyan](remembered -- running without asking)[/cyan]")
        final_argv = proposal.argv
        decision = "auto_approved"
    elif matched_template is not None:
        console.print(
            f"[cyan](matched template {matched_template.id} -- running without asking)[/cyan]"
        )
        final_argv = proposal.argv
        decision = "template_matched"
    else:
        choice = Prompt.ask(
            "Run this? \\[y]es / \\[e]dit / \\[n]o / \\[m]anual",
            choices=["y", "e", "n", "m"],
            default="n",
        )

        if choice == "n":
            audit.record_decision(command_id, "rejected", None)
            console.print("[yellow]rejected, nothing ran[/yellow]")
            raise typer.Exit(code=0)

        if choice == "e":
            edited = Prompt.ask("Edit command", default=shlex.join(proposal.argv))
            final_argv = shlex.split(edited)
            edited_choice = Prompt.ask(
                "Use edited command? \\[y]es / \\[n]o / \\[m]anual",
                choices=["y", "n", "m"],
                default="n",
            )
            if edited_choice == "n":
                audit.record_decision(command_id, "rejected", None)
                console.print("[yellow]rejected, nothing ran[/yellow]")
                raise typer.Exit(code=0)
            decision = "manual" if edited_choice == "m" else "edited"
        else:
            final_argv = proposal.argv
            decision = "manual" if choice == "m" else "approved"

    # Always checked, even for remembered or template-matched commands --
    # policy.py can gain new rules after an approval was stored.
    blocked_reason = check_prohibited(final_argv)
    if blocked_reason is not None:
        audit.record_decision(command_id, "blocked", final_argv, reason=blocked_reason)
        console.print(f"[red]blocked by policy:[/red] {blocked_reason}")
        raise typer.Exit(code=1)

    audit.record_decision(command_id, decision, final_argv)

    if decision == "manual":
        _show_manual_instructions(settings, command_id, final_argv)
        raise typer.Exit(code=0)

    if remembered is not None:
        approvals.mark_used(remembered.command_hash)
    elif matched_template is not None:
        approvals.mark_template_used(matched_template.id)

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
        exec_result = sandbox.exec(final_argv, timeout=timeout, network=allow_network)
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

    if remembered is None and matched_template is None:
        # Broad catch is deliberate: this question is purely optional
        # follow-up after the real work already happened and was already
        # reported above -- nothing raised here should change this
        # command's outcome or exit code.
        try:
            if settings.approval_template_matching:
                remember_choice = Prompt.ask(
                    r"Remember this command? \[y]es exact / \[n]o / \[t]emplate",
                    choices=["y", "n", "t"],
                    default="n",
                )
                if remember_choice == "y":
                    approvals.remember(final_argv, task_description=task)
                elif remember_choice == "t":
                    _create_template_interactively(approvals, final_argv)
            elif Confirm.ask(
                "Remember this exact command so it skips confirmation next time?",
                default=False,
            ):
                approvals.remember(final_argv, task_description=task)
        except Exception:  # noqa: BLE001, S110 -- deliberately blind, see comment above
            pass

    raise typer.Exit(code=124 if exec_result.timed_out else exec_result.exit_code)


@app.command()
def propose(
    task: str = typer.Argument(..., help="Plain-language description of what to do."),
    model: str | None = typer.Option(
        None, "--model", help="Override the configured default Ollama model."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Override the configured per-command sandbox timeout (seconds)."
    ),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Temporarily attach network access for this execution only.",
    ),
) -> None:
    """Asks the model to propose a command for TASK, shows you its
    reasoning, and lets you run it as-is, edit it, reject it, or handle it
    manually -- unless an exact command was remembered earlier or an enabled,
    human-authored approval template matches. Nothing else runs without
    confirmation, and the proposal, your decision, and (if anything ran) the
    execution are all recorded under one shared command_id -- see logs/audit.db."""
    _run_proposal(
        task,
        _proposal_services(),
        model=model,
        timeout=timeout,
        allow_network=allow_network,
    )


def _interactive_session(
    *,
    model: str | None = None,
    timeout: float | None = None,
    allow_network: bool = False,
) -> None:
    services = _proposal_services()
    console.print("[bold]brokkr interactive mode[/bold]")
    console.print("Type a task in plain language. Type help for a reminder, or exit/quit to leave.")

    while True:
        try:
            task = console.input("\n[bold cyan]brokkr>[/bold cyan] ")
        except EOFError:
            console.print()
            return

        command = task.strip().lower()
        if command in {"exit", "quit"}:
            return
        if command == "help":
            console.print("Type one task per line; use exit, quit, or Ctrl+D to leave.")
            continue
        if not task.strip():
            continue

        try:
            _run_proposal(
                task,
                services,
                model=model,
                timeout=timeout,
                allow_network=allow_network,
            )
        except typer.Exit:
            # A task's exit status ends `brokkr propose`, but only ends that
            # turn in the interactive session.
            continue
        except EOFError:
            # EOF while a sub-prompt (e.g. "Run this? [y/e/n/m]") is open --
            # Rich's Prompt.ask raises EOFError directly, distinct from the
            # top-level `console.input()` EOFError caught above, which ends
            # the whole session. Mid-task EOF only cancels that one turn.
            console.print()
            continue


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    model: str | None = typer.Option(
        None, "--model", help="Override the default model for every interactive task."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Override the sandbox timeout for every interactive task."
    ),
    allow_network: bool = typer.Option(
        False,
        "--allow-network",
        help="Allow temporary network access for every interactive task.",
    ),
) -> None:
    """Start interactive mode when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        _interactive_session(model=model, timeout=timeout, allow_network=allow_network)


@manual_app.command("show")
def manual_show(
    command_id: str = typer.Argument(..., help="Full command id or a unique short prefix."),
) -> None:
    """Shows a result file for a command you ran yourself."""
    settings = load_settings()
    decision = _resolve_manual_decision(AuditStore(settings), command_id)
    result_path = _manual_result_path(settings, decision.command_id)
    shown_path = _display_path(result_path)

    if not result_path.exists():
        console.print(f"[yellow]no result found at {shown_path} yet[/yellow]")
        raise typer.Exit(code=1)
    if result_path.is_symlink() or not result_path.is_file():
        console.print(f"[red]refusing result path that is not a regular workspace file: {shown_path}[/red]")
        raise typer.Exit(code=1)

    contents = result_path.read_text(encoding="utf-8", errors="replace")
    console.print(f"[bold]Manual result for {decision.command_id[:_MANUAL_ID_LENGTH]}:[/bold]")
    console.print(
        contents,
        markup=False,
        highlight=False,
        end="" if contents.endswith("\n") else "\n",
    )

    if Confirm.ask("Save this result as a memory note?", default=False):
        command = shlex.join(decision.final_argv)
        note = MemoryStore(settings).add(f"Manual result for {command}:\n{contents}")
        console.print(f"[green]added memory {note.id}[/green]")


@approvals_app.command("list")
def approvals_list() -> None:
    """Lists exact remembered commands and human-authored templates."""
    settings = load_settings()
    approvals = ApprovalStore(settings)
    remembered = approvals.list_all()
    templates = approvals.list_templates()
    if not remembered and not templates:
        console.print("[yellow]no remembered commands[/yellow]")
        return

    if remembered:
        console.print("[bold]Exact approvals[/bold]")
        exact_table = Table("id", "command", "task", "used", "created")
        for entry in remembered:
            exact_table.add_row(
                str(entry.id),
                shlex.join(entry.argv),
                entry.task_description or "",
                str(entry.use_count),
                entry.created_at,
            )
        console.print(exact_table)

    if templates:
        console.print("[bold]Approval templates[/bold]")
        template_table = Table()
        template_table.add_column("id", no_wrap=True)
        template_table.add_column("pattern")
        template_table.add_column("constraints")
        template_table.add_column("used", justify="right")
        for template in templates:
            template_table.add_row(
                template.id,
                format_template(template),
                _template_constraint_summary(template),
                str(template.use_count),
            )
        console.print(template_table)


@approvals_app.command("revoke")
def approvals_revoke(
    approval_id: str = typer.Argument(..., help="id shown by `brokkr approvals list`."),
) -> None:
    """Revokes an exact remembered command or an approval template."""
    settings = load_settings()
    approvals = ApprovalStore(settings)
    revoked = (
        approvals.revoke_template(approval_id)
        if approval_id.startswith("tpl_")
        else approvals.revoke(approval_id)
    )
    if revoked:
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
