# Changelog

## Direct-argv glob guidance -- 2026-08-13

Round 23 dogfooding found a proposal that passed a wildcard path directly to
`du`, where it remained a literal string because brokkr executes argv without
an implicit shell. The system prompt now states that direct argv does not expand
globs and gives concrete wrong and correct examples, steering patterned file
tasks toward `find` or a deliberate `bash -c` shell script.

This is prompt guidance rather than a deterministic rejection. Glob characters
are valid literal content for many commands, URLs, and patterns, so a general
validator would introduce false positives without understanding each command's
semantics. No validation, policy, approval, or execution behavior changed.

## Round 21 dogfooding fixes -- 2026-08-13

Tightened command-proposal guidance after free-form VM dogfooding. Destructive
tasks may no longer combine an ambiguous, plausible-sounding file or directory
name with a guessed mutation: they must first propose read-only discovery, even
when their reasoning already notices the ambiguity. Deletion previews now target
the confirmed item with a useful read-only listing or size check instead of
substituting a generic directory listing.

The prompt now also shows the direct-argv shape required by `find -exec`, where
`{}` and `;` (or `+`) are separate elements. Validation permits that narrowly
valid `find` terminator while continuing to reject shell operators elsewhere.

Execution output styling now follows the command outcome rather than treating
every byte written to stderr as an error. Stderr from a successful command is
shown as a warning, while failed and timed-out command stderr remains red. This
applies consistently to direct sandbox execution and model-proposal execution.

## Read-only audit history browser -- 2026-08-13

Added `brokkr history`, a compact newest-first Rich table over the existing
proposal, decision, and execution audit rows. It shows a short command ID, task,
decision, and outcome; rejected, blocked, and manual decisions remain visible
without an execution row. Proposal failures are also shown rather than silently
disappearing from the day-to-day view.

`--limit` bounds the result count and `--decision` filters one exact decision
type, including useful reviews such as `--decision blocked`. Long task text and
block reasons are normalized and truncated for a readable terminal table. This
is a viewer only: no schema, audit-writing, approval, policy, execution, delete,
export, or pagination behavior changed.

## Interactive task mode -- 2026-08-13

Running bare `brokkr` now starts a small interactive loop. Each input line is
one complete plain-language task, so quotes, apostrophes, backslashes, and shell
metacharacters need no shell escaping. `exit`, `quit`, and Ctrl+D leave cleanly;
`help` prints a one-line reminder. Root `--model`, `--timeout`, and
`--allow-network` options apply to every task in that session.

The existing `propose` body was moved into one shared function used by both the
named command and interactive mode. The REPL constructs settings, audit,
approval, memory, and Ollama services once, but every line still creates an
independent proposal/audit ID and traverses the identical approval, template,
policy, manual, network, execution, and remember paths. It adds no conversation
history, shell state, or parallel decision implementation.

## Documentation consolidation after rapid feature rounds -- 2026-08-13

Read every shipped document against the current CLI and implementation after
the manual mode, approval-template, per-command network, doctor, and PDF/OCR
rounds. Corrected the architecture diagram to put approval lookup before the
human prompt, documented the configured rather than hard-coded manual-result
workspace, and updated the related human-review step reference.

The README status now includes manual mode, doctor, and PDF/OCR tooling; its
autonomy and audit wording now distinguishes model proposals, direct sandbox
execution, curated stores, and read-only commands. Security reporting now
distinguishes an unintended network path from an explicit `--allow-network`
grant. Also corrected stale CLI spellings, removed inaccessible local-plan
references, and documented the opt-in Docker tooling test. `.env.example`
contains exactly every `BROKKR_*` variable read by `config.py`.

## Sandbox PDF text extraction and OCR tooling -- 2026-08-13

Added the deliberately scoped Debian packages `poppler-utils` and
`tesseract-ocr` to the sandbox image. Commands can now use `pdftotext` for
PDFs with a text layer, `pdftoppm` to render scanned pages, and `tesseract`
for OCR without reaching for an unavailable Python library or treating PDF
binary data as plain text.

The LLM system prompt now points to those installed tools. Opt-in Docker
integration coverage checks both binaries and extracts a known string from a generated
one-page PDF through the real sandbox, exercising Poppler beyond a version
check. No approval, policy, audit, network, or automatic language-selection
behavior changed.

