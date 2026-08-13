# Security policy

## Reporting a vulnerability

If you find a way to break out of the sandbox boundary described in
[docs/security-model.md](docs/security-model.md) — reaching the host
filesystem outside the mounted workspace, reaching the external network while
`BROKKR_SANDBOX_NETWORK=none` and no per-command grant was given, escaping the
container's resource limits, or escalating privileges inside or out of the
container — please report it
privately rather than opening a public issue.

**Email: hambalko@icloud.com**

Please include:

- The exact command(s) or sequence of steps that trigger it.
- What you expected to happen vs. what actually happened.
- Your Docker version and host OS, if it seems relevant.

This is a solo-maintained hobby project, not a company with a security
team or a bug bounty program — there's no SLA, but real sandbox-escape
reports will be taken seriously and prioritized over everything else.

## What counts as a security issue here

Given this project's stated scope (see README.md's "What this is not"
and docs/security-model.md's "What this deliberately does not defend
against"), the following **are** in scope:

- Anything that lets a command inside the sandbox read, write, or
  execute outside the single mounted workspace directory.
- Anything that lets a command reach the external network with
  `BROKKR_SANDBOX_NETWORK=none` set and no explicit `--allow-network` grant.
- Anything that lets a command escape the configured CPU/memory/PID
  limits.
- Anything that lets a command escalate privileges inside the container,
  or reach the Docker daemon/socket from inside it.
- A command from `permissions/policy.py`'s documented blocklist patterns
  that isn't actually blocked.

The following are **not** security issues, since they're documented,
known-and-accepted scope limits, not bugs:

- Container-escape via a host kernel vulnerability (Docker shares the
  host kernel; brokkr doesn't claim otherwise).
- A human approving a command that turns out to be a bad idea (human
  review is a judgment gate, not an automated safety mechanism).
- Anything requiring multi-tenant isolation (there is no user model;
  this is a single-operator tool).
