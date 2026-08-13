from __future__ import annotations

import json
import sqlite3

from brokkr.audit.store import AuditStore
from brokkr.config import SandboxConfig, Settings
from brokkr.sandbox.docker_sandbox import SandboxExecutionResult


def _settings(tmp_path) -> Settings:
    return Settings(
        ollama_url="http://127.0.0.1:11434",
        default_model="test-model",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sandbox=SandboxConfig(),
        approval_template_matching=False,
    )


def _result(network_enabled: bool) -> SandboxExecutionResult:
    return SandboxExecutionResult(
        command=["true"],
        exit_code=0,
        timed_out=False,
        truncated=False,
        stdout="",
        stderr="",
        duration_ms=1.0,
        container_id="container",
        image_id="image",
        network_enabled=network_enabled,
    )


def test_execution_audit_records_network_enabled_for_both_cases(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditStore(settings)

    audit.record_execution("without-network", _result(False))
    audit.record_execution("with-network", _result(True))

    with sqlite3.connect(settings.audit_db_path) as conn:
        rows = conn.execute(
            "SELECT command_id, network_enabled FROM commands ORDER BY command_id"
        ).fetchall()
    assert rows == [("with-network", 1), ("without-network", 0)]

    blob = json.loads(
        (settings.audit_blobs_dir / "with-network" / "execution.json").read_text()
    )
    assert blob["network_enabled"] is True
    records = [json.loads(line) for line in settings.audit_jsonl_path.read_text().splitlines()]
    assert [record["network_enabled"] for record in records] == [False, True]


def test_existing_audit_database_is_migrated_with_safe_default(tmp_path):
    settings = _settings(tmp_path)
    settings.log_dir.mkdir(parents=True)
    with sqlite3.connect(settings.audit_db_path) as conn:
        conn.execute(
            """
            CREATE TABLE commands (
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
            )
            """
        )
        conn.execute(
            """
            INSERT INTO commands VALUES (
                'old-command', 'now', 'manual', '["true"]', 0, 0, 0, 1.0,
                'container', 'image'
            )
            """
        )

    AuditStore(settings)

    with sqlite3.connect(settings.audit_db_path) as conn:
        assert conn.execute(
            "SELECT network_enabled FROM commands WHERE command_id = 'old-command'"
        ).fetchone() == (0,)
