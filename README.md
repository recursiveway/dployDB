# DployDB

DployDB is being built as a deployment-safety tool for applications that use one SQLite database on one Linux server.

## Current status

Milestone 0 provides:

- an installable `dploydb` CLI with help and version commands;
- a deterministic Docker Compose demo application;
- working v1 and v2 release fixtures;
- a deliberately broken migration fixture;
- a deliberately unhealthy application fixture;
- real SQLite reads, writes, and data-preserving migration behavior.

The demo controller is **not** the DployDB deployment engine. Configuration parsing, verified backups, locking, migration rehearsal, candidate isolation, production cutover, rollback, and recovery are not implemented yet.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Docker Engine or Docker Desktop
- Docker Compose

Install the development environment:

```bash
uv sync --locked
```

The CLI can also be installed in an isolated environment:

```bash
pipx install .
dploydb version
```

## Start working v1

Reset the default demo database, apply the v1 migration, build the image, start the single Compose application service, and verify HTTP health:

```bash
uv run python demo/controller.py start-v1
```

The application is available at `http://127.0.0.1:4510`. Its real SQLite database is stored at:

```text
demo/.state/default/data/app.db
```

Create and read data through the application:

```bash
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"body":"written under v1"}' \
  http://127.0.0.1:4510/notes

curl --fail-with-body http://127.0.0.1:4510/notes
```

Stop the container without deleting the database, then start v1 against the same data:

```bash
uv run python demo/controller.py stop
uv run python demo/controller.py start v1
```

Running `start-v1` again intentionally resets the default instance to an empty v1 database.

## Migrate to working v2

Start v1 and create a note first, then stop application writes and apply the v2 fixture migration:

```bash
uv run python demo/controller.py stop
uv run python demo/controller.py migrate v2
uv run python demo/controller.py start v2
```

The v1 note is preserved with the default category `general`. V2 also accepts an explicit category:

```bash
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"body":"written under v2","category":"deployment"}' \
  http://127.0.0.1:4510/notes
```

These commands are deterministic fixture controls, not safe deployment orchestration.

## Observe the broken migration

Return to a clean v1 database and create data, then stop the application:

```bash
uv run python demo/controller.py start-v1
curl --fail-with-body \
  --header 'Content-Type: application/json' \
  --data '{"body":"must survive"}' \
  http://127.0.0.1:4510/notes
uv run python demo/controller.py stop
```

Run the broken migration:

```bash
uv run python demo/controller.py migrate broken-migration
```

It exits nonzero with the stable SQLite reason `no such table: deliberate_missing_table`. The migration is transactional, so the v1 schema and note remain unchanged. Restart v1 to inspect them:

```bash
uv run python demo/controller.py start v1
curl --fail-with-body http://127.0.0.1:4510/notes
```

## Observe the broken-health release

The broken-health fixture performs a valid v1-to-v2 migration, starts normally, reads SQLite successfully, and then deliberately returns HTTP 503 from `/health`:

```bash
uv run python demo/controller.py start-v1
uv run python demo/controller.py stop
uv run python demo/controller.py migrate broken-health
uv run python demo/controller.py up broken-health
uv run python demo/controller.py health
```

The last command exits nonzero and reports `fixture_broken_health`. To inspect the response directly:

```bash
curl --include http://127.0.0.1:4510/health
```

## Instances, ports, and cleanup

Use separate state and Compose projects with global options placed before the command:

```bash
uv run python demo/controller.py --instance second --port 4520 start-v1
uv run python demo/controller.py --instance second --port 4520 stop
```

Generated databases live below `demo/.state/<instance>/` and are ignored by Git. `stop` preserves the selected database. `reset` stops the service and recreates a clean migrated v1 database without starting it:

```bash
uv run python demo/controller.py reset
```

## Validation

Run the complete Milestone 0 suite:

```bash
uv lock --check
uv sync --locked --check
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy dploydb
uv run mypy demo
uv build
uv run python scripts/verify_pipx_install.py
```

The Docker integration tests require a running Docker daemon and perform their own Compose cleanup.

## Configuration example

`demo/dploydb.yaml` documents the intended future configuration contract. Milestone 0 does not load or validate it, and its placeholder traffic-hook paths are not implemented. See `IMPLEMENTATION_PLAN.md` for the complete safety requirements and milestone order.
