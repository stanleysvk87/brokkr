"""Hybrid audit trail: SQLite index + per-command blob files + a thin
JSONL tail.

Payloads here are bigger than a typical audit line -- full stdout/stderr,
and from Stage 2 on, full model prompts/completions -- so a pure
append-only JSONL log would be unpleasant to query. Instead:

  - logs/audit.db     structured, indexed columns for querying
                       ("show me every non-zero exit today" or every
                       execution that had network access).
  - logs/blobs/<command_id>/<event>.json   the full raw payload for that
                       event, one file per event, independently
                       readable/deletable/greppable.
  - logs/audit.jsonl   a compact one-line-per-event summary, purely so
                       `tail -f logs/audit.jsonl` gives a live feed
                       without opening the database.

Every event a command produces shares one command_id, minted once by
new_command_id() before anything happens, so all three places join back
to one place. A full round trip through Stage 2 writes up to three
linked rows -- proposals (what the model suggested), decisions (what the
human actually did about it), commands (what the sandbox actually ran,
if anything did) -- and a command_id with a decision but no execution row
is not a bug: it means the human rejected or manually handled the proposal,
or the static policy blocklist caught it, before anything reached the sandbox.

Failures here are deliberately NOT swallowed, unlike a typical
best-effort audit logger. "Everything gets recorded so everything can be
debugged" is this project's actual stated purpose (see README) -- a
silently dropped record would quietly break that promise instead of
loudly surfacing a real problem (disk full, permissions) that the human
running brokkr needs to know about immediately.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from brokkr.config import Settings
from brokkr.llm.client import ProposalResult
from brokkr.sandbox.docker_sandbox import SandboxExecutionResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    command_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    task_description TEXT NOT NULL,
    model TEXT NOT NULL,
    reasoning TEXT,
    proposed_argv_json TEXT,
    latency_ms REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    command_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    final_argv_json TEXT,
    reason TEXT,
    workflow_run_id TEXT,
    workflow_name TEXT,
    workflow_step INTEGER
);

CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    timed_out INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    container_id TEXT NOT NULL,
    image_id TEXT NOT NULL,
    network_enabled INTEGER NOT NULL DEFAULT 0,
    workflow_run_id TEXT,
    workflow_name TEXT,
    workflow_step INTEGER
);

CREATE INDEX IF NOT EXISTS idx_commands_created_at ON commands (created_at);
CREATE INDEX IF NOT EXISTS idx_commands_exit_code ON commands (exit_code);
CREATE INDEX IF NOT EXISTS idx_proposals_created_at ON proposals (created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions (created_at);
"""


@dataclass(frozen=True)
class ManualDecision:
    command_id: str
    final_argv: list[str]


@dataclass(frozen=True)
class ReviewedCommand:
    command_id: str
    task_description: str
    argv: list[str]


@dataclass(frozen=True)
class HistoryEntry:
    command_id: str
    created_at: str
    task_description: str
    decision: str | None
    reason: str | None
    exit_code: int | None
    timed_out: bool
    proposal_error: str | None
    workflow_run_id: str | None = None
    workflow_name: str | None = None
    workflow_step: int | None = None

    @property
    def displayed_decision(self) -> str:
        if self.decision is not None:
            return self.decision
        return "proposal_failed" if self.proposal_error else "pending"

    @property
    def outcome(self) -> str:
        if self.exit_code is not None:
            return "timed out" if self.timed_out else f"exit {self.exit_code}"
        if self.decision == "manual":
            return "manual"
        if self.decision == "blocked":
            return self.reason or "blocked"
        if self.decision == "rejected":
            return "rejected"
        if self.proposal_error:
            return "proposal failed"
        return "not executed"


