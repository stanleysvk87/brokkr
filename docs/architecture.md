# Architecture

## The pipeline

Every proposal handled through `brokkr propose` goes through the same four
gates, in order, and every gate is independently visible in the audit trail:

```
task description
      |
      v
  [1] LLM proposal          brokkr/llm/client.py
      |  (structured output: {reasoning, argv})
      v
  [2] human decision         brokkr/cli.py (propose command)
      |  approve / edit / reject / manual
      |  -- OR skipped if [3] finds an exact or enabled template match
      v
  [3] remembered-approval    brokkr/approvals/store.py
      |  lookup (exact argv; optional human-authored template constraints)
      v
  [4] policy blocklist       brokkr/permissions/policy.py
      |  always checked, regardless of how the command was approved
      |
      +-- manual ----------> copyable instructions (no execution)
      |
      v
  [5] approved execution     brokkr/sandbox/docker_sandbox.py
```

Steps [1]–[4] never execute the proposed command. An approved command reaches
the sandbox in step [5]; a manual decision stops after policy and only prints
instructions for the human, who decides whether and where to run it.

## Why four separate gates instead of one

Each gate catches a different failure mode, and they're deliberately not
merged into one "is this safe" check:

- **The LLM proposal** can be wrong, but it can't do anything by itself —
  it only ever produces a JSON object.
- **Human review** catches the cases a human would obviously reject on
  sight, and is the only gate with actual judgment. It can also route a
  command to manual handling without granting brokkr any new capability.
- **The remembered-approval store** exists purely to reduce repeated
  confirmation fatigue for command shapes a human has explicitly reviewed
  and chosen to remember. Exact matching is always available; constrained
  templates are an off-by-default opt-in described below.
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
  edited, rejected, manual, auto-approved from memory, or blocked by policy.
- `commands` — what the sandbox actually ran, if anything did.

A `command_id` with a `decisions` row but no matching `commands` row is
expected, not a corrupted record — it means the pipeline stopped before
step [5]. This includes rejected and blocked proposals as well as a `manual`
decision, where the human was shown what to run but brokkr executed nothing.

Every one of those three tables also gets a full JSON blob written to
`logs/blobs/<command_id>/<event>.json` (the complete model response, the
complete stdout/stderr, everything — not just the indexed columns), plus
a compact summary line in `logs/audit.jsonl` for a live `tail -f` feed.
See `brokkr/audit/store.py`'s module docstring for the reasoning behind
this three-way split instead of a single log format.

`brokkr sandbox exec` (the direct, no-LLM entry point) only ever writes
a `commands` row — there's no proposal or decision to record, since a
human typed the exact command themselves.

## Manual/advisory results

Manual mode is for commands that need privileges or host access the sandbox
deliberately does not have. After the normal proposal, human-review, and policy
gates, brokkr records a `manual` decision and prints a copyable command plus a
predictable result path in the existing workspace:

```
~/brokkr-workspace/manual-<short-command-id>.txt
```

The human runs and redirects the command themselves. `brokkr manual show <id>`
resolves a unique full or short command-id prefix, reads that one result file,
and can save the displayed result as an explicit memory note. There is no
watcher, automatic ingestion, arbitrary-path reader, host execution, sudo
relay, or additional mount: this flow reuses only the already-configured
workspace directory and happens when the human explicitly invokes `show`.

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
is reset. Docker's init process runs as PID 1 and reaps a detached process
after it exits, so completed background work does not accumulate as zombies.
Later execs still work, but they do not implicitly stop a process that is
still running or inherit that process's environment.

Its rootfs is never modified in place across a reset: `brokkr sandbox
reset` stops and removes the container entirely, and the next command
recreates it from the image, fresh. Nothing about the container's
internal state silently persists in a way that isn't either (a) still
there because nobody reset it, or (b) gone because someone did.

## Approval matching: exact by default, explicitly constrained templates by opt-in

`ApprovalStore.find()` hashes the canonical JSON-encoded argv array and
looks up an exact match — nothing fuzzier. This was a deliberate
decision made before any code was written, not a limitation to be lifted
later without thought: semantic or embedding similarity was considered
and rejected, because "similar" is a bad axis for a decision that skips
human review. `rm file.txt` and `rm -rf /` can be close in embedding
space; their consequences are not close at all.

That remains the default. With `BROKKR_APPROVAL_TEMPLATE_MATCHING=on`, a
human can instead choose `template` after a reviewed command runs, select
specific argv positions, and type one constraint for each: a path that must
stay lexically under `/workspace`, an exact enum of allowed strings, or a
regular expression using full-match semantics. Every other argv position and
the argv length remain exact. The originating value must satisfy its own
constraint before the rule can be saved.

The model never selects a variable position, proposes a constraint, or creates
a template. Matching is disabled by default, and an existing template is not
even consulted while the flag is off. A match is recorded as
`template_matched`, separately from an exact match's `auto_approved`, and the
final argv still passes through the static policy blocklist before execution.
This explicit human authorship is the safety property that avoids
reintroducing "the model decides what's safe to skip" under a new name.

## Possible eventual convergence: looking up a known-good script instead of generating one

Dogfooding found that "write a script that does X and save it as a
reusable file" is a materially less reliable task shape than a single
direct command, across more than one local model — see
docs/security-model.md's "What this deliberately does not defend
against" section for the specific reproduced failures. Asking the model
to generate fresh multi-line script content from scratch every time is
one way to hit this task shape; it is not the only way to accomplish it.

A curated catalog of already-written, already-tested scripts (were one
available to query) sidesteps the generation-reliability problem
entirely for anything already in it: instead of the model authoring new
code, the task becomes "find the existing script that matches this
description," which is a much easier and more checkable problem than
generating correct multi-line shell code. This is not designed or
scoped — noted here only as a more promising direction than "try a
bigger model" for this specific task shape, should script generation
ever become a priority.
