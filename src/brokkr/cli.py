"""brokkr CLI entry point.

Stage 0: scaffold only. No sandbox, no LLM, no approval logic yet --
those land in Stages 1-3 (see ~/.claude/plans/partitioned-fluttering-sonnet.md
for the staged build plan this project follows).
"""

from __future__ import annotations

import typer

from brokkr import __version__

app = typer.Typer(help="brokkr -- a local, sandboxed, tool-augmented LLM agent.")


@app.command()
def version() -> None:
    """Print the installed brokkr version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