class AuditStore:
    def __init__(self, settings: Settings) -> None:
        self._db_path = settings.audit_db_path
        self._blobs_dir = settings.audit_blobs_dir
        self._jsonl_path = settings.audit_jsonl_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._ensure_columns(
                conn,
                "commands",
                {
                    "network_enabled": "INTEGER NOT NULL DEFAULT 0",
                    "workflow_run_id": "TEXT",
                    "workflow_name": "TEXT",
                    "workflow_step": "INTEGER",
                },
            )
            self._ensure_columns(
                conn,
                "decisions",
                {
                    "workflow_run_id": "TEXT",
                    "workflow_name": "TEXT",
                    "workflow_step": "INTEGER",
                },
            )

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError:
                current = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if name not in current:
                    raise

    @staticmethod
    def new_command_id() -> str:
        return uuid.uuid4().hex

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _write_blob(self, command_id: str, event: str, payload: dict) -> Path:
        command_dir = self._blobs_dir / command_id
        command_dir.mkdir(parents=True, exist_ok=True)
        blob_path = command_dir / f"{event}.json"
        blob_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return blob_path

    def _append_jsonl(self, record: dict) -> None:
        with self._jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    def record_proposal(self, command_id: str, result: ProposalResult) -> None:
        """Records what the model was asked and what it proposed (or
        failed to produce)."""
        now = datetime.now(timezone.utc).isoformat()
        proposal = result.proposal

        blob = {
            "command_id": command_id,
            "created_at": now,
            "task_description": result.task_description,
            "model": result.model,
            "raw_content": result.raw_content,
            "latency_ms": result.latency_ms,
            "error": result.error,
            "reasoning": proposal.reasoning if proposal else None,
            "argv": proposal.argv if proposal else None,
            "needs_network": proposal.needs_network if proposal else None,
        }
        self._write_blob(command_id, "proposal", blob)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO proposals (
                    command_id, created_at, task_description, model,
                    reasoning, proposed_argv_json, latency_ms, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    now,
                    result.task_description,
                    result.model,
                    proposal.reasoning if proposal else None,
                    json.dumps(proposal.argv, ensure_ascii=False) if proposal else None,
                    result.latency_ms,
                    result.error,
                ),
            )

        self._append_jsonl(
            {
                "timestamp": now,
                "command_id": command_id,
                "event": "proposal",
                "task_description": result.task_description,
                "argv": proposal.argv if proposal else None,
                "needs_network": proposal.needs_network if proposal else None,
                "error": result.error,
            }
        )

    def record_decision(
        self,
        command_id: str,
        decision: str,
        final_argv: list[str] | None,
        reason: str | None = None,
        *,
        workflow_run_id: str | None = None,
        workflow_name: str | None = None,
        workflow_step: int | None = None,
    ) -> None:
        """Records the human or approval-gate outcome, including distinct
        exact and template auto-approval decisions."""
        now = datetime.now(timezone.utc).isoformat()

        blob = {
            "command_id": command_id,
            "created_at": now,
            "decision": decision,
            "final_argv": final_argv,
            "reason": reason,
            "workflow_run_id": workflow_run_id,
            "workflow_name": workflow_name,
            "workflow_step": workflow_step,
        }
        self._write_blob(command_id, "decision", blob)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions (
                    command_id, created_at, decision, final_argv_json, reason,
                    workflow_run_id, workflow_name, workflow_step
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    now,
                    decision,
                    json.dumps(final_argv, ensure_ascii=False) if final_argv else None,
                    reason,
                    workflow_run_id,
                    workflow_name,
                    workflow_step,
                ),
            )

        self._append_jsonl(
            {
                "timestamp": now,
                "command_id": command_id,
                "event": "decision",
                "decision": decision,
                "final_argv": final_argv,
                "reason": reason,
                "workflow_run_id": workflow_run_id,
                "workflow_name": workflow_name,
                "workflow_step": workflow_step,
            }
        )

    def find_manual_decisions(self, command_id_prefix: str) -> list[ManualDecision]:
        """Returns manual decisions whose command id starts with PREFIX."""
        normalized = command_id_prefix.lower()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT command_id, final_argv_json
                FROM decisions
                WHERE decision = 'manual'
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [
            ManualDecision(command_id=row[0], final_argv=json.loads(row[1]))
            for row in rows
            if row[1] is not None and row[0].lower().startswith(normalized)
        ]

    def last_reviewed_commands(self, limit: int) -> list[ReviewedCommand]:
        """Return the last human-approved proposal executions in original order."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.command_id, p.task_description, c.argv_json
                FROM proposals AS p
                JOIN decisions AS d ON d.command_id = p.command_id
                JOIN commands AS c ON c.command_id = p.command_id
                WHERE d.decision IN ('approved', 'edited')
                ORDER BY c.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ReviewedCommand(row[0], row[1], json.loads(row[2])) for row in reversed(rows)
        ]

    def list_history(
        self,
        limit: int = 20,
        decision: str | None = None,
        workflow: str | None = None,
    ) -> list[HistoryEntry]:
        """Return recent proposal turns and workflow steps without modifying audit data."""
        if limit < 1:
            raise ValueError("limit must be at least 1")

        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH history_rows AS (
                    SELECT
                        p.command_id, p.created_at, p.task_description,
                        d.decision, d.reason, c.exit_code, c.timed_out, p.error,
                        NULL AS workflow_run_id, NULL AS workflow_name,
                        NULL AS workflow_step
                    FROM proposals AS p
                    LEFT JOIN decisions AS d ON d.command_id = p.command_id
                    LEFT JOIN commands AS c ON c.command_id = p.command_id
                    UNION ALL
                    SELECT
                        d.command_id, d.created_at,
                        'workflow ' || d.workflow_name || ' step ' || d.workflow_step,
                        d.decision, d.reason, c.exit_code, c.timed_out, NULL,
                        d.workflow_run_id, d.workflow_name, d.workflow_step
                    FROM decisions AS d
                    LEFT JOIN commands AS c ON c.command_id = d.command_id
                    WHERE d.workflow_run_id IS NOT NULL
                )
                SELECT * FROM history_rows
                WHERE (? IS NULL OR decision = ?)
                  AND (? IS NULL OR workflow_name = ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (decision, decision, workflow, workflow, limit),
            ).fetchall()
        return [
            HistoryEntry(
                command_id=row[0],
                created_at=row[1],
                task_description=row[2],
                decision=row[3],
                reason=row[4],
                exit_code=row[5],
                timed_out=bool(row[6]),
                proposal_error=row[7],
                workflow_run_id=row[8],
                workflow_name=row[9],
                workflow_step=row[10],
            )
            for row in rows
        ]

    def record_execution(
        self,
        command_id: str,
        result: SandboxExecutionResult,
        source: str = "manual",
        *,
        workflow_run_id: str | None = None,
        workflow_name: str | None = None,
        workflow_step: int | None = None,
    ) -> None:
        """Records one sandbox execution across all three stores."""
        now = datetime.now(timezone.utc).isoformat()

        blob = {
            "command_id": command_id,
            "created_at": now,
            "source": source,
            "workflow_run_id": workflow_run_id,
            "workflow_name": workflow_name,
            "workflow_step": workflow_step,
            **asdict(result),
        }
        self._write_blob(command_id, "execution", blob)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO commands (
                    command_id, created_at, source, argv_json,
                    exit_code, timed_out, truncated, duration_ms,
                    container_id, image_id, network_enabled,
                    workflow_run_id, workflow_name, workflow_step
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command_id,
                    now,
                    source,
                    json.dumps(result.command, ensure_ascii=False),
                    result.exit_code,
                    int(result.timed_out),
                    int(result.truncated),
                    result.duration_ms,
                    result.container_id,
                    result.image_id,
                    int(result.network_enabled),
                    workflow_run_id,
                    workflow_name,
                    workflow_step,
                ),
            )

        self._append_jsonl(
            {
                "timestamp": now,
                "command_id": command_id,
                "event": "execution",
                "source": source,
                "command": result.command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "network_enabled": result.network_enabled,
                "duration_ms": round(result.duration_ms, 1),
                "workflow_run_id": workflow_run_id,
                "workflow_name": workflow_name,
                "workflow_step": workflow_step,
            }
        )
