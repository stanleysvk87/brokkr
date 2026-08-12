"""Exact-match "remembered command" store.

A command is only ever auto-approved if its argv is byte-for-byte
identical (as a canonical JSON array) to one a human explicitly chose to
remember in an earlier session -- never "similar", never inferred.
Semantic/embedding similarity was deliberately rejected as a matching
strategy: `rm file.txt` and `rm -rf /` can be "similar" by any embedding
distance, but their consequences are not remotely comparable, which makes
"similar" a bad axis for a decision that skips human review. Generalized
template matching (e.g. "same command, any file under /workspace") is a
real, useful feature for later, but only ever as an explicit, human-typed
opt-in per rule -- see BROKKR_APPROVAL_TEMPLATE_MATCHING in .env.example
-- never something this store infers on its own.

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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
"""


@dataclass
class ApprovedCommand:
    id: int
    command_hash: str
    argv: list[str]
    task_description: str | None
    created_at: str
    last_used_at: str | None
    use_count: int


def command_hash(argv: list[str]) -> str:
    canonical = json.dumps(argv, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    def list_all(self) -> list[ApprovedCommand]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approved_commands ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def revoke(self, approval_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM approved_commands WHERE id = ?", (approval_id,))
        return cursor.rowcount > 0

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