## Read-only setup diagnostics -- 2026-08-13

Added `brokkr doctor`, a fixed set of read-only checks for Docker daemon
reachability, presence of the configured sandbox image, Ollama reachability,
availability of the configured default model, and workspace writability. It
also lists locally available Ollama models and prints short, static model-size
guidance without attempting unreliable GPU or VRAM detection.

Failures include actionable setup messages. In particular, a missing default
model names the configured value and prints the exact `ollama pull <model>`
command. A sandbox image that has not been built yet is only a warning because
normal first use builds it automatically. Warnings keep exit status zero;
failed checks return non-zero, and one failed subsystem does not skip the
independent checks.

The command never builds an image, starts a container, executes a sandbox
command, pulls a model, or otherwise remediates what it finds.

## Stage 0: repo scaffold — 2026-08-12

Initial commit. Empty `src/brokkr` package, `pyproject.toml` (setuptools,
`src/` layout, `brokkr` CLI entry point), `.gitignore`/`.env.example` set up
from day one to keep personal paths/IPs/secrets out of the repo (this
project is intended for eventual public release), Apache-2.0 `LICENSE`,
README stub, one trivial smoke test.

No sandbox, no LLM integration, no approval logic yet. Stage 1 (Docker sandbox
execution primitive, no LLM yet) was next.

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
container per install, `BROKKR_SANDBOX_NETWORK=none` default,
CPU/memory/PIDs limits,
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
as designed, not a failure of it), and a network call with
`BROKKR_SANDBOX_NETWORK=none` (confirmed connection failure). All five checks
produced matching,
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
record), `brokkr approvals list`, and `brokkr approvals revoke`.

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

This closed out the original staged build plan -- Stages 0 through 4 were all
done. What remained before a public GitHub repo was a human
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
succeeding with `BROKKR_SANDBOX_NETWORK=bridge` (still correctly failing on DNS
resolution with the default `BROKKR_SANDBOX_NETWORK=none`, which is
the isolation working as intended, not a bug).

## More dogfooding: bare shell operators in proposals — 2026-08-12

