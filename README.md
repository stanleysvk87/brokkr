# brokkr

A local, tool-augmented LLM agent that can **propose** and, after human
confirmation, **execute** shell commands inside a Docker sandbox — with
exhaustive logging so every proposed command, every approval decision, and
every execution is fully reconstructible after the fact.

Built for consumer hardware: developed and tested on a 6GB-VRAM laptop GPU,
targeting the same class of machine most homelab/local-LLM hobbyists
actually have, not a datacenter GPU. If you can run a 7B model in Ollama on
your machine, you can run brokkr.

**Why this exists**: most local-agent examples either don't actually execute
anything (they just print a suggested command and stop), or they execute
directly on your real machine with no isolation and no record of what
happened. brokkr is an attempt at the middle ground that a homelab actually
needs — a model that can act, inside a boundary that holds even when the
model is wrong, with a complete trail of what it proposed, what a human
decided, and what actually ran.

## Status

Stages 0–5 of the build are done: the sandbox mechanism, LLM proposals,
human confirmation, the policy blocklist, exact-match remembered approvals,
and explicit human-curated memory all work end-to-end and are covered by tests. See
[CHANGELOG.md](CHANGELOG.md) for exactly what was built and verified at
each stage, and [docs/architecture.md](docs/architecture.md) for how the
pieces fit together.

## What this is not

- **Not a replacement for a hardened sandbox** like gVisor or Firecracker.
  Docker here is a real, verified boundary (see
  [docs/security-model.md](docs/security-model.md)), but it shares the host
  kernel — it is not the right tool against a determined, actively
  malicious adversary with local access.
- **Not for untrusted multi-tenant use.** One user, one machine, one trust
  boundary. There is no concept of "users" in this project at all.
- **Not a fire-and-forget autonomous agent.** Every command that runs was
  either approved by a human at the time, or matches one a human explicitly
  chose to remember. There is no mode where brokkr acts entirely on its
  own.

## Prerequisites

- Docker, with your user in the `docker` group (so `docker ps` works
  without `sudo`).
- [Ollama](https://ollama.com), running locally, with a model pulled that
  supports structured output (any recent instruct/coder model works —
  development used `qwen2.5-coder:7b`).
- Python 3.10+.

## Install

```bash
git clone <this repo>
cd brokkr
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env   # adjust if your Ollama URL/model/paths differ
source .venv/bin/activate
```

## Quickstart

```bash
# Run an exact command directly in the sandbox -- no LLM involved.
# Useful for exploring what the sandbox can see, and for the safety
# checks in docs/security-model.md.
brokkr sandbox exec -- ls -la /workspace

# Ask the model to propose a command for a task, in plain language.
# You'll see its reasoning and the exact command before anything runs.
brokkr propose "list every file in the workspace, including hidden ones"

# See what's been remembered (auto-approved without asking again).
brokkr approvals list

# Forget a remembered command -- replace 1 with an ID from the list.
brokkr approvals revoke 1

# Add explicit workspace context for future proposals, then inspect it.
brokkr memory add "This workspace uses Python 3.12"
brokkr memory list

# Remove a note -- replace 1 with an ID from the memory list.
brokkr memory forget 1

# Wipe the sandbox container -- next command gets a fresh rootfs.
brokkr sandbox reset
```

Everything that happens is recorded in `logs/audit.db` (queryable SQLite),
`logs/blobs/<command_id>/` (full raw payloads — prompts, completions,
stdout/stderr), and `logs/audit.jsonl` (a flat summary for `tail -f`).

## Configuration

All configuration is environment variables, documented with their defaults
in [.env.example](.env.example) — copy it to `.env` and adjust. Nothing in
this repository ever contains real paths, hostnames, or IPs; `.env` is
gitignored specifically so your own local configuration stays local.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports, especially anything
that looks like a sandbox escape, should go through
[SECURITY.md](SECURITY.md) instead of a public issue.

## License

Apache-2.0 — see [LICENSE](LICENSE).
