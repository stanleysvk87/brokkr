"""Human-curated workspace context for future proposals.

Notes enter this store only through an explicit human action. Nothing is
inferred from command output or audit history: automatic extraction would
let a model's guesses quietly influence later proposals, while this project
keeps changes to model behavior visible and operator-controlled. Bounded
recency is deliberately enough for this small store; semantic search would
add an uncertain matching layer and a dependency without a demonstrated
need.

The database is separate from both approvals and the audit trail because
all three have different ownership and retention semantics: notes are
mutable context, approvals are safety decisions, and audit records describe
what actually happened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from brokkr.config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@dataclass
class Note:
    id: int
    note: str
    created_at: str


class MemoryValidationError(ValueError):
    pass


class MemoryStore:
    def __init__(self, settings: Settings) -> None:
        self._db_path: Path = settings.memory_db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, note: str) -> Note:
        if not note.strip():
            raise MemoryValidationError("memory note must not be empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO notes (note, created_at) VALUES (?, ?)",
                (note, now),
            )
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._row_to_model(row)

    def list_all(self) -> list[Note]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def forget(self, note_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        return cursor.rowcount > 0

    def recent(self, limit: int) -> list[Note]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM notes
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row: sqlite3.Row) -> Note:
        return Note(id=row["id"], note=row["note"], created_at=row["created_at"])
