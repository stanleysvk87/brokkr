# Changelog

## Stage 0: repo scaffold — 2026-08-12

Initial commit. Empty `src/brokkr` package, `pyproject.toml` (setuptools,
`src/` layout, `brokkr` CLI entry point), `.gitignore`/`.env.example` set up
from day one to keep personal paths/IPs/secrets out of the repo (this
project is intended for eventual public release), Apache-2.0 `LICENSE`,
README stub, one trivial smoke test.

No sandbox, no LLM integration, no approval logic yet — see
`~/.claude/plans/partitioned-fluttering-sonnet.md` for the full staged
build plan this project follows. Stage 1 (Docker sandbox execution
primitive, no LLM yet) is next.

## Stage 1: Docker sandbox execution primitive — 2026-08-12

Still no LLM — `brokkr sandbox exec -- <argv>` lets a human run an exact
command directly inside the sandbox, so the sandbox's own safety
guarantees could be proven before any model is ever wired in to propose
commands on its own.

Added: `permissions/tiers.py` (five-tier permission model, enforcement
inverted from a typical read-only agent — `REQUIRES_APPROVAL` routes
through confirmation instead of being hard-rejected), `permissions/
policy.py` (static PROHIBITED blocklist — `rm -rf /`, `dd` to a raw block
device, `mkfs`, recursive `chmod` on `/`, recognizable fork bombs — kept
deliberately separate from and not applied inside the sandbox mechanism
itself, since Stage 1's own verification needs to run these *through* the
sandbox to prove the Docker-level boundary holds on its own merits),
`config.py` (`BROKKR_*` settings, `.env` over built-in defaults),
`sandbox/Dockerfile` + `sandbox/docker_sandbox.py` (one long-lived
container per install, `--network none` default, CPU/memory/PIDs limits,
`--cap-drop ALL`, `no-new-privileges`, no Docker socket passthrough,
single bind-mounted workspace, per-command timeout enforced *inside* the
container via GNU `timeout` rather than host-side process management),
`audit/store.py` (hybrid SQLite index + per-command JSON blob files + a
thin JSONL tail — every sandbox execution is fully recorded, and unlike
a typical best-effort audit logger, a failure to record is never
silently swallowed here), and CLI wiring (`brokkr sandbox exec/reset/
status`).

**Real bug found during manual verification**: the sandbox container
initially ran as a fixed non-root container user (uid 10001) that had no
write access to the host-mounted workspace directory (owned by whichever
account runs brokkr) — every write inside `/workspace` failed with
"Permission denied", which would have silently broken the entire point
of giving a proposed command a real place to work in. Fixed by running
the container with `user=<host uid>:<host gid>` instead of the image's
built-in user, so the mount is actually writable and files created
through it come out owned by a real host account rather than an orphaned
container-only uid.

Manually verified per the plan's Stage 1 checklist: a successful command,
a non-zero exit code, a timed-out infinite loop (confirmed no leftover
process afterward), `rm -rf /` (confirmed everything outside the mounted
workspace is untouched — the workspace itself is real, writable storage,
so its own contents being deleted is the mount boundary working exactly
as designed, not a failure of it), and a network call with `--network
none` (confirmed connection failure). All five checks produced matching,
complete records across `logs/audit.db`, `logs/blobs/`, and
`logs/audit.jsonl`.

Stage 2 (an LLM proposes a command via structured output, a human
confirms or edits it, it runs through this same sandbox mechanism) is
next.

## Stage 2: LLM proposes, human confirms — 2026-08-12

`brokkr propose "<task description>"`. Still no approval memory --
every proposal is reviewed fresh (that's Stage 3).

Added: `llm/client.py` (`OllamaClient.propose()` -- uses Ollama's
structured-output feature, a JSON Schema passed as the request's
`format` field, so the model's response is constrained to valid JSON
matching a `{reasoning, argv}` shape at decode time, then independently
re-validated with Pydantic since constrained decoding guarantees
syntactically valid JSON, not sane values -- an empty `argv` is valid
JSON and useless as a command). Confirmed working end-to-end locally
against Ollama 0.32 with `qwen2.5-coder:7b`.

`audit/store.py` gained two more linked tables, `proposals` and
`decisions`, alongside Stage 1's `commands` -- all three share one
command_id, minted once via `AuditStore.new_command_id()` before the
model is even called. A command_id with a decision row but no commands
row is expected, not a bug: it means the human rejected the proposal,
or the policy blocklist caught it, before anything reached the sandbox.

`permissions/policy.py`'s blocklist is now actually wired in -- at the
`brokkr propose` layer, checked against whatever the human's final
decision produced (approved as-is or edited), right before the sandbox
call. `brokkr sandbox exec` (Stage 1) still has no blocklist gate of its
own by design; that's the raw mechanism Stage 1's own verification
depends on being able to reach the sandbox unfiltered.

Manually verified: a proposal that's approved as-is and actually runs;
one that's rejected outright (nothing runs, decision row still
recorded); and one that's edited by the human into `rm -rf /` --
confirmed the blocklist rejects it even after human approval of the
edited form, and confirmed no `commands` row was created for it.
Inspected `logs/audit.db` afterward: exactly the expected linked rows
across all three tables for each case.