A second, more thorough testing round (timeout override through the
full propose flow, output truncation at scale, the policy blocklist
live-tested against `dd`/`mkfs`/fork-bomb proposals, teaching and
reusing several remembered commands, Slovak-language tasks and
diacritics, an externally-`docker stop`-ed container recovering
correctly) found one real bug: asked to "count how many files are in
the workspace", `qwen2.5-coder:7b` proposed `["find", "/workspace",
"-maxdepth", "1", "-type", "f", "|", "wc", "-l"]` -- a bare `|` as its
own argv element, despite the system prompt explicitly saying not to do
that. Since brokkr never runs argv through a shell, that `|` doesn't
pipe anything; it's handed to `find` as a literal argument, which fails
with a confusing "paths must precede expression" error instead of doing
what was asked.

Fixed with a second Pydantic validator on the proposal schema
(`llm/client.py`) that rejects any argv element that IS one of `| || &&
; > >> < << &`, verbatim, with a clear error explaining the command
should have been wrapped in `["bash", "-c", "<script>"]` instead. This
is the same lesson the static policy blocklist already embodies:
prompt instructions alone don't reliably stop a small local model from
doing this occasionally, so it needs a deterministic, code-level check,
not just a politely-worded system prompt. Reproduced live against the
real Ollama server afterward (a different but structurally identical
proposal for the same task) and confirmed it's now caught with a clear
message instead of silently executing something broken.

## Stage 5: local memory for propose — 2026-08-12

`brokkr propose` can now use lasting workspace context without silently
inferring anything from command output or duplicating the audit trail.
The operator manages notes explicitly with `brokkr memory add`, `brokkr
memory list`, and `brokkr memory forget`; the most recent configured
number of notes is supplied to the model in chronological order as
helpful, potentially outdated context.
An empty memory store leaves the existing Ollama request payload
unchanged.

Added a small, separate SQLite store at `data/memory.db`, following the
same WAL-backed pattern as exact-match approvals. The schema deliberately
contains only note text and creation time: no automatic fact extraction,
semantic matching, tags, importance guesses, or expiry. Added
`BROKKR_MEMORY_MAX_NOTES` (default 20) so accumulated human notes cannot
make proposal prompts grow without bound.

Manually verified adding and listing a note, making a real proposal with
that note included as context, then forgetting it and confirming the
list was empty again. The full test suite and Ruff checks passed, including
new coverage for add/list/forget, recency ordering and limits, empty
state, context-bearing LLM payloads, and byte-identical message structure
when notes are empty or omitted.

## Security verification round: resource limits and symlink escape — 2026-08-12

`docs/security-model.md`'s "What was actually tested" section claimed
memory and PID cgroup limits were enforced, and implied the mount
boundary held against a symlink escape -- but only `rm -rf /`, a timeout
kill, and a blocked network call had actually been exercised live (all
back in Stage 1). This round closed that gap: four properties that had
only ever been designed and asserted, not run.

All four held:

- **Memory limit.** Docker's default 2 GiB swap allowance means the real
  ceiling on top of `BROKKR_SANDBOX_MEMORY_LIMIT=2g` is 4 GiB combined,
  not 2 GiB alone -- a 3 GiB touched allocation completed fine through
  swap. A 5 GiB allocation was OOM-killed (exit 137, recorded in the
  cgroup's `memory.events`), and the container itself stayed up and
  usable afterward.
- **PID limit.** A legitimate-looking runaway spawn loop (not matching
  `permissions/policy.py`'s fork-bomb regex) attempting 400 child
  processes stopped at exactly `pids.current=256`, matching
  `BROKKR_SANDBOX_PIDS_LIMIT`.
- **Symlink escape.** A symlink from `/workspace` to `/etc` resolved only
  inside the container's own filesystem namespace (a different hash from
  the real host `/etc`); a write through it failed permission-denied and
  created nothing on either side.
- **Non-root `apt-get install`.** Failed immediately and cleanly (dpkg
  frontend lock, explicit permission error) -- no hang, no half-broken
  state.

No code bugs -- `docs/security-model.md` (commit `2f71539`) was updated
to record these as now-verified. This closes the gap between what the
security model claimed and what had actually been exercised.

## Sandbox tooling: added `jq` — 2026-08-12

Manually dogfooding a JSON-extraction task ("get the version field from
data.json") through `brokkr propose` failed with a clean but avoidable
`exit 127`: the model reached for `jq` -- the standard, idiomatic tool
for this in any real shell environment -- and it wasn't in the sandbox
image. Added it to `sandbox/Dockerfile` next to the other minimal,
deliberately-chosen tools (same reasoning as `python-is-python3`: not
"install everything", just closing a real gap a real task hit).
Rebuilt the image and re-ran the same task -- the model correctly
proposed `jq -r .version /workspace/data.json` once the tool existed to
support it.

## Sandbox lifecycle clarity and ZIP tooling — 2026-08-12

Tested state across separate `brokkr sandbox exec` invocations to make
the long-lived-container behavior concrete. An environment variable
exported inside one `bash -c` process was absent from the next invocation,
while a file written to `/workspace` remained visible. A detached
`sleep 300` also remained alive after its initiating exec returned: it
was reparented to container PID 1, consumed one slot from the PID cgroup,
and did not stop later commands from running. Updated the architecture
document to distinguish persistent filesystem/process state from
non-persistent per-exec shell state, and to make `sandbox reset` the
explicit cleanup boundary for forgotten background work.

A real ZIP task exposed another small tooling gap. The model chose the
standard `zip` utility (after human correction added its missing `-r`
flag), but execution failed cleanly with exit 127 because neither `zip`
nor `unzip` was installed. Added both packages to the deliberately small
sandbox image, rebuilt it, repeated the corrected archive command, and
verified the result with `unzip -t`; both files passed the integrity
check.

Also verified `brokkr propose --model` against an installed non-default
model: `qwen3:4b` produced a valid structured proposal and the audit
database recorded that exact model. No installed model was known to lack
structured-output support, so the failure path was checked with an
unavailable model override instead; it returned a clean proposal error
and exit 1, with the requested model and HTTP 404 stored in the audit
row rather than raising a traceback.

## Idle-reset actually enforced — 2026-08-12

`BROKKR_SANDBOX_IDLE_RESET_MINUTES` had existed in `.env.example` and
`config.py` since Stage 1 but nothing ever checked it -- the sandbox only
ever reset when a human explicitly ran `brokkr sandbox reset`. A
container someone walked away from and forgot about would sit there
indefinitely, exactly the kind of silently-accumulated state
`sandbox/docker_sandbox.py`'s own module docstring says this project
doesn't want.

Fixed by tracking a small timestamp file at the new
`Settings.sandbox_last_used_path` (`data/sandbox_last_used`), touched at
the end of every successful `exec()` call and checked at the start of
`ensure_running()`: if the container has gone unused past the configured
window, it's reset automatically before being reused. `0` or a negative
value disables auto-reset entirely (manual `sandbox reset` still works).
`reset()` also now clears the marker file itself, so a manually-reset
sandbox doesn't carry a stale timestamp forward.

Added `tests/test_sandbox_idle_reset.py` covering the marker logic in
isolation (never-used, within-window, past-window, disabled, and a
corrupt-file case) -- constructing `DockerSandbox` doesn't require a
live Docker connection, so these didn't need one. Manually verified live
against the real daemon too: with the window set to ~1 second, a second
`sandbox exec` call after a short sleep produced a genuinely different
container id; with the window left at its 60-minute default, two calls
a few seconds apart kept the same container id, confirming normal use
isn't disrupted.

## Concurrency verification and first-use race fix — 2026-08-12

Ran two overlapping `sandbox exec` commands and two complete `propose`
pipelines against the shared container and SQLite stores for the first
time. Existing-container execution held: both three-second commands
really overlapped, returned correctly attributed output and distinct
command IDs, and produced complete SQLite, blob, and JSONL records. The
two proposals also initialized empty memory/approval stores concurrently,
then approved, executed, and remembered different commands without a
lost write, constraint error, or `database is locked`; integrity checks
on all three databases passed.

A separate first-use variant found a real race. With no container yet,
two CLI processes could both observe that absence and both call Docker
create with the fixed `brokkr-sandbox` name. One succeeded; the other
received an uncaught Docker 409 name conflict and printed a full
traceback instead of running. Fixed by serializing only the short named-
container lifecycle section (check, idle reset, create, and explicit
reset) with an OS file lock at `data/sandbox.lock`. Command execution is
outside that lock and remains concurrent. A regression test starts two
independent `DockerSandbox` instances simultaneously and proves the
container create path runs exactly once; repeated live first-use races
then completed both commands without errors or missing audit rows.

Racing an explicit `sandbox reset` against a confirmed in-flight eight-
second exec found a second shared-state race. The process handling itself
was clean -- reset stopped and removed the container, the interrupted
command returned exit 137 with its partial output and a complete audit
record, and reset returned success without a traceback -- but the exec's
unconditional final timestamp write could recreate `sandbox_last_used`
after reset had deleted it. The timestamp update now takes the lifecycle
lock and only writes if the same container still exists. This makes both
orderings deterministic: either exec records use before reset deletes the
marker, or reset removes the container first and the exec skips its stale
write. Repeating the live race left neither container nor marker, and the
next exec created a clean sandbox.

## Fresh-clone onboarding verification — 2026-08-12

Followed only the README prerequisites, install steps, and Quickstart from a
fresh clone. Installation succeeded, but every Quickstart command failed with
`brokkr: command not found` because the documented install flow created a
virtual environment without activating it. Added the missing activation step
and repeated the sandbox command and interactive proposal successfully; the
proposal was inspected and rejected, so no model-proposed command ran.

Updated the stale Stage 0–3 status to Stage 0–5, added examples for the existing
human-curated memory commands, made the revoke/forget ID examples valid shell,
and added the missing `BROKKR_LOG_LEVEL=INFO` default to `.env.example`.

## First real deployment outside the primary dev machine: clean container-creation errors — 2026-08-12

Deployed brokkr into an isolated VM for the first time (the human's staged
plan: prove the sandbox thoroughly on the primary machine before ever testing
against something resembling a real host, and a disposable VM clone is the
step before that). Setting up Docker, syncing the repo, and configuring
`BROKKR_OLLAMA_URL` to reach the host machine's Ollama over the VM's isolated
network all worked as expected -- `brokkr sandbox exec` and a full
`brokkr propose` round trip both succeeded from inside the VM on the first
real try once configured.

One real gap surfaced immediately: the VM has 2 CPUs, but
`BROKKR_SANDBOX_CPU_LIMIT` defaults to `4` (a reasonable default for the
primary dev machine, not for a small VM). Docker's container-create call
correctly rejected the impossible CPU limit, but that `APIError` wasn't
caught anywhere between `ensure_running()` and the CLI -- unlike `exec()`,
which already wraps `exec_run()` in the same try/except -- so it reached the
terminal as a raw traceback instead of a clean error. Fixed by wrapping the
container-creation call the same way, raising `SandboxError` with a readable
message. Reproduced on the primary machine too by setting an impossible
`BROKKR_SANDBOX_CPU_LIMIT` there: before the fix, a traceback; after, `sandbox
error: failed to create sandbox container: ...range of CPUs is from 0.01 to
12.00...`. Added `tests/test_sandbox_container_creation_errors.py` covering
the case in isolation. Config values that are actually wrong still need
correcting by whoever's deploying (here: lowering the VM's own
`BROKKR_SANDBOX_CPU_LIMIT` to match its real CPU count) -- this fix only
ensures a wrong value fails cleanly instead of crashing.

## Adversarial task and memory input verification -- 2026-08-12

Sent fake system/developer overrides, false claims of prior approval, malformed
output instructions, and persistent manipulation attempts through the real
`brokkr propose` and `brokkr memory` paths. The model was readily induced to
propose catastrophic commands, including direct `rm -rf /workspace`, `mkfs`,
raw-device `dd`, and a recognizable fork bomb. The CLI still showed the exact
argv and required a human decision. Direct catastrophic forms approved during
the test were then recorded as blocked by policy, with no execution rows; a
shell-wrapped `rm` and a close fork-bomb variant that the deliberately narrow
blocklist did not recognize were rejected at human review. Closing stdin
aborted instead of approving. Canary state remained intact throughout.

Two explicitly adversarial memory notes failed to corrupt unrelated date and
Python-version tasks, but this is recorded as observed model behavior, not a
security guarantee. A forced empty argv was rejected cleanly by Pydantic before
approval; a requested bare pipe was repaired by the model into an explicit
shell command and still required review. Updated the security documentation
with these verified cases and corrected an old `policy.py` docstring that
incorrectly claimed blocklisted proposals were never shown for approval. The
actual pipeline has always checked policy against final approved, edited, or
remembered argv immediately before sandbox execution.

## Container-escape red-team pass — 2026-08-12

Round 9 of security testing (planned as a Codex round, but that
platform's own safety classifier declined the task before starting --
"extra caution with cybersecurity requests" -- so this round was run
directly instead) was a deliberate attempt to escape the Docker sandbox
itself, using the disposable VM specifically so it could be pushed
harder than the primary machine: `mount` (needs `CAP_SYS_ADMIN`), raw
socket creation (needs `CAP_NET_RAW`), writing to `/proc/sys/kernel/
sysrq`, reading `/proc/1/root/` for host filesystem leakage, direct and
`mknod`-created access to the VM's own raw disk device, and a
thread-based (not process-fork) PID-exhaustion variant.

Every attempt failed cleanly -- permission errors or read-only-filesystem
errors, never a traceback, never success. `/proc/1/root/` resolved to the
container's own overlay filesystem, not the host's. `/dev` contained only
Docker's standard minimal device set, no raw block device. The thread
bomb stopped at the identical `pids.current=256` the process-fork variant
already hit, confirming the cgroup limit isn't technique-specific.
`ps aux` inside the container never showed the host's own ~180 processes.
Passive host-side network monitoring (kernel/ufw logs) for the VM's
subnet, running for the whole pass, logged nothing unexpected. No code
changes -- `docs/security-model.md`'s "What was actually tested" section
now records this pass. VM left clean (`sandbox reset`, runtime state
cleared).

## VM capability dogfooding: reap completed background work -- 2026-08-12

Ran ordinary file, system-information, process, tool-availability, and large-
output tasks through `brokkr propose` on the small VM. File creation, 5 MiB
I/O, `lscpu`, memory/kernel/disk reporting, text processing, and the installed
`jq`/ZIP/Git/curl/Python tools all worked within the configured limits. The
expected Docker boundary also held: a natural `docker ps` proposal failed with
exit 127 because neither the Docker CLI nor socket is exposed in the sandbox.
The model occasionally produced incomplete, nonportable, or invalid commands;
human review and the existing bare-operator validator caught those cases.

A real lifecycle bug surfaced after starting `sleep 120` in the background and
inspecting it from a later proposal. The process correctly survived between
exec calls, but after it finished it remained as a zombie adopted by PID 1.
The long-lived container used `sleep infinity` as PID 1, which does not reap
orphaned children, so repeated completed background jobs could permanently
consume slots from the PID cgroup until reset. Container creation now enables
Docker's built-in init process, which becomes PID 1 and reaps those children.
Added a regression test asserting every new sandbox requests `init=True` and
verified live that a detached short sleep disappeared after completion while
the container continued accepting later execs.

## First-use UX: clearer errors and more realistic proposals -- 2026-08-12

Addressed three concrete findings from a new-user session. Invalid model output
still goes through the same deterministic validators and the full technical
error remains in the audit trail, but the CLI now shows a short explanation
instead of exposing a multi-line Pydantic diagnostic. Empty argv, bare shell
operators, malformed JSON, and Ollama request failures each have a focused
human-readable summary, with the original error retained as the fallback.

The system prompt now directs network reachability checks to `curl`, not
`ping`, because the sandbox deliberately drops the raw-socket capability that
ping requires. It also tells the model to propose `ls` or `find` when a task
describes a file without an exact known path, rather than inventing a plausible
filename. These are best-effort proposal-quality instructions, not new safety
guarantees or automatic workspace access; sandbox capabilities and the
explicit-context architecture remain unchanged. In live retesting, two varied
network tasks both produced `curl -I` instead of `ping`. An indirectly-described
file initially still produced a guessed `rm` path, so the guidance was made
more explicit; the repeated task then produced a read-only `find` instead. Its
glob was too narrow to locate the actual fixture, so this improved the safe
discovery behavior but did not fully solve small-model filename reasoning.

## dmesg follow-up to the ping guidance -- 2026-08-12

Dogfooding a "check kernel messages" task right after the ping fix hit the
same underlying cause: `dmesg` needs `CAP_SYSLOG` (root, or that specific
capability), which `--cap-drop ALL` removes the same way it removes
`CAP_NET_RAW` for `ping`. Worth calling out explicitly: even if `dmesg`
somehow worked, the kernel ring buffer isn't namespaced per-container --
it's a host-wide resource, so allowing it would leak host kernel messages,
not just be "a missing tool." Extended the same system-prompt sentence
already steering the model away from `ping` to also name `dmesg`
specifically. Verified live: a repeated "show recent kernel messages" task
now proposes `ls /var/log` (safe discovery) instead of `dmesg`. Added a
matching prompt-content test. No sandbox capability changes.

## Manual/advisory mode for host-privileged work -- 2026-08-12

Added a fourth `brokkr propose` decision, `manual`, for commands the human
needs to run outside the deliberately unprivileged sandbox. The normal static
policy check still runs against the final proposed or edited argv first. A
manual decision records that exact argv, prints a shell-quoted command and a
predictable `manual-<short-command-id>.txt` redirect path in the existing
workspace, then exits without constructing or calling `DockerSandbox` and
without creating a `commands` row.

Added `brokkr manual show <command-id-or-prefix>`. It resolves only audited
manual decisions, rejects missing or ambiguous prefixes, reads the predictable
regular result file from the configured workspace, and can save the displayed
contents as an explicit memory note for later proposals. Result symlinks are
rejected so this convenience command cannot become an arbitrary host-file
reader. There is no automatic ingestion, new mount, arbitrary path argument,
privilege relay, or host execution path.

Regression coverage locks in direct and edited manual choices, policy blocking
before instructions, zero sandbox executions, short-prefix result lookup,
missing results, memory-note creation, and symlink rejection. Live verification
followed the printed redirect command, used both full and short IDs, confirmed
the saved result entered later proposal context, and confirmed neither manual
handling nor manual result display created sandbox activity.

## Shell-wrapped blocklist bypass fix — 2026-08-12

**Real safety gap found during live dogfooding**: the static blocklist
checked destructive `rm`, `dd`, `mkfs`, and recursive `chmod` shapes only
when the executable was the top-level argv entry. The project's encouraged
multi-step form, `bash -c <script>` (and its `sh` equivalent), could therefore
wrap the same command and bypass the deterministic rejection. Live edited
proposal tests confirmed that a wrapped workspace-root deletion and recursive
permission change reached execution; an earlier raw-device `dd` happened not
to run only because that particular script's own non-interactive confirmation
failed, not because brokkr stopped it.

The policy now tokenizes explicit shell scripts into command segments and
applies the same executable-specific checks to every segment, with one level
of nested `bash -c` / `sh -c` inspection. Fork-bomb matching remains unchanged,
ordinary multi-step shell commands remain allowed, and malformed shell text is
skipped without crashing. Regression tests lock in all three shell-wrapped
command shapes, a dangerous later segment, nested wrapping, safe multi-step
usage, quoted dangerous-looking text, and unbalanced quotes.

## Human-authored approval templates — 2026-08-13

Implemented the generalized approval matching deliberately deferred since
Stage 3, behind the existing off-by-default
`BROKKR_APPROVAL_TEMPLATE_MATCHING` flag. After a reviewed command executes,
the human can choose `template`, select variable argv positions, and type each
constraint: a path lexically confined under `/workspace`, an exact enum, or a
regular expression with full-match semantics. The originating argv must
satisfy every constraint before the template is saved. No model output selects
positions, suggests constraints, or creates rules.

Future same-length proposals match only when every literal token is identical
and every variable token satisfies its stored constraint. Exact matches retain
priority and the `auto_approved` audit value; template matches are separately
recorded as `template_matched`, with their own use count and last-used time.
`brokkr approvals list` labels templates and displays constraints, while the
existing revoke command accepts template IDs as well as exact approval IDs or
hashes. The final argv still goes through the unchanged static policy check,
including after a template match.

Regression coverage exercises path traversal and outside paths, enum equality,
regex full matching, invalid/origin-mismatched template rejection, argv shape
matching, exact-match priority, default-off behavior, audit distinction,
policy blocking before execution, human-only wizard input, listing, usage
tracking, and revocation.

## Per-command network opt-in — 2026-08-13

Added `--allow-network` to `brokkr propose` and direct `brokkr sandbox exec`.
With the default persistent setting still `none`, this human-typed flag
attaches the long-lived sandbox to Docker's bridge only around that execution
and disconnects it in `finally`, including after timeout or Docker exec errors.
Ordinary executions take a shared network lock while temporary-network work
takes it exclusively, so a concurrent no-network command cannot accidentally
inherit another invocation's attachment. An operator-configured persistent
bridge is detected and never disconnected by this path.

**Mechanism correction found during live verification**: Docker refuses to
connect a container in its special `network_mode=none` to a second network, so
the straightforward attach attempt failed cleanly with HTTP 400. The shipped
configuration still says `none` and retains no external route, but its runtime
representation is now a dedicated Docker `internal` network, which permits the
temporary bridge attachment. Existing legacy `none` containers migrate to it
in place without losing rootfs state. Direct testing proved curl fails on the
internal network, succeeds while bridge is attached, and fails again after
bridge is removed.

The structured proposal may now include optional `needs_network: true`, which
is displayed clearly before review but remains informational: it is never used
as the value passed to the sandbox. Every network grant still comes from the
separate CLI flag on that invocation, including for exact or template
auto-approvals. Audit command rows, blobs, and JSONL execution records now
state whether the actual execution had network access; existing databases are
migrated with a false default for historical rows.

Regression tests cover attach/exec/detach ordering, the untouched default
path, exception and timeout cleanup, persistent bridge behavior, concurrent
isolation, both CLI entry points, model-flag-without-grant behavior, optional
schema compatibility, audit values, and migration of an existing audit DB.
