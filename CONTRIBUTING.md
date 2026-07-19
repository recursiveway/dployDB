# Contributing to DployDB

DployDB protects production databases, so safety evidence is part of every
change. Bug reports, focused feature proposals, documentation improvements, and
pull requests are welcome.

## Before opening an issue

- Search existing issues first.
- Use the bug or feature template and remove credentials, tokens, database rows,
  hostnames, and unredacted logs.
- Report security vulnerabilities privately through the repository's
  [security advisory form](https://github.com/recursiveway/dployDB/security/advisories/new),
  not through a public issue.

## Development setup

Use Python 3.12+, `uv`, Docker Engine, and the Docker Compose plugin:

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy dploydb
uv run mypy demo
```

Run focused tests while developing, then run the complete gate before opening a
pull request. Docker and loopback access are required for the real integration
tests. Do not replace a real deployment-path check with a mock.

## Pull-request expectations

- Keep changes within one milestone or one bounded safety concern.
- Add tests for success, failure, timeout, cleanup, and secret-redaction paths.
- Preserve atomic state, bounded execution, verified backups, and the
  pre-traffic-only rollback boundary.
- Update `CHANGELOG.md` for user-visible changes and add migration guidance for
  any permitted `0.x` breaking change.
- Never include production data, secrets, generated demo state, or local agent
  configuration.

By intentionally submitting a contribution for inclusion in DployDB, you agree
that it is licensed under the project's [Apache License 2.0](LICENSE), as
described by section 5 of that license. The Alpha project uses no separate CLA
or DCO.
