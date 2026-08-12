"""Static PROHIBITED blocklist -- checked before ANY proposed command
reaches the approval flow or the sandbox, independent of the
approved_commands table (Stage 3). Defense in depth on top of the Docker
mount boundary (see sandbox/docker_sandbox.py's module docstring for why
that mount boundary, not this list, is the PRIMARY defense) -- this exists
to give a fast, auditable "never even considered" rejection for obviously
catastrophic patterns, and to avoid needlessly destroying/recreating the
sandbox rootfs for commands that were never going anywhere legitimate.

Deliberately NOT trying to be a comprehensive shell-attack blocklist:
brokkr never uses shell=True (see sandbox/docker_sandbox.py's exec_run
call), so classic shell metacharacter attacks (`;`, `&&`, `|`, `>`) don't
work as shell syntax at all when passed as argv elements -- they just
become literal strings handed to the target program. What DOES still need
blocking here is programs that take a "write target" as a plain argument
(dd, mkfs) and a small number of well-known catastrophic command shapes.
This list is expected to grow; it is not, and does not claim to be,
exhaustive.
"""

from __future__ import annotations

import re

_DANGEROUS_DD_TARGET_RE = re.compile(r"^of=/dev/(sd|nvme|vd|hd|mmcblk)")
_MKFS_RE = re.compile(r"^mkfs(\.\w+)?$")
# Matches the classic `:(){ :|:& };:` shape and close variants -- a
# function named anything that immediately forks-and-backgrounds itself.
_FORK_BOMB_RE = re.compile(r"\(\)\s*\{[^}]*\|\s*:?\s*&")

_RM_EXECUTABLES = {"rm", "/bin/rm", "/usr/bin/rm"}
_DD_EXECUTABLES = {"dd", "/bin/dd", "/usr/bin/dd"}
_CHMOD_EXECUTABLES = {"chmod", "/bin/chmod", "/usr/bin/chmod"}
_SHELL_EXECUTABLES = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/bash", "/usr/bin/sh"}
_CATASTROPHIC_RM_TARGETS = {"/", "/*", "~", "$HOME", "/workspace", "/workspace/*"}


def check_prohibited(argv: list[str]) -> str | None:
    """Returns a human-readable reason if `argv` matches a known
    catastrophic pattern, else None. Called before anything else -- a
    match here means the command is never even shown to the human for
    approval."""
    if not argv:
        return None

    executable = argv[0]
    args = argv[1:]

    if executable in _RM_EXECUTABLES:
        has_recursive_force = any(a in ("-rf", "-fr", "-r", "-R", "-f") for a in args) or any(
            a.startswith("-") and "r" in a.lower() and "f" in a.lower() for a in args
        )
        if has_recursive_force and any(a in _CATASTROPHIC_RM_TARGETS for a in args):
            return "refusing rm with a recursive/force flag against / or the workspace root"

    if executable in _DD_EXECUTABLES and any(_DANGEROUS_DD_TARGET_RE.match(a) for a in args):
        return "refusing dd with a raw block device as its output target (of=/dev/...)"

    if _MKFS_RE.match(executable):
        return "refusing mkfs -- formats a filesystem, always destructive"

    if (
        executable in _CHMOD_EXECUTABLES
        and any(a in ("-R", "--recursive") for a in args)
        and any(a in ("/", "/*", "/workspace", "/workspace/*") for a in args)
    ):
        return "refusing recursive chmod against / or the workspace root"

    if executable in _SHELL_EXECUTABLES and "-c" in args:
        idx = args.index("-c")
        script = args[idx + 1] if idx + 1 < len(args) else ""
        if _FORK_BOMB_RE.search(script):
            return "refusing a recognizable fork-bomb pattern"

    return None
