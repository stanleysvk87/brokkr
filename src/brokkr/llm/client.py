"""Ollama client for command proposals.

Uses Ollama's structured-output feature (a JSON Schema passed as the
request's `format` field, which constrains decoding so the model can
only emit tokens that produce valid JSON matching the schema) rather than
asking for free text and hoping the model wraps its answer in parseable
JSON. This isn't a style preference: an earlier local-agent experiment on
this same class of hardware (see the project's README/CHANGELOG) tried
free-text command proposals and the model's output was too unreliable to
parse into anything executable. Structured output was confirmed working
against a local Ollama 0.32+ server with qwen2.5-coder:7b before this
module was written.

The Pydantic schema below is a second, independent check on top of
Ollama's own schema-constrained decoding -- constrained decoding
guarantees syntactically valid JSON shaped like the schema, not that the
values inside it are sane (an empty argv list is valid JSON matching the
schema and useless as a command).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from brokkr.config import Settings

_PROPOSAL_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "argv": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reasoning", "argv"],
}

_SYSTEM_PROMPT = (
    "You propose exactly one shell command, as a list of argv strings, to "
    "accomplish the user's task. The command will run inside an isolated "
    "Docker sandbox with no network access and only a /workspace directory "
    "visible. A human reviews every proposal before it runs, and may edit "
    "or reject it. Propose the most direct, minimal command for the task. "
    "Never propose a shell pipeline joined by ;, &&, or | as a single argv "
    'string -- if the task genuinely needs a shell, propose ["bash", "-c", '
    '"<script>"] instead, with the whole script as one argv element. '
    "Respond only with the JSON object described by the schema."
)


class _ProposalSchema(BaseModel):
    reasoning: str
    argv: list[str]

    @field_validator("argv")
    @classmethod
    def _argv_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("argv must not be empty")
        return value


@dataclass
class CommandProposal:
    reasoning: str
    argv: list[str]


@dataclass
class ProposalResult:
    task_description: str
    model: str
    raw_content: str
    latency_ms: float
    proposal: CommandProposal | None = None
    error: str | None = None


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ollama_url.rstrip("/")
        self._default_model = settings.default_model
        self._timeout = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)

    def propose(self, task_description: str, model: str | None = None) -> ProposalResult:
        """Asks the model to propose a single command for task_description.
        Never raises -- network/HTTP/parsing failures all come back as a
        ProposalResult with `error` set and `proposal` left None, so
        callers (the CLI, and eventually Stage 3's approval flow) have one
        place to check instead of a try/except around every call site."""
        effective_model = model or self._default_model
        payload = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": task_description},
            ],
            "stream": False,
            "format": _PROPOSAL_JSON_SCHEMA,
        }

        started = time.monotonic()
        try:
            response = httpx.post(
                f"{self._base_url}/api/chat", json=payload, timeout=self._timeout
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return ProposalResult(
                task_description=task_description,
                model=effective_model,
                raw_content="",
                latency_ms=(time.monotonic() - started) * 1000,
                error=f"Ollama request failed: {exc}",
            )

        latency_ms = (time.monotonic() - started) * 1000
        raw_content = response.json().get("message", {}).get("content", "")

        try:
            parsed = json.loads(raw_content)
            validated = _ProposalSchema.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError) as exc:
            return ProposalResult(
                task_description=task_description,
                model=effective_model,
                raw_content=raw_content,
                latency_ms=latency_ms,
                error=f"model returned an invalid proposal: {exc}",
            )

        return ProposalResult(
            task_description=task_description,
            model=effective_model,
            raw_content=raw_content,
            latency_ms=latency_ms,
            proposal=CommandProposal(reasoning=validated.reasoning, argv=validated.argv),
        )
