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
schema and useless as a command). It also catches a real failure mode
found by dogfooding, not just a hypothetical one: asked to "count files
in the workspace", qwen2.5-coder:7b proposed `["find", "/workspace",
"-maxdepth", "1", "-type", "f", "|", "wc", "-l"]` -- a bare `|` as its
own argv element, despite the system prompt explicitly saying not to do
that. Since brokkr never uses a shell to run argv (see
sandbox/docker_sandbox.py), that `|` doesn't pipe anything -- it's just
handed to `find` as a literal argument, which fails with a confusing
"paths must precede expression" error instead of doing what was asked.
Prompt instructions alone don't reliably stop a small local model from
doing this occasionally; catching it here, deterministically, is the
same lesson this project has already applied elsewhere (the static
policy blocklist exists for the same reason: code-level checks catch
what a prompt only asks nicely for).
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
        "needs_network": {"type": "boolean"},
    },
    "required": ["reasoning", "argv"],
}

_SYSTEM_PROMPT = (
    "You propose exactly one shell command, as a list of argv strings, to "
    "accomplish the user's task. The command will run inside an isolated "
    "Docker sandbox with no network access by default and only a /workspace directory "
    "visible. A human reviews every proposal before it runs, and may edit "
    "or reject it. Propose the most direct, minimal command for the task. "
    "Never propose a shell pipeline joined by ;, &&, or | as a single argv "
    'string -- if the task genuinely needs a shell, propose ["bash", "-c", '
    '"<script>"] instead, with the whole script as one argv element. '
    "Argv elements are never expanded by a shell. A glob such as '*.txt' or "
    "'/workspace/reports/*' passed directly to du, cat, cp, rm, or another ordinary "
    "command is a literal string, not a file match. Use find's own pattern matching "
    "for multiple files instead, or deliberately use bash -c when a shell glob is "
    "genuinely required. Wrong: [\"du\", \"-sh\", \"/workspace/reports/*\"]. "
    'Right: ["find", "/workspace/reports", "-maxdepth", "1", "-type", "f", '
    '"-exec", "du", "-sh", "{}", ";"]. '
    "For network reachability checks, use curl instead of ping: this sandbox "
    "deliberately has no raw-socket capability, so ping cannot work. dmesg "
    "and other kernel-log commands will fail the same way (no CAP_SYSLOG); "
    "do not propose them. "
    "Use pdftotext to extract an existing text layer from a PDF. For images or "
    "scanned PDF pages, use pdftoppm to render them and tesseract for OCR. "
    "A description such as 'the shopping list file' is not an exact filename. "
    "The same is true of a plausible-sounding directory name such as 'the reports "
    "folder'. Never infer spaces, underscores, or an extension from a description, "
    "and never infer a path from one. Hard rule: if a task asks to delete or modify "
    "an item referenced "
    "this way and its exact path is not stated in the current task or human-provided "
    "context, propose only a read-only ls or find without -exec or -delete to discover "
    "it. Never include rm, mv, or any other mutation with a guessed path in the same "
    "proposal, even when your reasoning acknowledges that the path is ambiguous. "
    "Do not turn a description into an invented candidate path such as "
    "'/workspace/reports_folder'. Search a known parent read-only instead; for example, "
    'use ["find", "/workspace", "-type", "d", "-iname", "*reports*", "-print"] '
    "to discover a directory described as 'the reports folder'. "
    "For a deletion preview or dry run, answer the specific question with a read-only "
    "command targeting the confirmed path, such as find <path> to list exactly what "
    "would be removed or du -sh <path> to show its size; do not substitute a generic "
    "listing of an unrelated directory. When proposing find ... -exec, '{}' and its "
    "terminator (';' or '+') must be separate argv elements because no shell will "
    'split them. Correct example: ["find", "/workspace", "-type", "f", "-exec", '
    '"stat", "-c", "%s %n", "{}", ";"]. '
    "Set needs_network to true only when the proposed command requires network "
    "access. This is informational for the human and does not grant access. "
    "Respond only with the JSON object described by the schema."
)


# Standalone argv tokens that only mean anything to a shell. A quoted
# string legitimately *containing* one of these (e.g. an argument whose
# value happens to be "a|b") is fine and untouched -- this only rejects
# an element that IS one of these, verbatim. The one narrow exception is
# find's `-exec` terminator: direct argv must contain `;` as its own token.
_BARE_SHELL_OPERATOR_TOKENS = frozenset({"|", "||", "&&", ";", ">", ">>", "<", "<<", "&"})


class _ProposalSchema(BaseModel):
    reasoning: str
    argv: list[str]
    needs_network: bool = False

    @field_validator("argv")
    @classmethod
    def _argv_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("argv must not be empty")
        return value

    @field_validator("argv")
    @classmethod
    def _no_bare_shell_operators(cls, value: list[str]) -> list[str]:
        is_find = value[0].rsplit("/", 1)[-1] == "find"
        find_exec_terminators: set[int] = set()
        if is_find:
            search_from = 0
            while "-exec" in value[search_from:]:
                exec_index = value.index("-exec", search_from)
                try:
                    terminator_index = value.index(";", exec_index + 1)
                except ValueError:
                    break
                find_exec_terminators.add(terminator_index)
                search_from = terminator_index + 1
        bad_tokens = [
            token
            for index, token in enumerate(value)
            if token in _BARE_SHELL_OPERATOR_TOKENS
            and index not in find_exec_terminators
        ]
        if bad_tokens:
            raise ValueError(
                f"argv contains a bare shell operator token {bad_tokens!r} -- "
                "shell operators only work inside a shell, and brokkr never runs "
                'one implicitly; the proposal should have used ["bash", "-c", '
                '"<script>"] instead of putting the operator directly in argv'
            )
        return value


@dataclass
class CommandProposal:
    reasoning: str
    argv: list[str]
    needs_network: bool = False


@dataclass
class ProposalResult:
    task_description: str
    model: str
    raw_content: str
    latency_ms: float
    proposal: CommandProposal | None = None
    error: str | None = None
    user_error: str | None = None


def _validation_user_error(exc: ValidationError) -> str:
    messages = [str(error.get("msg", "")) for error in exc.errors()]
    if any("argv must not be empty" in message for message in messages):
        return "The model did not propose a command. Try rephrasing the task."
    if any("bare shell operator" in message for message in messages):
        return (
            "The model proposed a shell operator outside a shell, so brokkr "
            "rejected it. Try rephrasing the task."
        )
    return "The model returned a command in an unsupported format. Try rephrasing the task."


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.ollama_url.rstrip("/")
        self._default_model = settings.default_model
        self._timeout = httpx.Timeout(connect=5.0, read=180.0, write=10.0, pool=5.0)

    def propose(
        self,
        task_description: str,
        model: str | None = None,
        notes: list[str] | None = None,
    ) -> ProposalResult:
        """Asks the model to propose a single command for task_description.
        Never raises -- network/HTTP/parsing failures all come back as a
        ProposalResult with `error` set and `proposal` left None, so
        callers (the CLI, and eventually Stage 3's approval flow) have one
        place to check instead of a try/except around every call site."""
        effective_model = model or self._default_model
        messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
        if notes:
            context = (
                "Known context about this workspace, provided by the human operator "
                "across earlier sessions (may be incomplete or outdated -- treat as "
                "helpful background, not as ground truth that overrides what you can "
                "see in the task itself):\n"
                + "\n".join(f"- {note}" for note in notes)
            )
            messages.append({"role": "system", "content": context})
        messages.append({"role": "user", "content": task_description})

        payload = {
            "model": effective_model,
            "messages": messages,
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
                user_error=(
                    "The model request failed. Check that Ollama is running, the URL is "
                    "correct, and the configured model is available."
                ),
            )

        latency_ms = (time.monotonic() - started) * 1000
        raw_content = response.json().get("message", {}).get("content", "")

        try:
            parsed = json.loads(raw_content)
            validated = _ProposalSchema.model_validate(parsed)
        except json.JSONDecodeError as exc:
            return ProposalResult(
                task_description=task_description,
                model=effective_model,
                raw_content=raw_content,
                latency_ms=latency_ms,
                error=f"model returned an invalid proposal: {exc}",
                user_error=(
                    "The model returned a response brokkr could not parse. "
                    "Try rephrasing the task."
                ),
            )
        except ValidationError as exc:
            return ProposalResult(
                task_description=task_description,
                model=effective_model,
                raw_content=raw_content,
                latency_ms=latency_ms,
                error=f"model returned an invalid proposal: {exc}",
                user_error=_validation_user_error(exc),
            )

        return ProposalResult(
            task_description=task_description,
            model=effective_model,
            raw_content=raw_content,
            latency_ms=latency_ms,
            proposal=CommandProposal(
                reasoning=validated.reasoning,
                argv=validated.argv,
                needs_network=validated.needs_network,
            ),
        )
