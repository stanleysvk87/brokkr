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
