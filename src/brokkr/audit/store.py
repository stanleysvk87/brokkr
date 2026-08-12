"""Hybrid audit trail: SQLite index + per-command blob files + a thin
JSONL tail.

Payloads here are bigger than a typical audit line -- full stdout/stderr,
and from Stage 2 on, full model prompts/completions -- so a pure
append-only JSONL log would be unpleasant to query. Instead:

  - logs/audit.db     structured, indexed columns for querying
                       ("show me every non-zero exit today").
  - logs/blobs/<command_id>/<event>.json   the full raw payload for that
                       event, one file per event, independently
                       readable/deletable/greppable.
  - logs/audit.jsonl   a compact one-line-per-event summary, purely so
                       `tail -f logs/audit.jsonl` gives a live feed
                       without opening the database.

Every event a command produces gets the same command_id, so all three
places (and later, the Stage 2/3 proposal + approval-decision events)
join back to one place.

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
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from brokkr.config import Settings
from brokkr.sandbox.docker_sandbox import SandboxExecutionResult

_SCHEMA = """
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
    image_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_commands_created_at ON commands (created_at);
CREATE INDEX IF NOT EXISTS idx_commands_exit_code ON commands (exit_code);
"""


class AuditStore:
    def __init__(self, settings: Settings) -> None:
        self._db_path = settings.audit_db_path
        self._blobs_dir = settings.audit_blobs_dir
        self._jsonl_path = settings.audit_jsonl_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

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

    def record_execution(
        self, result: SandboxExecutionResult, source: str = "manual"
    ) -> str:
        """Records one sandbox execution across all three stores. Returns
        the generated command_id."""
        command_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        blob = {"command_id": command_id, "created_at": now, "source": source, **asdict(result)}
        self._write_blob(command_id, "execution", blob)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO commands (
                    command_id, created_at, source, argv_json,
                    exit_code, timed_out, truncated, duration_ms,
                    container_id, image_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

        self._append_jsonl(
            {
                "timestamp": now,
                "command_id": command_id,
                "source": source,
                "command": result.command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": round(result.duration_ms, 1),
            }
        )
        return command_id
