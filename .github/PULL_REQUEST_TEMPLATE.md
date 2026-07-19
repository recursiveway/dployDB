## What changed

Describe the bounded change and why it is needed.

## Safety impact

State whether this touches production mutation, backup verification, migration,
candidate checks, traffic, rollback, restore, recovery, state, or redaction.

## Validation

- [ ] Focused success and failure tests pass.
- [ ] `uv run pytest -q` passes with Docker and loopback access.
- [ ] Ruff check and format check pass.
- [ ] Strict mypy passes for `dploydb` and `demo`.
- [ ] User-visible changes and migrations are documented in `CHANGELOG.md`.
- [ ] No credential, production data, generated state, or local agent setting is included.
