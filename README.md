# DployDB

DployDB is being built as a deployment-safety tool for applications that use one SQLite database on one Linux server.

## Current status

Milestones 0 through 5 provide:

- an installable `dploydb` CLI with help and version commands;
- strict, duplicate-safe configuration parsing with environment interpolation;
- in-memory secret registration and output redaction;
- `dploydb init`, which creates a valid mode-`0600` starter file without overwriting;
- atomic generic operation manifests and append-only, redacted event logs;
- a durable `fcntl.flock` deployment lock with atomic owner metadata and stale-owner diagnosis;
- bounded, redacted subprocess execution with process-group timeout cleanup;
- `dploydb doctor`, with layered host checks, bounded SQLite verification, and explicit deferred checks;
- read-only `dploydb status`, including active, interrupted, stale, and recovery-required state;
- SQLite online backups that remain consistent while the application is writing;
- immutable mode-`0600` backup databases and metadata under a mode-`0700` directory;
- `dploydb backup` and read-only `dploydb verify <backup-id>` with stable JSON output;
- an internal stopped-application restore engine that creates a verified pre-restore backup and
  restores it automatically if replacement fails;
- an internal, lock-protected migration rehearsal stage that creates a verified
  snapshot, migrates only a private disposable copy, captures redacted command
  evidence, enforces process-tree timeout cleanup, reruns SQLite checks, and
  records `rehearsal_passed` or a durable failed-safe result;
- an internal Docker Compose candidate runner that creates an operation-scoped
  one-off project, binds only the configured loopback candidate port, overlays
  the rehearsed SQLite directory at the configured container target, validates
  live mounts/ports/labels before acceptance, captures bounded redacted logs,
  and proves idempotent container/network cleanup;
- a public `dploydb deploy --version <version>` command that runs verified
  backup, migration rehearsal, isolated candidate checks, controlled
  maintenance, stopped-writer final backup, production migration, final
  application/database checks, and traffic activation under one durable lock;
- exact previous-container preservation and automatic application/database
  rollback for failures before new traffic activation, with durable release,
  hook, health, checksum, and event evidence;
- a deterministic Docker Compose demo application;
- working v1 and v2 release fixtures;
- a deliberately broken migration fixture;
- a deliberately unhealthy application fixture;
- a production-only final-health failure fixture used to prove real cutover
  rollback after candidate validation passes;
- real SQLite reads, writes, and data-preserving migration behavior.

The demo controller is **not** the DployDB deployment engine. Migration
rehearsal and candidate validation are internal stages of the public deployment
engine. Controlled production cutover and automatic pre-traffic rollback are
implemented. Public manual restore, interrupted-operation recovery, remote
backup storage, and retention are not implemented yet.

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

## Deploy with DployDB

After adapting the generated configuration to the real absolute database,
Compose, backup, health, migration, and traffic-hook paths, deploy with:

```bash
dploydb deploy --version v2 --config /absolute/path/to/dploydb.yaml
dploydb deploy --version v2 --config /absolute/path/to/dploydb.yaml \
  --json --non-interactive
```

The command does not modify production until the verified snapshot, migration
rehearsal, and isolated candidate checks pass. During cutover it enables
maintenance, stops the exact current container, creates and verifies a final
backup, migrates production, starts and checks the new application while normal
traffic remains blocked, then activates traffic and records the release.

If a failure occurs before traffic activation, DployDB restores the final
backup when needed, restarts the exact previous container, activates the old
target, disables maintenance, verifies the old application/database, and
returns an `outcome` of `rolled_back`. Once new traffic may be active, DployDB
does not automatically restore the old database; uncertain routing or
post-activation failure is reported as `recovery_required` with the evidence
log and next safe action.

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

Run the complete repository suite:

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

Create a restrictive starter configuration without overwriting an existing path:

```bash
uv run dploydb init
```

The generated `dploydb.yaml` is strict: duplicate keys, unknown fields, unsafe
candidate URLs, relative production paths, shell-style command strings, and
invalid timeout/retention values are rejected. `${VARIABLE}` interpolation is
resolved only after structural validation. Host and database checks are kept
out of parsing and performed by `doctor` or the relevant lock-tracked operation.
Candidate isolation defaults to container port `8080` and database volume target
`/data`; set `application.candidate_container_port` and
`application.database_volume_target` explicitly when the Compose service uses
different container-side values. Compose files may use the reserved
`DPLOYDB_VERSION` interpolation value; test-mode configuration cannot override it.
Production deployment additionally requires `application.production_project`,
`application.production_port`, and `application.production_health_url`, with a
distinct candidate port. All four traffic commands are bounded argument arrays;
they must implement maintenance on/off and new/old target activation
idempotently.

`demo/dploydb.yaml` is another valid example for the deterministic fixture. Its
`/srv/dploydb-demo` paths and placeholder traffic hooks must be adapted before
host validation or real use.

Run the implemented host checks with:

```bash
uv run dploydb doctor --config /absolute/path/to/dploydb.yaml
uv run dploydb doctor --config /absolute/path/to/dploydb.yaml --deep
```

Normal mode checks configuration, required paths and executables, Docker and
Compose CLI availability, candidate-port availability, lock ownership, durable
operation state, and bounded read-only SQLite `quick_check` and
`foreign_key_check` results. Deep mode additionally runs SQLite
`integrity_check`, cleaned-up write probes, disk-space checks, Docker daemon
inspection, and Compose service validation. Both modes explicitly report
remote storage, migration execution, application health, and traffic execution
as skipped. `doctor` never runs a developer migration as a diagnostic; the
implemented internal rehearsal stage runs it only against a verified disposable
copy inside a lock-tracked operation. The other skipped integrations remain
assigned to their later milestones.

Inspect current state without creating, repairing, or deleting state files:

```bash
uv run dploydb status --config /absolute/path/to/dploydb.yaml
uv run dploydb status --config /absolute/path/to/dploydb.yaml --json
```

`status` exits `0` for coherent idle or active state and `60` for interrupted,
stale, contradictory, corrupt, or recovery-required state. See
`IMPLEMENTATION_PLAN.md` for the complete safety requirements and milestone
order.

Create and independently reverify a local backup:

```bash
uv run dploydb backup --config /absolute/path/to/dploydb.yaml
uv run dploydb backup --config /absolute/path/to/dploydb.yaml --json
uv run dploydb verify backup_0123456789abcdef0123456789abcdef \
  --config /absolute/path/to/dploydb.yaml
```

The configured backup directory must be owned privately with mode `0700` when
it already exists. Backup database and metadata files are written with mode
`0600`; metadata is published last and is the success marker. `verify` accepts
only committed backup IDs in Milestone 2. Public release restore and remote
upload remain assigned to later milestones.

The rehearsal and candidate lifecycle APIs remain internal implementation
stages of `deploy`. A configured migration command must use `database.path_env`
for its database target and must not hard-code the production path or perform
unrelated production side effects; DployDB does not claim to sandbox an
arbitrary developer-supplied executable.
