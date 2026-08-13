# Working on brokkr with Claude + Codex

This file exists so that any Claude Code session picking up this project
-- on any machine, any account -- can continue the same collaborative
workflow this project was actually built with, without having to
rediscover it from scratch. It describes a *methodology*, not this
specific developer's infrastructure -- no hostnames, IPs, or machine
names belong in this file, or anywhere else in this repo (see
CONTRIBUTING.md's "No personal data, ever" rule, which applies here too).

## The division of labor

This project is built by two AI tools working different roles, not one
tool doing everything:

- **Claude** designs, writes detailed implementation specs, dispatches
  work, and -- this is the part that actually matters --
  **independently verifies everything**, never trusting a summary of
  "done, tests pass."
- **Codex** implements against the written spec, in its own session,
  working on the same checkout.

If you are the Claude session reading this: assume you are the design-
and-verify role unless told otherwise. If you're picking this up on a
fresh machine, the human operator will tell you where Codex's session
lives and how to reach it (some kind of persistent terminal session on
another machine or the same one) -- that's operational detail specific
to their setup, not something this file should hardcode.

## Writing a spec for Codex

Before dispatching any real work, write a plan file
(`CODEX_<TOPIC>_PLAN.md` in the repo root -- these are working documents
for coordinating rounds, not shipped documentation; they're fine to
leave in the repo, existing ones are kept as a record of design
reasoning). A good plan has:

- **What this is and why**, including the actual problem/finding that
  motivated it -- link to a live dogfooding session's findings when
  that's the source, not a hypothetical.
- **The actual design decision**, spelled out, not left for Codex to
  improvise -- especially anything safety-relevant. If there's a reason
  a tempting-looking approach is wrong (e.g. "don't build a deterministic
  blocklist rule for this, here's why a heuristic would be unreliable"),
  say so explicitly.
- **A "What NOT to build" section.** This is load-bearing, not optional.
  Without it, Codex has repeatedly expanded scope beyond what was asked.
- **Concrete tests to add**, especially for anything safety-relevant --
  name the exact scenario, don't just say "add tests."
- **A manual verification checklist** -- the steps you (Claude) will
  personally re-run afterward, not just what Codex should check.
- **The standing constraints**: no personal data in tracked files
  (grep for anything that looks like a real hostname/IP/username before
  any commit), `pytest`/`ruff` clean, commit but don't push, don't touch
  the GitHub remote, log progress somewhere Codex can be told to write
  to.

Keep each round scoped to one coherent piece of work. Several small
rounds with real verification between them work better than one huge
round.

## Dispatching

However Codex's session is reached in this environment, the pattern
that's worked reliably:
1. Write the plan file into the repo.
2. Send Codex a short message pointing at the plan file and giving a
   one-paragraph summary of what it says and why -- don't make Codex
   read the whole thing blind with zero framing.
3. **Codex frequently reads a plan, summarizes it, and then stops
   without starting implementation.** If that happens, send an explicit
   follow-up: "Now implement the plan you just read -- start now." This
   is a real, repeated pattern in this workflow, not a one-off glitch --
   expect to need it most rounds.
4. Let it work. Rounds have taken anywhere from ~3 minutes (a narrow
   prompt-only fix) to ~20 minutes (a genuinely large feature like
   multi-step workflows). Don't rush it.

## Independent verification -- the actual point of this whole setup

When Codex reports done, **do not stop at reading its summary**. Every
single time:
1. `git log` / `git status` -- confirm what actually landed and that
   the working tree is clean.
2. Re-run `pytest` and `ruff` yourself, don't trust "tests passed" as
   reported.
3. Re-run the privacy grep yourself across the diff.
4. **Read the actual diff**, especially any logic that's security- or
   safety-relevant (approval matching, the policy blocklist, sandbox
   execution, anything touching what runs without a human decision).
5. For anything safety-relevant, **personally reproduce the scenario
   live** -- don't just trust that tests passing means the real behavior
   is correct. This project's own history includes at least one case
   where a careful-looking test suite still missed something that only
   showed up when a human (not Codex, not an automated test) tried the
   tool as a genuinely curious user. See CHANGELOG.md's entry on the
   shell-wrapped blocklist bypass for the concrete example -- that bug
   was found through ordinary use, not code review, and it's the reason
   this verification discipline exists at all, not a hypothetical
   justification.
6. Clean up any test state (databases, sandbox containers, temporary
   files) you created while verifying, before considering a round done.

If Codex's own platform declines a task (this has happened for
container-escape-style testing requests, likely due to how the request
was worded triggering an extra-caution filter around cybersecurity
framing) -- don't try to work around that decline. Either do the testing
yourself directly, or rephrase the *next* round's framing around
capability/behavior rather than attack/escape terminology.

## Dogfooding rounds (a distinct, valuable pattern)

Separately from build rounds, this project has repeatedly benefited from
rounds with **no plan file and no code changes allowed** -- just
genuinely using the tool as a curious real user would, writing down
whatever's found (organized as "worked as expected" vs. "worth a closer
look," with exact repro steps for the latter), and only *then* writing a
follow-up fix-plan round for whatever real findings came out of it. This
found real, non-obvious bugs repeatedly. Don't skip this pattern in
favor of only ever building forward -- alternating build rounds with
dogfooding rounds has been more productive than either alone.

## After a round lands

- Independently verify (above) before doing anything else.
- If there's a test/disposable deployment environment for this project,
  sync the verified change there and re-run the test suite in that
  environment too, not just on the primary checkout.
- Only push to the GitHub remote when the human operator explicitly
  asks for it in that moment -- approval doesn't carry over from a
  previous push, ask again each time.

## Why this matters enough to write down

The value of two AI tools building this together isn't that either one
is more capable alone -- it's that independent verification catches
things a single author (human or AI) tends to miss in their own work.
The discipline in this file is what makes that actually true in
practice rather than just in theory. Skipping the verification step to
save time defeats the entire premise of working this way.
