# Contributing

Bug reports, feature discussion, and pull requests are all welcome.
Sandbox-escape or other security findings should go through
[SECURITY.md](SECURITY.md) instead of a public issue.

## Development setup

```bash
git clone <this repo>
cd brokkr
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

## Before opening a PR

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

Both must pass. The PDF/OCR tooling tests are opt-in because the normal suite
does not require Docker; after changing the sandbox Dockerfile or its tool list,
also run:

```bash
BROKKR_RUN_DOCKER_TESTS=1 .venv/bin/pytest -q \
  tests/test_pdf_ocr_tooling.py tests/test_library_seed_integration.py
```

If you touch `sandbox/docker_sandbox.py` or
`permissions/policy.py`, please also manually re-run the checks
described in docs/security-model.md's "What was actually tested"
section. Most of those checks are not part of the automated suite: they need a
real Docker daemon and some are deliberately destructive, including actually
running `rm -rf /` inside a throwaway sandbox container.

## Conventions this project actually enforces

- **argv lists only, never `shell=True`, never a joined shell string.**
  Every place that runs a command follows this — see
  `sandbox/docker_sandbox.py`'s module docstring for why this alone
  already defeats most classic shell-injection patterns.
- **No personal data, ever.** No real hostnames, IPs, usernames, or file
  paths outside the repo's own directory structure, anywhere in tracked
  files — not even in a comment or a docstring example. `.env` and
  `data/`/`logs/` are gitignored specifically so local configuration and
  history never end up in a commit. Before committing, it's worth a
  quick `grep` for your own username/hostname across what you're about
  to stage.
- **Model output is never trusted implicitly.** Anywhere the LLM's
  output is used (see `llm/client.py`), it goes through Ollama's
  structured-output constraint *and* independent Pydantic validation
  afterward — constrained decoding guarantees syntactically valid JSON,
  not sane values. If you're adding a new place the model's output
  drives behavior, both layers apply, not just one.
- **Comments explain *why*, not *what*.** Code should be readable enough
  that a comment restating what a line does is unnecessary. A comment
  earns its place by capturing a non-obvious constraint, a rejected
  alternative and why it was rejected, or a decision that would
  otherwise look arbitrary.

## Why some things are deliberately not built yet

If you're looking at `permissions/policy.py` and thinking "this blocklist
is missing pattern X", or at `approvals/store.py` and thinking "this
should support fuzzy matching" — both are likely deliberate scope
decisions, not oversights. See docs/architecture.md's "Approval
matching" section and the module docstrings before assuming something
needs adding; several designs (semantic/embedding-based approval
matching, in particular) were considered and explicitly rejected during
planning, not simply not-yet-implemented.
