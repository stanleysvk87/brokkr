"""Static PROHIBITED blocklist -- checked against the final argv before ANY
approved, edited, or remembered command reaches the sandbox, independent of
the approved_commands table (Stage 3). Defense in depth on top of the Docker
mount boundary (see sandbox/docker_sandbox.py's module docstring for why
that mount boundary, not this list, is the PRIMARY defense) -- this exists
to give a fast, auditable "never executed" rejection for obviously
catastrophic patterns, and to avoid needlessly destroying/recreating the
sandbox rootfs for commands that were never going anywhere legitimate.

Deliberately NOT trying to be a comprehensive shell-attack blocklist:
brokkr never uses shell=True (see sandbox/docker_sandbox.py's exec_run
call), so classic shell metacharacter attacks (`;`, `&&`, `|`, `>`) don't
work as shell syntax when passed as top-level argv elements. Explicit
`bash -c` and `sh -c` commands do interpret them, so their command segments
are checked for the same catastrophic shapes, including one nested shell
level. This remains a narrow argv-aware check, not a general shell parser.
The list is expected to grow; it is not, and does not claim to be,
exhaustive.
"""

from __future__ import annotations

import re
import shlex

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
_SHELL_SEPARATORS = {";", "&&", "||", "|", "\n"}
_SHELL_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")


def _check_single_command(executable: str, args: list[str]) -> str | None:
    """Checks one argv-like command against executable-specific rules."""
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

    return None


def _shell_commands(script: str) -> list[list[str]]:
    """Returns argv-like commands separated by the supported shell operators."""
    lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True

    try:
        tokens = list(lexer)
    except ValueError:
        commands = []
        for segment in _SHELL_SEPARATOR_RE.split(script):
            try:
                command = shlex.split(segment)
            except ValueError:
                continue
            if command:
                commands.append(command)
        return commands

    commands: list[list[str]] = []
    command: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if command:
                commands.append(command)
                command = []
        else:
            command.append(token)
    if command:
        commands.append(command)
    return commands


def _shell_script(argv: list[str]) -> str | None:
    args = argv[1:]
    if argv[0] not in _SHELL_EXECUTABLES or "-c" not in args:
        return None
    idx = args.index("-c")
    return args[idx + 1] if idx + 1 < len(args) else ""


def _check_shell_commands(script: str, *, nested_shell_levels: int) -> str | None:
    for command in _shell_commands(script):
        reason = _check_single_command(command[0], command[1:])
        if reason is not None:
            return reason

        nested_script = _shell_script(command)
        if nested_script is not None and nested_shell_levels > 0:
            reason = _check_shell_commands(
                nested_script,
                nested_shell_levels=nested_shell_levels - 1,
            )
            if reason is not None:
                return reason

    return None


def check_prohibited(argv: list[str]) -> str | None:
    """Returns a human-readable reason if `argv` matches a known
    catastrophic pattern, else None. A match means the final command is
    recorded as blocked and never reaches sandbox execution."""
    if not argv:
        return None

    reason = _check_single_command(argv[0], argv[1:])
    if reason is not None:
        return reason

    script = _shell_script(argv)
    if script is not None:
        if _FORK_BOMB_RE.search(script):
            return "refusing a recognizable fork-bomb pattern"
        return _check_shell_commands(script, nested_shell_levels=1)

    return None
