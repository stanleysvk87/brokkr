# brokkr

A local, tool-augmented LLM agent that can **propose** and, after human
confirmation, **execute** shell commands inside a Docker sandbox — with
exhaustive logging so every proposed command, every approval decision, and
every execution is fully reconstructible after the fact.

Built for consumer hardware: developed and tested on a 6GB-VRAM laptop GPU
(RTX 4050), targeting the same class of machine most homelab/local-LLM
hobbyists actually have, not a datacenter GPU.

**Status: early scaffold (Stage 0).** No sandbox, no LLM integration, and no
approval logic exist yet — see `docs/architecture.md` (coming in a later
stage) for the staged build plan.

## What this is not

- Not a replacement for a hardened sandbox like gVisor or Firecracker —
  Docker here is a *mistake-containment* boundary (catches accidental
  `rm -rf /`, runaway processes, network exfiltration attempts), not a
  defense against a determined, actively malicious model or attacker.
- Not for untrusted multi-tenant use. One user, one machine.

## Quickstart

Not ready yet — this section will be filled in once Stage 1 (sandbox
execution) and Stage 2 (LLM integration) land.

## License

Apache-2.0 — see [LICENSE](LICENSE).
