"""Human-curated remembered-command and approval-template store.

A command is auto-approved either when its argv is byte-for-byte identical
(as a canonical JSON array) to one a human explicitly chose to remember, or
when optional template matching is enabled and every human-authored template
constraint matches. Templates are never inferred or suggested by the model.
Semantic/embedding similarity was deliberately rejected as a matching
strategy: `rm file.txt` and `rm -rf /` can be "similar" by any embedding
distance, but their consequences are not remotely comparable, which makes
"similar" a bad axis for a decision that skips human review.

Separate database file from the audit trail (data/approvals.db vs.
logs/audit.db): this one is small, mutable, user-curated state ("what am
I willing to auto-run"); the audit trail is an append-only record of what
actually happened. Mixing them would make it awkward to, say, ship
someone your approvals list without also handing over your command
history.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from brokkr.config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approved_commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_hash TEXT NOT NULL UNIQUE,
    argv_json TEXT NOT NULL,
    task_description TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS approval_templates (
    id TEXT PRIMARY KEY,
    original_command TEXT NOT NULL,
    template_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);
"""

_WORKSPACE_ROOT = PurePosixPath("/workspace")
_CONSTRAINT_TYPES = {"path_under_workdir", "enum", "regex"}


@dataclass
class ApprovedCommand:
    id: int
    command_hash: str
    argv: list[str]
    task_description: str | None
    created_at: str
    last_used_at: str | None
    use_count: int


@dataclass(frozen=True)
class TemplateConstraint:
    constraint_type: str
    value: str | list[str] | None = None


@dataclass(frozen=True)
class TemplatePart:
    literal: str | None = None
    variable: TemplateConstraint | None = None


@dataclass(frozen=True)
class ApprovalTemplate:
    id: str
    original_command: str
    parts: list[TemplatePart]
    created_at: str
    last_used_at: str | None
    use_count: int


class TemplateValidationError(ValueError):
    """Raised when a human-authored template cannot be saved safely."""


def command_hash(argv: list[str]) -> str:
    canonical = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path_under_workdir(value: str) -> bool:
    if not value:
        return False
    path = PurePosixPath(value)
    if ".." in path.parts:
        return False
    resolved = path if path.is_absolute() else _WORKSPACE_ROOT / path
    return resolved == _WORKSPACE_ROOT or _WORKSPACE_ROOT in resolved.parents


def _validate_constraint(constraint: TemplateConstraint) -> None:
    if (
        not isinstance(constraint.constraint_type, str)
        or constraint.constraint_type not in _CONSTRAINT_TYPES
    ):
        raise TemplateValidationError(f"unknown constraint type: {constraint.constraint_type}")

    if constraint.constraint_type == "path_under_workdir":
        if constraint.value is not None:
            raise TemplateValidationError("path_under_workdir does not accept a value")
        return

    if constraint.constraint_type == "enum":
        if not isinstance(constraint.value, list) or not constraint.value:
            raise TemplateValidationError("enum requires at least one allowed value")
        if any(not isinstance(value, str) or not value for value in constraint.value):
            raise TemplateValidationError("enum values must be non-empty strings")
        return

    if not isinstance(constraint.value, str) or not constraint.value:
        raise TemplateValidationError("regex requires a non-empty pattern")
    try:
        re.compile(constraint.value)
    except re.error as exc:
        raise TemplateValidationError(f"invalid regex: {exc}") from exc


def constraint_matches(constraint: TemplateConstraint, value: str) -> bool:
    """Checks one argv token against a validated human-authored constraint."""
    try:
        _validate_constraint(constraint)
    except TemplateValidationError:
        return False

    if constraint.constraint_type == "path_under_workdir":
        return _path_under_workdir(value)
    if constraint.constraint_type == "enum":
        assert isinstance(constraint.value, list)
        return value in constraint.value

    assert isinstance(constraint.value, str)
    return re.fullmatch(constraint.value, value) is not None


def format_template(template: ApprovalTemplate) -> str:
    tokens: list[str] = []
    for part in template.parts:
        if part.literal is not None:
            tokens.append(shlex.quote(part.literal))
            continue

        constraint = part.variable
        assert constraint is not None
        if constraint.constraint_type == "path_under_workdir":
            tokens.append("<path under /workspace>")
        elif constraint.constraint_type == "enum":
            assert isinstance(constraint.value, list)
            tokens.append(f"<enum: {' | '.join(constraint.value)}>")
        else:
            tokens.append(f"<regex: {constraint.value}>")
    return " ".join(tokens)


def _template_matches(template: ApprovalTemplate, argv: list[str]) -> bool:
    if len(template.parts) != len(argv):
        return False
    for part, value in zip(template.parts, argv, strict=True):
        if part.literal is not None:
            if part.literal != value:
                return False
        elif part.variable is None or not constraint_matches(part.variable, value):
            return False
    return True


