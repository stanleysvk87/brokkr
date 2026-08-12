# Architecture

## The pipeline

Every command that runs through `brokkr propose` goes through the same
four stages, in order, and every stage is independently skippable in a
way that's visible in the audit trail:

```
task description
      |
      v
  [1] LLM proposal          brokkr/llm/client.py
      |  (structured output: {reasoning, argv})
      v
  [2] human decision         brokkr/cli.py (propose command)
      |  approve / edit / reject
      |  -- OR skipped entirely if [3] finds an exact match
      v
  [3] remembered-approval    brokkr/approvals/store.py
      |  lookup (checked before asking; exact argv match only)
      v
  [4] policy blocklist       brokkr/permissions/policy.py
      |  always checked, regardless of how the command was approved
      v
  [5] sandbox execution      brokkr/sandbox/docker_sandbox.py
```

Steps [1]–[4] never touch the real filesystem or network — the actual
consequential action is entirely inside step [5], and everything before
it exists to decide whether that step should happen at all.

## Why four separate gates instead of one

Each gate catches a different failure mode, and they're deliberately not
merged into one "is this safe" check:

- **The LLM proposal** can be wrong, but it can't do anything by itself —
  it only ever produces a JSON object.
- **Human review** catches the cases a human would obviously reject on
  sight, and is the only gate with actual judgment.
- **The remembered-approval store** exists purely to reduce repeated
  confirmation fatigue for things a human has already reviewed once —
  see [docs/security-model.md](security-model.md) for why it's
  exact-match only.
- **The policy blocklist** is a static, code-level backstop that runs no
  matter which path a command took to get there — including a remembered
  command from a previous version of the blocklist that didn't yet know
  about a given pattern.

None of these four are the actual safety boundary. That's step [5] — see
[docs/security-model.md](security-model.md).

## The audit trail

`brokkr/audit/store.py` (`logs/audit.db`) has three tables, all keyed by
one `command_id` minted once per `brokkr propose` invocation, before the
model is even called:

- `proposals` — what the model was asked, and what it proposed (or
  failed to produce).
- `decisions` — what actually happened to the proposal: approved,
  edited, rejected, auto-approved from memory, or blocked by policy.
- `commands` — what the sandbox actually ran, if anything did.

A `command_id` with a `decisions` row but no matching `commands` row is
expected, not a corrupted record — it means the pipeline stopped before
step [5], and exactly where it stopped is recorded in `decisions.decision`.

Every one of those three tables also gets a full JSON blob written to
`logs/blobs/<command_id>/<event>.json` (the complete model response, the
complete stdout/stderr, everything — not just the indexed columns), plus
a compact summary line in `logs/audit.jsonl` for a live `tail -f` feed.
See `brokkr/audit/store.py`'s module docstring for the reasoning behind
this three-way split instead of a single log format.

`brokkr sandbox exec` (the direct, no-LLM entry point) only ever writes
a `commands` row — there's no proposal or decision to record, since a
human typed the exact command themselves.

## The sandbox container's lifecycle

One container, `brokkr-sandbox`, is created lazily on first use and
reused across calls to `docker exec` into it — not a fresh `docker run
--rm` per command — so a sequence like "install a package, then use it"
works across separate `brokkr propose`/`sandbox exec` invocations.

Container reuse does not mean the commands share one persistent shell.
Each invocation starts a new process tree with the container's configured
environment, so a variable exported by one `bash -c` command is absent
from the next. Files written to `/workspace` do persist, as do changes to
the container rootfs, because both invocations use the same container.
Detached background processes can persist too: a process that does not
keep the initiating exec's streams open is reparented to container PID 1
when its shell exits and continues consuming the sandbox's PID, memory,
and CPU allowances until it exits, is killed explicitly, or the sandbox
is reset. Later execs still work, but they do not implicitly clean up or
inherit that process's environment.

Its rootfs is never modified in place across a reset: `brokkr sandbox
reset` stops and removes the container entirely, and the next command
recreates it from the image, fresh. Nothing about the container's
internal state silently persists in a way that isn't either (a) still
there because nobody reset it, or (b) gone because someone did.

## Approval matching: why exact-match only

`ApprovalStore.find()` hashes the canonical JSON-encoded argv array and
looks up an exact match — nothing fuzzier. This was a deliberate
decision made before any code was written, not a limitation to be lifted
later without thought: semantic or embedding similarity was considered
and rejected, because "similar" is a bad axis for a decision that skips
human review. `rm file.txt` and `rm -rf /` can be close in embedding
space; their consequences are not close at all.

Generalized template matching (e.g. "same command, any file under
`/workspace`") is real, useful, and explicitly out of scope for the
current approval store — see `BROKKR_APPROVAL_TEMPLATE_MATCHING` in
`.env.example`. If it's ever built, the constraint types must be
human-authored per rule (a path glob, an enum of allowed values, a
regex someone actually wrote) — never a pattern the model itself infers
or proposes, since that would reintroduce exactly the "the model decides
what's safe to skip" problem exact-match was designed to avoid.
