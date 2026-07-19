# DployDB

[![CI](https://github.com/recursiveway/dployDB/actions/workflows/ci.yml/badge.svg)](https://github.com/recursiveway/dployDB/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dploydb.svg)](https://pypi.org/project/dploydb/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

DployDB is being built as a deployment-safety tool for applications that use one SQLite database on one Linux server.

> [!WARNING]
> DployDB 0.1.1 is Alpha software. Its safety gates are real and extensively
> tested, but public interfaces may still change in documented `0.x` minor
> releases. Read the [supported limits](docs/limitations.md), keep independent
> backups, and prove the complete failure/restore flow on a non-production host
> before relying on it for production.

## Quick start: install and run the real deployment demo

This path starts a real v1 Docker Compose application and SQLite database, then
uses the installed `dploydb` CLI—not the fixture controller—to rehearse, validate,
and deploy v2. Run it from a checked-out DployDB source tree on Linux with Python
3.12+, `pipx`, Docker Engine, and the Docker Compose plugin.

```bash
python3 --version  # must report 3.12 or newer
pipx --version
docker version
docker compose version

git clone https://github.com/recursiveway/dployDB.git
cd dployDB
pipx install dploydb==0.1.1
dploydb --no-color version
```

Create the known-good v1 application, then generate private absolute-path demo
configuration. `start-v1` intentionally resets only the selected `quickstart`
demo instance.

```bash
python3 demo/controller.py --instance quickstart --port 4510 start-v1
python3 -m demo.prepare \
  --instance quickstart --port 4510 --candidate-port 4511
. demo/.state/quickstart/dploydb.env
```

Run the complete host audit and deploy v2 with machine-readable output:

```bash
dploydb --no-color doctor --deep \
  --config demo/.state/quickstart/dploydb.yaml
dploydb --no-color deploy --version v2 \
  --config demo/.state/quickstart/dploydb.yaml \
  --json --non-interactive > /tmp/dploydb-quickstart-result.json
python3 -c 'import json; p=json.load(open("/tmp/dploydb-quickstart-result.json")); assert p["ok"] is True and p["outcome"] == "active"; print(p["release_id"])'
curl --fail-with-body http://127.0.0.1:4510/health
dploydb releases --config demo/.state/quickstart/dploydb.yaml
```

The JSON assertion proves CI can parse the real deployment result. The v1
database is migrated to schema version 2, the candidate is tested on an isolated
copy and port, and normal traffic is activated only after final production
checks. Stop the demo container without deleting its database, backups, or
release evidence:

```bash
python3 demo/controller.py --instance quickstart --port 4510 stop
```

If either port is already occupied, choose two unused distinct ports in all
three commands. For production setup, continue with the
[first-run guide](docs/first-run.md), [Nginx hook example](examples/nginx/README.md),
[security model](docs/security.md), [limitations](docs/limitations.md), and
[backup-preserving uninstall](docs/uninstall.md).

## Current status

Milestones 0 through 8 provide:

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
- `dploydb backup [--upload]` and read-only `dploydb verify <backup-id>` with stable JSON output;
- S3-compatible off-server replication, including Cloudflare R2, with local
  verification before upload, database-first/metadata-last commit, full remote
  readback verification, bounded requests/retries, and runtime-only credentials;
- verified remote hydration when a protected local backup is absent, used by
  manual restore and interrupted-operation recovery without accepting corrupt
  local evidence;
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
- required final-backup replication before production migration, plus
  post-activation local/remote retention that always protects the active and
  immediately previous releases' rehearsal and final backups;
- read-only `dploydb releases` and `dploydb release show <release-id>` history
  with validated active/previous pointers and preserved failure evidence;
- `dploydb restore <release-id>`, which previews the protected immediately
  previous release, warns about data loss, and creates a verified backup of the
  current state before any confirmed database replacement;
- `dploydb recover`, which reconciles durable intent, backups, checksums, exact
  Docker resources, traffic evidence, and the OS lock before offering an
  idempotent recovery plan;
- real abrupt-process crash tests after maintenance, current-app stop, and
  production migration, plus a real backup-first previous-release restore;
- a deterministic Docker Compose demo application;
- working v1 and v2 release fixtures;
- a deliberately broken migration fixture;
- a deliberately unhealthy application fixture;
- a production-only final-health failure fixture used to prove real cutover
  rollback after candidate validation passes;
- real SQLite reads, writes, and data-preserving migration behavior.
- explicit ANSI-free `--no-color`/`NO_COLOR` behavior and installed help audits
  for every required command;
- finite per-operation evidence logs, with byte and record-count limits that
  preserve append-only failure evidence;
- a clean-Linux-tested installed-CLI quick start, production first-run guide,
  validated Nginx traffic hooks, security/limitations guidance, and an
  uninstall path proven to preserve database, backup, release, and event bytes.

The demo controller is **not** the DployDB deployment engine. Migration
rehearsal and candidate validation are internal stages of the public deployment
engine. Controlled production cutover, automatic pre-traffic rollback,
release-aware manual restore, interrupted-operation recovery, verified
off-server backup, and protected retention are implemented.

## Prerequisites

- Python 3.12 or newer
- `pipx` for an isolated end-user installation
- Docker Engine or Docker Desktop
- Docker Compose

Install the published Alpha CLI:

```bash
pipx install dploydb==0.1.1
dploydb version
```

Contributors can install the checked-out source tree with `pipx install .`.

Repository development and validation use [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
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
When `backup.remote.required` is true, the stopped-writer final backup must be
committed and read back from remote storage before production migration intent
is recorded. A failed upload restarts and verifies the previous application
without migrating or restoring the unchanged database.

If a failure occurs before traffic activation, DployDB restores the final
backup when needed, restarts the exact previous container, activates the old
target, disables maintenance, verifies the old application/database, and
returns an `outcome` of `rolled_back`. Once new traffic may be active, DployDB
does not automatically restore the old database; uncertain routing or
post-activation failure is reported as `recovery_required` with the evidence
log and next safe action.

## Inspect and restore releases

List validated local history or inspect one immutable release record:

```bash
dploydb releases --config /absolute/path/to/dploydb.yaml
dploydb releases --config /absolute/path/to/dploydb.yaml --json
dploydb release show release_0123456789abcdef0123456789abcdef \
  --config /absolute/path/to/dploydb.yaml --json
```

Manual restore is intentionally limited to the protected immediately previous
release. Previewing is read-only and prints the exact application, database
backup, checksum, and data-loss warning:

```bash
dploydb restore release_0123456789abcdef0123456789abcdef \
  --config /absolute/path/to/dploydb.yaml
dploydb restore release_0123456789abcdef0123456789abcdef \
  --config /absolute/path/to/dploydb.yaml --json
```

After reviewing the preview, confirm interactively or use `--yes` explicitly:

```bash
dploydb restore release_0123456789abcdef0123456789abcdef \
  --config /absolute/path/to/dploydb.yaml --yes
```

DployDB revalidates the selection under the deployment lock, enables
maintenance, stops the current writer, creates and verifies a `PRE_RESTORE`
backup, restores the selected database, restarts and checks the selected
application, switches traffic, and then swaps active/previous pointers. A
pre-traffic failure restores and verifies the pre-restore database and current
application. Once selected traffic may have been exposed, DployDB does not
automatically replace the database again.

## Recover an interrupted deployment

`status` reports an abrupt or contradictory operation with exit code `60`.
Diagnose it without mutation:

```bash
dploydb recover --config /absolute/path/to/dploydb.yaml
dploydb recover --config /absolute/path/to/dploydb.yaml --json
```

An executable plan lists its exact ordered actions and requires confirmation.
Run it interactively or acknowledge it explicitly:

```bash
dploydb recover --config /absolute/path/to/dploydb.yaml --yes
```

Recovery can return to the verified previous application/database when traffic
was not activated, or finish an already checked new release when durable hook
evidence proves activation succeeded. If traffic activation is uncertain,
backup lineage conflicts, live identities cannot be proven, or unrelated
operations are unfinished, it refuses automatic mutation with
`recovery_required`, the evidence log, and the next safe action. Re-running a
partially interrupted recovery re-inspects live state and skips database
replacement when the checksum already matches the verified target.

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
inspection, and Compose service validation. When remote storage is disabled,
both modes report it as skipped. When enabled, normal mode validates its runtime
credentials and client configuration; deep mode also performs one bounded,
read-only, prefix-scoped bucket access probe. Migration execution, application
health, and traffic execution remain skipped. `doctor` never runs a developer
migration as a diagnostic; the implemented internal rehearsal stage runs it
only against a verified disposable copy inside a lock-tracked operation.

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

Enable an S3-compatible target by naming environment variables rather than
putting credentials in YAML. Cloudflare R2 uses its account endpoint and the
`auto` region:

```yaml
backup:
  local_directory: /srv/dploydb/backups/example-app
  keep_last: 10
  remote:
    enabled: true
    required: true
    provider: s3
    bucket: example-backups
    prefix: dploydb/example-app
    region_name: auto
    endpoint_url_env: DPLOYDB_S3_ENDPOINT_URL
    access_key_env: DPLOYDB_S3_ACCESS_KEY_ID
    secret_key_env: DPLOYDB_S3_SECRET_ACCESS_KEY
    timeout_seconds: 30
    max_attempts: 3
```

```bash
export DPLOYDB_S3_ENDPOINT_URL='https://ACCOUNT_ID.r2.cloudflarestorage.com'
export DPLOYDB_S3_ACCESS_KEY_ID='...'
export DPLOYDB_S3_SECRET_ACCESS_KEY='...'
dploydb doctor --deep --config /absolute/path/to/dploydb.yaml
dploydb backup --upload --config /absolute/path/to/dploydb.yaml --json
```

Use an R2 S3 access-key pair with access limited to the selected bucket. An R2
API token is not used by DployDB's S3 adapter. Never commit credentials or pass
them as command-line arguments. Uploaded database bytes are not considered
committed until DployDB reads them back and verifies size and SHA-256, then
publishes and rereads immutable metadata. Restore downloads to a private
temporary file and repeats size, checksum, and SQLite verification.
For AWS S3, set `region_name` to the bucket's AWS region instead of `auto`.

To validate another S3-compatible service without writing credentials to disk
or command history, run the acceptance helper and answer its no-echo credential
prompts. It uses a unique child prefix, proves upload/list/download/SQLite
restore/delete, and removes only the objects it created:

```bash
.venv/bin/python scripts/verify_s3_compatibility.py
```

The configured backup directory must be owned privately with mode `0700` when
it already exists. Backup database and metadata files are written with mode
`0600`; metadata is published last and is the success marker. `verify` accepts
only committed backup IDs. Manual restore accepts a protected release ID rather
than a raw backup ID. After a release and its active pointer are durable,
retention keeps the newest `keep_last` unprotected backups and additionally
preserves every rehearsal and final backup referenced by the active and
immediately previous releases, both locally and remotely.

The rehearsal and candidate lifecycle APIs remain internal implementation
stages of `deploy`. A configured migration command must use `database.path_env`
for its database target and must not hard-code the production path or perform
unrelated production side effects; DployDB does not claim to sandbox an
arbitrary developer-supplied executable.

## Security, limitations, and removal

- [First production setup](docs/first-run.md)
- [Security model and trust boundaries](docs/security.md)
- [Supported scope, rollback boundary, and post-traffic data-loss risk](docs/limitations.md)
- [Nginx maintenance and fixed-port activation hooks](examples/nginx/README.md)
- [Uninstalling the CLI without deleting backups or evidence](docs/uninstall.md)

## Alpha lifecycle and compatibility

DployDB follows Semantic Versioning. Compatible `0.1.x` releases contain Alpha
fixes; later `0.x` minor releases may make documented breaking changes. `0.9.0`
is reserved for Beta, `1.0.0rc1` for the first release candidate, and `1.0.0`
for Stable. Durable state is never silently guessed or rewritten: an upgrade
must provide a tested migration or stop with an exact safe action.

Promotion is based on real deployment, failure-drill, recovery, and soak-time
evidence—not a calendar date. The exact gates and release procedure are in
[RELEASING.md](RELEASING.md), and user-visible changes are recorded in
[CHANGELOG.md](CHANGELOG.md).

## Community and license

DployDB is licensed under the [Apache License 2.0](LICENSE), copyright 2026
RecursiveWay. Bug reports and pull requests are welcome under
[CONTRIBUTING.md](CONTRIBUTING.md) and the [community conduct policy](CODE_OF_CONDUCT.md).
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