class ApprovalStore:
    def __init__(self, settings: Settings) -> None:
        self._db_path: Path = settings.approvals_db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def find(self, argv: list[str]) -> ApprovedCommand | None:
        """Exact-match lookup only -- see module docstring for why."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approved_commands WHERE command_hash = ?",
                (command_hash(argv),),
            ).fetchone()
        return self._row_to_model(row) if row else None

    def remember(self, argv: list[str], task_description: str | None = None) -> ApprovedCommand:
        """Records argv as auto-approvable from now on. Idempotent -- if
        it's already remembered, this is a no-op on the stored data (the
        original created_at and use_count are preserved)."""
        now = datetime.now(timezone.utc).isoformat()
        digest = command_hash(argv)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approved_commands
                    (command_hash, argv_json, task_description, created_at, use_count)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(command_hash) DO NOTHING
                """,
                (digest, json.dumps(argv, ensure_ascii=False), task_description, now),
            )
            row = conn.execute(
                "SELECT * FROM approved_commands WHERE command_hash = ?", (digest,)
            ).fetchone()
        return self._row_to_model(row)

    def mark_used(self, command_hash_value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE approved_commands
                SET last_used_at = ?, use_count = use_count + 1
                WHERE command_hash = ?
                """,
                (now, command_hash_value),
            )

    def create_template(
        self,
        argv: list[str],
        variables: dict[int, TemplateConstraint],
    ) -> ApprovalTemplate:
        if not argv:
            raise TemplateValidationError("a template requires a non-empty command")
        if not variables:
            raise TemplateValidationError("select at least one variable position")

        for position, constraint in variables.items():
            if isinstance(position, bool) or not isinstance(position, int):
                raise TemplateValidationError("variable positions must be integers")
            if position < 0 or position >= len(argv):
                raise TemplateValidationError(f"position {position} is outside this argv")
            _validate_constraint(constraint)
            if not constraint_matches(constraint, argv[position]):
                raise TemplateValidationError(
                    f"current value at position {position} does not satisfy its constraint"
                )

        parts = [
            TemplatePart(variable=variables[position])
            if position in variables
            else TemplatePart(literal=value)
            for position, value in enumerate(argv)
        ]
        template_id = f"tpl_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        template_json = json.dumps(
            [self._part_to_json(part) for part in parts],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approval_templates (
                    id, original_command, template_json, created_at, use_count
                ) VALUES (?, ?, ?, ?, 0)
                """,
                (template_id, shlex.join(argv), template_json, now),
            )
            row = conn.execute(
                "SELECT * FROM approval_templates WHERE id = ?", (template_id,)
            ).fetchone()
        return self._row_to_template(row)

    def find_template(self, argv: list[str]) -> ApprovalTemplate | None:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_templates ORDER BY created_at, id"
            ).fetchall()
        for row in rows:
            try:
                template = self._row_to_template(row)
            except (KeyError, TypeError, TemplateValidationError, json.JSONDecodeError):
                continue
            if _template_matches(template, argv):
                return template
        return None

    def mark_template_used(self, template_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE approval_templates
                SET last_used_at = ?, use_count = use_count + 1
                WHERE id = ?
                """,
                (now, template_id),
            )

    def list_templates(self) -> list[ApprovalTemplate]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_templates ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_template(row) for row in rows]

    def revoke_template(self, template_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM approval_templates WHERE id = ?", (template_id,))
        return cursor.rowcount > 0

    def list_all(self) -> list[ApprovedCommand]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approved_commands ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def revoke(self, approval_id: int | str) -> bool:
        with self._connect() as conn:
            approval_ref = str(approval_id)
            is_hash = len(approval_ref) == 64 and all(
                character in "0123456789abcdef" for character in approval_ref.lower()
            )
            if isinstance(approval_id, int) or (approval_ref.isdigit() and not is_hash):
                cursor = conn.execute(
                    "DELETE FROM approved_commands WHERE id = ?", (int(approval_id),)
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM approved_commands WHERE command_hash = ?", (approval_ref,)
                )
        return cursor.rowcount > 0

    @staticmethod
    def _part_to_json(part: TemplatePart) -> dict:
        if part.literal is not None:
            return {"literal": part.literal}
        constraint = part.variable
        assert constraint is not None
        return {
            "variable": {
                "type": constraint.constraint_type,
                "value": constraint.value,
            }
        }

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> ApprovedCommand:
        return ApprovedCommand(
            id=row["id"],
            command_hash=row["command_hash"],
            argv=json.loads(row["argv_json"]),
            task_description=row["task_description"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=row["use_count"],
        )

    @staticmethod
    def _row_to_template(row: sqlite3.Row) -> ApprovalTemplate:
        raw_parts = json.loads(row["template_json"])
        if not isinstance(raw_parts, list) or not raw_parts:
            raise TemplateValidationError("template argv must be a non-empty list")

        parts: list[TemplatePart] = []
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict):
                raise TemplateValidationError("template positions must be objects")
            if set(raw_part) == {"literal"} and isinstance(raw_part["literal"], str):
                parts.append(TemplatePart(literal=raw_part["literal"]))
                continue
            if set(raw_part) != {"variable"} or not isinstance(raw_part["variable"], dict):
                raise TemplateValidationError("invalid template position")
            raw_constraint = raw_part["variable"]
            if set(raw_constraint) != {"type", "value"}:
                raise TemplateValidationError("invalid template constraint")
            constraint = TemplateConstraint(
                constraint_type=raw_constraint["type"],
                value=raw_constraint["value"],
            )
            _validate_constraint(constraint)
            parts.append(TemplatePart(variable=constraint))

        return ApprovalTemplate(
            id=row["id"],
            original_command=row["original_command"],
            parts=parts,
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            use_count=row["use_count"],
        )
