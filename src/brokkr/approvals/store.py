"""Human-curated remembered-command and approval-template store.

A command is auto-approved either when its argv is byte-for-byte identical
(as a canonical JSON array) to one a human explicitly chose to remember, or
when optional template matching is enabled and every human-authored template
constraint matches. Templates are never inferred or suggested by the model.
Semantic/embedding similarity was deliberately rejected as an approval-matching
strategy: `rm file.txt` and `rm -rf /` can be "similar" by any embedding
distance, but their consequences are not remotely comparable, which makes
"similar" a bad axis for a decision that skips human review.

Separate database file from the audit trail (data/approvals.db vs.
logs/audit.db): this one is small, mutable, user-curated state ("what am
I willing to auto-run"); the audit trail is an append-only record of what
actually happened. Mixing them would make it awkward to, say, ship
someone your approvals list without also handing over your command
history.

Named workflows and library entries share this database because they are also
mutable, human-curated command state, but neither is an approval match. Library
description lookup uses transparent keyword overlap only to offer an entry
before a model call; it always requires a fresh human choice and never auto-runs.
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

CREATE TABLE IF NOT EXISTS workflows (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    workflow_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    argv_json TEXT NOT NULL,
    template_id TEXT,
    use_previous_stdout INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workflow_name, position),
    FOREIGN KEY (workflow_name) REFERENCES workflows(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS library_entries (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    template_id TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS library_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_WORKSPACE_ROOT = PurePosixPath("/workspace")
_CONSTRAINT_TYPES = {"path_under_workdir", "enum", "regex"}
_LIBRARY_SEED_VERSION = "1"
_LIBRARY_MATCH_THRESHOLD = 0.5
_LIBRARY_STOP_WORDS = {
    "and",
    "for",
    "from",
    "into",
    "last",
    "the",
    "this",
    "with",
    "workspace",
}
_SEEDED_LIBRARY_ENTRIES = (
    (
        "workspace-disk-usage",
        "Show how much disk space the workspace files use in total",
        ["du", "-sh", "/workspace"],
    ),
    (
        "find-large-files",
        "Find large files over 100 MB in the workspace",
        ["find", "/workspace", "-type", "f", "-size", "+100M", "-print"],
    ),
    (
        "find-recent-files",
        "Find files changed or modified during the last seven days",
        ["find", "/workspace", "-type", "f", "-mtime", "-7", "-print"],
    ),
    (
        "archive-workspace",
        "Create a compressed tar gz archive backup of the whole workspace",
        [
            "bash",
            "-c",
            (
                "tar -czf /tmp/brokkr-workspace.tar.gz --exclude=workspace.tar.gz "
                "-C /workspace . && "
                "mv /tmp/brokkr-workspace.tar.gz /workspace/workspace.tar.gz"
            ),
        ],
    ),
    (
        "extract-tar-archive",
        "Extract a tar gz archive into the workspace",
        [
            "bash",
            "-c",
            (
                "mkdir -p /workspace/extracted-tar && "
                "tar -xzf /workspace/archive.tar.gz -C /workspace/extracted-tar"
            ),
        ],
    ),
    (
        "extract-zip-archive",
        "Extract a zip archive into the workspace",
        [
            "bash",
            "-c",
            (
                "mkdir -p /workspace/extracted-zip && "
                "unzip -o /workspace/archive.zip -d /workspace/extracted-zip"
            ),
        ],
    ),
    (
        "count-todo-lines",
        "Count lines containing TODO in the workspace input text file",
        ["grep", "-c", "--", "TODO", "/workspace/input.txt"],
    ),
    (
        "extract-pdf-text",
        "Extract readable text from a PDF document in the workspace",
        ["pdftotext", "/workspace/document.pdf", "/workspace/document.txt"],
    ),
    (
        "ocr-scanned-image",
        "Run OCR on a scanned image and save the recognized text",
        ["tesseract", "/workspace/scan.png", "/workspace/ocr"],
    ),
    (
        "git-worktree-status",
        "Show git status and changed files for the workspace repository",
        ["git", "-C", "/workspace/repo", "status", "--short", "--branch"],
    ),
)


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


@dataclass(frozen=True)
class WorkflowStep:
    position: int
    argv: list[str]
    template_id: str | None = None
    use_previous_stdout: bool = False


@dataclass(frozen=True)
class Workflow:
    name: str
    steps: list[WorkflowStep]
    created_at: str
    last_used_at: str | None
    use_count: int


@dataclass(frozen=True)
class LibraryEntry:
    name: str
    description: str
    argv: list[str]
    template_id: str | None
    created_at: str
    last_used_at: str | None
    use_count: int


@dataclass(frozen=True)
class LibraryMatch:
    entry: LibraryEntry
    score: float


class TemplateValidationError(ValueError):
    """Raised when a human-authored template cannot be saved safely."""


class WorkflowValidationError(ValueError):
    """Raised when a human-authored workflow is invalid or cannot resolve."""


class LibraryValidationError(ValueError):
    """Raised when a human-curated library entry is invalid or cannot resolve."""


def validate_workflow_name(name: str) -> str:
    normalized = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", normalized):
        raise WorkflowValidationError(
            "workflow name must use only letters, numbers, '.', '_', or '-'"
        )
    if len(normalized) > 64:
        raise WorkflowValidationError("workflow name must be at most 64 characters")
    return normalized


def validate_library_name(name: str) -> str:
    try:
        return validate_workflow_name(name)
    except WorkflowValidationError as exc:
        raise LibraryValidationError(str(exc).replace("workflow name", "library name")) from exc


def _library_keywords(value: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[^\W_]+", value.lower())
        if len(word) >= 3 and word not in _LIBRARY_STOP_WORDS
    }


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
            self._seed_library(conn)

    @staticmethod
    def _seed_library(conn: sqlite3.Connection) -> None:
        seeded = conn.execute(
            "SELECT value FROM library_metadata WHERE key = 'seed_version'"
        ).fetchone()
        if seeded is not None:
            return

        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """
            INSERT OR IGNORE INTO library_entries (
                name, description, argv_json, template_id, created_at, use_count
            ) VALUES (?, ?, ?, NULL, ?, 0)
            """,
            [
                (name, description, json.dumps(argv, ensure_ascii=False), now)
                for name, description, argv in _SEEDED_LIBRARY_ENTRIES
            ],
        )
        conn.execute(
            "INSERT OR IGNORE INTO library_metadata (key, value) VALUES ('seed_version', ?)",
            (_LIBRARY_SEED_VERSION,),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA foreign_keys=ON")
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

    def get_template(self, template_id: str) -> ApprovalTemplate | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_templates WHERE id = ?", (template_id,)
            ).fetchone()
        return self._row_to_template(row) if row else None

    def create_library_entry(
        self,
        name: str,
        description: str,
        argv: list[str],
        template_id: str | None = None,
    ) -> LibraryEntry:
        normalized_name = validate_library_name(name)
        normalized_description = description.strip()
        if not normalized_description:
            raise LibraryValidationError("library description must not be empty")
        if not argv or any(not isinstance(value, str) for value in argv):
            raise LibraryValidationError("library command must be a non-empty argv")

        if template_id is not None:
            template = self._library_template(template_id, normalized_name)
            if not _template_matches(template, argv):
                raise LibraryValidationError(
                    f"template {template_id} does not match library command {normalized_name}"
                )

        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO library_entries (
                        name, description, argv_json, template_id, created_at, use_count
                    ) VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (
                        normalized_name,
                        normalized_description,
                        json.dumps(argv, ensure_ascii=False),
                        template_id,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LibraryValidationError(
                f"library entry {normalized_name!r} already exists"
            ) from exc
        entry = self.get_library_entry(normalized_name)
        assert entry is not None
        return entry

    def get_library_entry(self, name: str) -> LibraryEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM library_entries WHERE name = ?", (name,)
            ).fetchone()
        return self._row_to_library_entry(row) if row else None

    def list_library_entries(self) -> list[LibraryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM library_entries ORDER BY created_at DESC, name"
            ).fetchall()
        return [self._row_to_library_entry(row) for row in rows]

    def delete_library_entry(self, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM library_entries WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def mark_library_entry_used(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE library_entries
                SET last_used_at = ?, use_count = use_count + 1
                WHERE name = ?
                """,
                (now, name),
            )

    def search_library(self, task: str) -> list[LibraryMatch]:
        task_words = _library_keywords(task)
        if len(task_words) < 2:
            return []

        matches: list[LibraryMatch] = []
        for entry in self.list_library_entries():
            description_words = _library_keywords(entry.description)
            shared = task_words & description_words
            if len(shared) < 2:
                continue
            score = len(shared) / min(len(task_words), len(description_words))
            if score >= _LIBRARY_MATCH_THRESHOLD:
                matches.append(LibraryMatch(entry, score))
        return sorted(matches, key=lambda match: (-match.score, match.entry.name))

    def validate_library_reference(self, entry: LibraryEntry) -> None:
        if entry.template_id is not None:
            template = self._library_template(entry.template_id, entry.name)
            if not _template_matches(template, entry.argv):
                raise LibraryValidationError(
                    f"template {entry.template_id} no longer matches library entry {entry.name}"
                )

    def resolve_library_entry(
        self, entry: LibraryEntry, variable_value: str | None = None
    ) -> list[str]:
        if entry.template_id is None:
            return list(entry.argv)

        template = self._library_template(entry.template_id, entry.name)
        if variable_value is None:
            raise LibraryValidationError(
                f"library entry {entry.name} requires a value for its template variable"
            )
        variable_parts = [part.variable for part in template.parts if part.variable is not None]
        constraint = variable_parts[0]
        assert constraint is not None
        if not constraint_matches(constraint, variable_value):
            raise LibraryValidationError(
                f"value {variable_value!r} does not satisfy {constraint.constraint_type}"
            )
        return [
            part.literal if part.literal is not None else variable_value
            for part in template.parts
        ]

    def format_library_command(self, entry: LibraryEntry) -> str:
        if entry.template_id is None:
            return shlex.join(entry.argv)
        return format_template(self._library_template(entry.template_id, entry.name))

    def _library_template(self, template_id: str, entry_name: str) -> ApprovalTemplate:
        try:
            template = self.get_template(template_id)
        except (KeyError, TypeError, TemplateValidationError, json.JSONDecodeError) as exc:
            raise LibraryValidationError(
                f"library entry {entry_name} references invalid template {template_id}"
            ) from exc
        if template is None:
            raise LibraryValidationError(
                f"library entry {entry_name} references missing template {template_id}"
            )
        if sum(part.variable is not None for part in template.parts) != 1:
            raise LibraryValidationError(
                f"library entry {entry_name} template {template_id} must have exactly one variable"
            )
        return template

    def prepare_workflow_steps(
        self,
        commands: list[list[str]],
        previous_stdout_templates: dict[int, str] | None = None,
    ) -> list[WorkflowStep]:
        """Validate captured commands and optional 1-based template overrides."""
        if not commands:
            raise WorkflowValidationError("a workflow requires at least one step")
        if any(not command for command in commands):
            raise WorkflowValidationError("workflow commands must not be empty")

        overrides = previous_stdout_templates or {}
        if any(position < 2 or position > len(commands) for position in overrides):
            raise WorkflowValidationError(
                "previous-stdout template positions must be between 2 and the step count"
            )

        steps: list[WorkflowStep] = []
        for position, argv in enumerate(commands, start=1):
            template_id = overrides.get(position)
            if template_id is None:
                steps.append(WorkflowStep(position=position, argv=list(argv)))
                continue

            template = self.get_template(template_id)
            if template is None:
                raise WorkflowValidationError(f"no approval template with id {template_id}")
            variable_count = sum(part.variable is not None for part in template.parts)
            if variable_count != 1:
                raise WorkflowValidationError(
                    f"template {template_id} must have exactly one variable position"
                )
            if not _template_matches(template, argv):
                raise WorkflowValidationError(
                    f"template {template_id} does not match captured step {position}"
                )
            steps.append(
                WorkflowStep(
                    position=position,
                    argv=list(argv),
                    template_id=template_id,
                    use_previous_stdout=True,
                )
            )
        return steps

    def create_workflow(self, name: str, steps: list[WorkflowStep]) -> Workflow:
        normalized_name = validate_workflow_name(name)
        if not steps or [step.position for step in steps] != list(range(1, len(steps) + 1)):
            raise WorkflowValidationError("workflow steps must be ordered from 1 without gaps")
        for step in steps:
            if not step.argv:
                raise WorkflowValidationError("workflow commands must not be empty")
            if step.use_previous_stdout != (step.template_id is not None):
                raise WorkflowValidationError(
                    f"workflow step {step.position} has inconsistent template data flow"
                )
            if step.template_id is None:
                continue
            if step.position == 1:
                raise WorkflowValidationError(
                    "workflow step 1 cannot use previous-step stdout"
                )
            template = self.get_template(step.template_id)
            if template is None:
                raise WorkflowValidationError(
                    f"no approval template with id {step.template_id}"
                )
            if sum(part.variable is not None for part in template.parts) != 1:
                raise WorkflowValidationError(
                    f"template {step.template_id} must have exactly one variable position"
                )
            if not _template_matches(template, step.argv):
                raise WorkflowValidationError(
                    f"template {step.template_id} does not match captured step {step.position}"
                )

        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO workflows (name, created_at, use_count) VALUES (?, ?, 0)",
                    (normalized_name, now),
                )
                conn.executemany(
                    """
                    INSERT INTO workflow_steps (
                        workflow_name, position, argv_json, template_id,
                        use_previous_stdout
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            normalized_name,
                            step.position,
                            json.dumps(step.argv, ensure_ascii=False),
                            step.template_id,
                            int(step.use_previous_stdout),
                        )
                        for step in steps
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise WorkflowValidationError(
                f"workflow {normalized_name!r} already exists"
            ) from exc
        workflow = self.get_workflow(normalized_name)
        assert workflow is not None
        return workflow

    def get_workflow(self, name: str) -> Workflow | None:
        with self._connect() as conn:
            workflow_row = conn.execute(
                "SELECT * FROM workflows WHERE name = ?", (name,)
            ).fetchone()
            if workflow_row is None:
                return None
            step_rows = conn.execute(
                "SELECT * FROM workflow_steps WHERE workflow_name = ? ORDER BY position",
                (name,),
            ).fetchall()
        return Workflow(
            name=workflow_row["name"],
            steps=[
                WorkflowStep(
                    position=row["position"],
                    argv=json.loads(row["argv_json"]),
                    template_id=row["template_id"],
                    use_previous_stdout=bool(row["use_previous_stdout"]),
                )
                for row in step_rows
            ],
            created_at=workflow_row["created_at"],
            last_used_at=workflow_row["last_used_at"],
            use_count=workflow_row["use_count"],
        )

    def list_workflows(self) -> list[Workflow]:
        with self._connect() as conn:
            names = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM workflows ORDER BY created_at DESC, name"
                ).fetchall()
            ]
        return [workflow for name in names if (workflow := self.get_workflow(name))]

    def delete_workflow(self, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM workflows WHERE name = ?", (name,))
        return cursor.rowcount > 0

    def mark_workflow_used(self, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE workflows
                SET last_used_at = ?, use_count = use_count + 1
                WHERE name = ?
                """,
                (now, name),
            )

    def validate_workflow_references(self, workflow: Workflow) -> None:
        """Check template references that can be validated before execution."""
        for step in workflow.steps:
            if step.template_id is None:
                continue
            try:
                template = self.get_template(step.template_id)
            except (KeyError, TypeError, TemplateValidationError, json.JSONDecodeError) as exc:
                raise WorkflowValidationError(
                    f"workflow step {step.position} references invalid template "
                    f"{step.template_id}"
                ) from exc
            if template is None:
                raise WorkflowValidationError(
                    f"workflow step {step.position} references missing template "
                    f"{step.template_id}"
                )
            variable_count = sum(part.variable is not None for part in template.parts)
            if variable_count != 1:
                raise WorkflowValidationError(
                    f"workflow step {step.position} template {step.template_id} must have "
                    "exactly one variable"
                )

    def resolve_workflow_step(
        self, step: WorkflowStep, previous_stdout: str | None
    ) -> list[str]:
        if not step.use_previous_stdout:
            return list(step.argv)
        if step.template_id is None:
            raise WorkflowValidationError(
                f"workflow step {step.position} has no template reference"
            )
        if previous_stdout is None:
            raise WorkflowValidationError(
                f"workflow step {step.position} requires previous-step stdout"
            )

        template = self.get_template(step.template_id)
        if template is None:
            raise WorkflowValidationError(
                f"workflow step {step.position} references missing template {step.template_id}"
            )
        variable_parts = [part.variable for part in template.parts if part.variable is not None]
        if len(variable_parts) != 1:
            raise WorkflowValidationError(
                f"workflow step {step.position} template must have exactly one variable"
            )

        value = previous_stdout.strip()
        constraint = variable_parts[0]
        assert constraint is not None
        if not constraint_matches(constraint, value):
            raise WorkflowValidationError(
                f"workflow step {step.position} rejected previous stdout {value!r}: "
                f"it does not satisfy {constraint.constraint_type}"
            )
        return [part.literal if part.literal is not None else value for part in template.parts]

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
    def _row_to_library_entry(row: sqlite3.Row) -> LibraryEntry:
        try:
            argv = json.loads(row["argv_json"])
        except json.JSONDecodeError as exc:
            raise LibraryValidationError(
                f"library entry {row['name']} has invalid command data"
            ) from exc
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(value, str) for value in argv)
        ):
            raise LibraryValidationError(
                f"library entry {row['name']} must contain a non-empty string argv"
            )
        return LibraryEntry(
            name=row["name"],
            description=row["description"],
            argv=argv,
            template_id=row["template_id"],
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