Stage 3 (an `approved_commands` table + exact-match lookup, so a
previously-approved command doesn't need re-confirming) is next.

## Stage 3: remembered exact-match approvals — 2026-08-12

After running a proposed command, `brokkr propose` now offers to
remember its exact argv; a later proposal producing that identical argv
runs immediately, no confirmation prompt. Added `approvals/store.py`
(`ApprovalStore`, its own `data/approvals.db`, separate from the audit
trail on purpose -- one is curated state, the other an append-only
record) and `brokkr approvals list/revoke`.

Matching is exact-only: the stored key is a sha256 of the canonical
JSON-encoded argv array, nothing fuzzier. Template/generalized matching
(e.g. "same command, any file") was deliberately left out of this store
entirely rather than built and left half-off behind a flag -- semantic
similarity was already rejected during planning as unsafe for a
decision that skips human review, and a real generalized-matching
design (human-authored constraints only, never model-inferred) is real
scope for a later stage, not a checkbox to bolt on here.

The policy blocklist still runs even for a remembered command --
verified by design, not by a new test, since it's the same code path
Stage 2 already exercises regardless of how `final_argv` was decided.

Manually verified end-to-end: remembered a command, then re-proposed
the identical task with `< /dev/null` (no stdin available at all) --
it ran without any prompt, which is only possible if confirmation was
genuinely skipped rather than silently defaulted. Confirmed `use_count`
incremented on the remembered entry, and that `approvals revoke`
removes it and reverts to asking again.

Stage 4 (public-release documentation: full README, security-model
doc, SECURITY.md, CONTRIBUTING.md) is next -- Stages 0-3 cover
everything the plan called the core, riskiest mechanism; what's left
is making the project legible to someone who didn't build it.

## Stage 4: public-release documentation — 2026-08-12

No code changes. Full README (prerequisites, install, quickstart,
config, what-this-is-not), `docs/architecture.md` (the propose →
decide → approve → policy → execute pipeline, the audit trail's three
linked tables, the sandbox container's lifecycle, and why approval
matching is exact-match only), `docs/security-model.md` (the four
properties of the sandbox that make it the actual boundary, what was
manually verified during Stage 1 and why that's the evidence rather
than the design alone, and an explicit list of what this project does
and does not defend against), `SECURITY.md` (private vulnerability
reporting, explicit in/out-of-scope list), and `CONTRIBUTING.md` (dev
setup, required checks, the conventions the codebase actually
enforces, and a note on designs that were deliberately rejected rather
than simply not-yet-built, so a contributor doesn't reintroduce them).

This closes out the plan in
`~/.claude/plans/partitioned-fluttering-sonnet.md` -- Stages 0 through
4 are all done. What's left before a public GitHub repo is a human
read-through of the whole thing, not more building.

## Dogfooding pass: 3 real bugs found and fixed — 2026-08-12

Manually ran a round of realistic `brokkr propose` tasks (create files,
install a package, configure git, an intentionally vague "clean up the
workspace" prompt) rather than only the scripted Stage 1-3 verification
scenarios. Found and fixed:

- **Approved commands could silently never run.** `brokkr propose`
  asked "remember this?" *after* recording the approval decision but
  *before* actually executing the command. An interrupted or EOF'd
  answer to that optional follow-up question (Ctrl-D, Ctrl-C, or stdin
  simply closing) aborted the whole process with a bare "Aborted.",
  discarding a command that was already approved and had a `decisions`
  row saying so, but no matching `commands` row -- reproduced and
  confirmed via `logs/audit.db` before fixing. Execution now happens
  immediately after approval; the remember question comes after, and
  any failure answering it is caught and ignored rather than able to
  affect the command's already-determined outcome or exit code.
- **`$HOME` was unwritable inside the sandbox**, a side effect of the
  Stage 1 host-uid fix: the container's `/etc/passwd` has no entry for
  an arbitrary host uid, so `$HOME` defaulted to `/`. Broke `pip`'s
  cache (a warning) and `git config --global` (a hard failure). Fixed
  by explicitly setting `HOME=/workspace` when the container starts.
- **`pip install` failed outright** on Debian's system Python (PEP 668,
  "externally-managed-environment") -- a protection meant for a real,
  persistent machine that doesn't meaningfully apply to a filesystem
  that's disposable and recreated from the image on every `sandbox
  reset`. Fixed with `PIP_BREAK_SYSTEM_PACKAGES=1` in the Dockerfile.

Also added `python-is-python3` to the sandbox image after a proposed
`python --version` failed with "command not found" -- only `python3`
existed, which is correct Debian convention but not what most people
(or models) type by default.

All three fixes verified manually afterward: the interrupted-prompt
case not aborting, `git config --global` succeeding, and `pip install`
succeeding with `BROKKR_SANDBOX_NETWORK=bridge` (still correctly
failing on DNS resolution with the default `--network none`, which is
the isolation working as intended, not a bug).
