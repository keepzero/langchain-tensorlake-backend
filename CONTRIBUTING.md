# Contributing

Run `uv sync --locked` from the repository root. Keep dependency changes in
`pyproject.toml` and `uv.lock` together.

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Use unit tests for path validation, SDK calls, errors and protocol responses.
Real-service tests belong in `tests/integration_tests` and run only
when that directory is explicitly selected. Never use a production filesystem as
a test fixture. Keep credentials outside source files and logs.

Keep the backend independent of conversation identities, databases, sandbox
mounts and task lifecycle. Record significant changes in `docs/CHANGELOG.md`.
