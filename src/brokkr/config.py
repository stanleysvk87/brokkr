"""Configuration loading.

Precedence (lowest to highest): built-in defaults -> .env / process
environment. No config.toml layer yet -- brokkr's settings are simple
(one sandbox, one workdir) and everything fits comfortably in BROKKR_*
environment variables; a toml layer can be added later if that stops
being true.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class SandboxConfig(BaseModel):
    model_config = {"frozen": True}

    image: str = "brokkr-sandbox:latest"
    workdir_host: Path = Path.home() / "brokkr-workspace"
    workdir_container: str = "/workspace"
    network: str = "none"
    cpu_limit: float = 4.0
    memory_limit: str = "2g"
    pids_limit: int = 256
    command_timeout_seconds: float = 30.0
    idle_reset_minutes: float = 60.0
    container_name: str = "brokkr-sandbox"


class Settings(BaseModel):
    model_config = {"frozen": True}

    ollama_url: str
    default_model: str
    data_dir: Path
    log_dir: Path
    log_level: str
    sandbox: SandboxConfig
    approval_template_matching: bool
    memory_max_notes: int = 20

    @property
    def audit_db_path(self) -> Path:
        return self.log_dir / "audit.db"

    @property
    def audit_jsonl_path(self) -> Path:
        return self.log_dir / "audit.jsonl"

    @property
    def audit_blobs_dir(self) -> Path:
        return self.log_dir / "blobs"

    @property
    def approvals_db_path(self) -> Path:
        return self.data_dir / "approvals.db"

    @property
    def memory_db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def sandbox_last_used_path(self) -> Path:
        return self.data_dir / "sandbox_last_used"


def _resolve_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from a .env file (if present) plus process environment.

    Explicit process environment variables always win over .env file
    values, and both win over the built-in defaults above.
    """
    load_dotenv(dotenv_path=env_file or (PROJECT_ROOT / ".env"), override=False)

    sandbox_workdir_host = Path(
        os.environ.get("BROKKR_SANDBOX_WORKDIR_HOST", str(Path.home() / "brokkr-workspace"))
    ).expanduser()
    sandbox_workdir_host.mkdir(parents=True, exist_ok=True)

    sandbox_config = SandboxConfig(
        image=os.environ.get("BROKKR_SANDBOX_IMAGE", "brokkr-sandbox:latest"),
        workdir_host=sandbox_workdir_host,
        network=os.environ.get("BROKKR_SANDBOX_NETWORK", "none"),
        cpu_limit=float(os.environ.get("BROKKR_SANDBOX_CPU_LIMIT", "4.0")),
        memory_limit=os.environ.get("BROKKR_SANDBOX_MEMORY_LIMIT", "2g"),
        pids_limit=int(os.environ.get("BROKKR_SANDBOX_PIDS_LIMIT", "256")),
        command_timeout_seconds=float(
            os.environ.get("BROKKR_SANDBOX_COMMAND_TIMEOUT_SECONDS", "30")
        ),
        idle_reset_minutes=float(os.environ.get("BROKKR_SANDBOX_IDLE_RESET_MINUTES", "60")),
    )

    return Settings(
        ollama_url=os.environ.get("BROKKR_OLLAMA_URL", "http://127.0.0.1:11434"),
        default_model=os.environ.get("BROKKR_DEFAULT_MODEL", "qwen2.5-coder:7b"),
        data_dir=_resolve_dir(os.environ.get("BROKKR_DATA_DIR", "data")),
        log_dir=_resolve_dir(os.environ.get("BROKKR_LOG_DIR", "logs")),
        log_level=os.environ.get("BROKKR_LOG_LEVEL", "INFO").upper(),
        sandbox=sandbox_config,
        approval_template_matching=_bool_env("BROKKR_APPROVAL_TEMPLATE_MATCHING", False),
        memory_max_notes=int(os.environ.get("BROKKR_MEMORY_MAX_NOTES", "20")),
    )
