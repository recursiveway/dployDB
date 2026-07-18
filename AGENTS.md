# AGENTS.md — DployDB

## Mission

Build DployDB as a real deployment-safety tool for SQLite applications, not a scripted demo.

The supported hackathon setup is deliberately narrow:

> One Linux VPS, one Docker Compose application, one SQLite database file, one migration command, and one HTTP health endpoint.

The core promise is:

> A failed rehearsal never touches production. A failed cutover before traffic activation restores the last verified application and database.

Before coding, read `IMPLEMENTATION_PLAN.md`. It contains the complete feature list, build order, acceptance gates, failure tests, and demo flow. Keep it updated with actual evidence as work progresses.

## Product boundaries

Support for the hackathon:

- Linux.
- One SQLite database.
- One Docker Compose application service.
- Local verified backups.
- Optional S3-compatible off-server backup.
- Developer-supplied migration command.
- HTTP health check and optional smoke-test command.
- Controlled maintenance period during production migration.

Do not build before the required milestones pass:

- Kubernetes or multi-host support.
- SQLite replication or point-in-time recovery.
- Authentication, billing, teams, or multi-tenancy.
- A custom migration language.
- Universal zero-downtime migrations.
- Automatic database rollback after production traffic is active.
- A large dashboard.
- Support for every framework, operating system, or cloud.

## Non-negotiable safety rules

1. Never modify production before backup verification, migration rehearsal, and candidate checks pass.
2. Never call a backup successful until it opens, passes SQLite checks, and has a SHA-256 checksum.
3. Use SQLite's online backup API for live snapshots. Do not copy a live database with `cp` or a normal file-copy call.
4. Only one deployment may run at a time. Use a durable operating-system-backed lock.
5. Release manifests and event logs must be written atomically and preserved after failures.
6. Every migration, hook, container, health check, and storage operation must have a timeout.
7. Do not use `shell=True` by default. Commands should be argument arrays.
8. Redact secrets from terminal output, JSON output, manifests, and logs.
9. Do not fake success in the real deployment path. Mocks belong only in tests.
10. Automatic database rollback is permitted only before the new release receives production traffic.
11. A manual restore must warn about possible data loss and back up the current state first.
12. Unknown or contradictory state becomes `recovery_required`; do not guess destructively.
13. Cleanup must be idempotent because recovery may repeat it.
14. Never delete the active or previous release's protected backup.
15. Preserve failure evidence and explain the next safe action in plain language.

## Required CLI

Implement:

```text
dploydb init
dploydb doctor [--deep]
dploydb backup [--upload]
dploydb verify <backup-or-release-id>
dploydb deploy --version <version> [--json] [--non-interactive]
dploydb status
dploydb releases
dploydb release show <release-id>
dploydb restore <release-id> [--yes]
dploydb recover [--yes]
dploydb version
```

Every failure must state:

- what failed,
- whether production changed,
- whether the previous application is running,
- whether recovery is required,
- where the relevant log is,
- the next safe action.

Use stable exit codes and stable JSON output for CI.

## Default stack

When the repository is empty, use:

- Python 3.12+.
- Typer.
- Rich.
- Pydantic and PyYAML.
- Python `sqlite3` backup API.
- HTTPX.
- Boto3 for optional S3-compatible storage.
- Pytest.
- Ruff.
- Mypy.
- `pyproject.toml` with an installable `dploydb` command.

Keep orchestration separate from integrations. Provide narrow interfaces for:

- application runner,
- traffic controller,
- health checker,
- backup storage.

Implement only Docker Compose, command-based traffic hooks, local storage, and one S3-compatible storage adapter during the hackathon.

## Exact build order

Do not skip ahead. A later milestone starts only after the earlier gate passes.

### 0. Repository and deterministic demo app

Build the package, CLI entry point, configuration example, and four demo releases:

- working v1,
- working v2 with a valid migration,
- broken migration,
- broken application health.

Gate: the demo is repeatable and uses real SQLite reads and writes.

### 1. Configuration, locking, state, and subprocess safety

Build strict configuration validation, secret redaction, deployment lock, atomic release manifest, append-only event log, stable errors, timeouts, and cleanup.

Gate: invalid config cannot reach the database; concurrent deploys are blocked; interrupted state is visible.

### 2. Backup, verification, and basic restore

Build SQLite preflight checks, online snapshot, checksum, `quick_check`, `foreign_key_check`, local metadata, `backup`, `verify`, and safe restore through a temporary path.

Gate: a live-writer backup is valid; corruption is detected; restore cannot destroy the current database if it fails.

Do not continue until this gate passes.

### 3. Migration rehearsal

Run the migration against a disposable copy of a verified snapshot. Capture output, enforce timeout, and rerun database checks.

Gate: a broken or timed-out migration leaves the production checksum and schema unchanged.

### 4. Candidate application validation

Start the new Docker Compose application against the rehearsed database on an isolated port. Inject test-mode environment variables, run HTTP readiness and optional smoke checks, capture logs, and clean up.

Gate: a broken candidate is rejected while the current application continues serving.

### 5. Controlled production cutover and pre-traffic rollback

In order:

1. Enable maintenance mode.
2. Stop the current application and background writers.
3. Create and verify a final backup.
4. Apply the rehearsed migration to production.
5. Start the new application while traffic is blocked.
6. Run final database and application checks.
7. Activate traffic.
8. Disable maintenance mode.
9. Record the active release.

If any step before traffic activation fails, restore the final backup, restart the previous application, restore its traffic target, verify health, and record the rollback.

Gate: forced production-migration and final-health failures restore both the application and database.

This is the main hackathon feature. Do not replace it with UI work.

### 6. Release history, manual restore, and crash recovery

Build release listing/details, restore preview, data-loss warning, backup-before-restore, interrupted-operation diagnosis, and `recover`.

Gate: simulated crashes at several cutover points either recover to a proven healthy state or stop with exact manual instructions.

Milestones 0 through 6 plus installation and quick-start documentation form the minimum production-useful release.

### 7. S3-compatible backup and retention

Upload only verified backups. Verify downloads before restore. Protect active and previous backups from retention.

Gate: a MinIO or equivalent integration test proves upload, download, checksum verification, and restore.

Build this before a dashboard.

### 8. Packaging and real-user documentation

Provide clean installation, `--help`, JSON/non-interactive operation, setup guide, Caddy or Nginx hook example, limitations, security notes, and uninstall behavior that preserves backups.

Gate: a new Linux environment can install and run the demo using the README only.

### 9. Presentation polish

After all core gates pass, add:

- Rich deployment timeline.
- Human-readable failure summaries.
- Generated release report.
- Optional schema diff.
- Deterministic one-command demo.
- Read-only dashboard only when time remains.

## Required deployment states

Use durable, explicit states:

```text
created
preflight_passed
snapshot_verified
rehearsal_passed
candidate_healthy
maintenance_enabled
current_app_stopped
final_snapshot_verified
production_migrated
new_app_healthy
traffic_activated
active
rollback_started
rolled_back
failed_safe
recovery_required
manual_restore_started
manual_restore_completed
```

Do not infer success only from a running process. State transitions require stored evidence.

## Required tests

At minimum, automate:

- missing/unreadable database,
- unwritable backup directory,
- live writer during snapshot,
- corrupted backup,
- migration non-zero exit,
- migration timeout,
- post-migration database failure,
- candidate startup failure,
- candidate HTTP 500,
- smoke-test failure,
- maintenance-hook failure,
- final-backup failure,
- production-migration failure and rollback,
- final-health failure and rollback,
- traffic-switch failure,
- concurrent deployment,
- crash during cutover and recovery,
- manual restore with backup-first behavior,
- remote checksum mismatch,
- retention protection,
- secret redaction.

Before marking work complete, run:

```bash
python -m pytest -q
ruff check .
ruff format --check .
mypy dploydb
```

Use equivalent commands only when the repository intentionally changes tools.

## Agent workflow

Before editing:

1. Read this file and `IMPLEMENTATION_PLAN.md`.
2. Inspect current code and tests.
3. Identify the current milestone and acceptance gate.
4. Update `IMPLEMENTATION_PLAN.md` with the planned change.
5. State the modules you will own.

While editing:

- Work on one milestone or one bounded part.
- Add tests with the implementation.
- Keep critical-path behavior real.
- Keep interfaces typed and small.
- Capture and redact command output before storing it.
- Use temporary files plus atomic replacement for important state.
- Keep timestamps in UTC.
- Make temporary-resource cleanup reliable on success, error, timeout, and interruption.
- Do not broaden scope while a safety gate is failing.

Before declaring completion:

1. Run focused tests.
2. Run the full validation suite.
3. Run the related CLI flow against the demo application.
4. Update the implementation plan with commands and observed results.
5. Report remaining safety gaps clearly.

For parallel agents, define interfaces first, assign separate module ownership, merge in milestone order, and rerun all integration tests after each merge. Avoid simultaneous edits to the configuration schema, state model, and main deployment orchestrator.

## Prohibited shortcuts

- No hard-coded deployment results.
- No plain live-database copy.
- No sleep-only readiness checks.
- No ignored or hidden test failures.
- No backup marked complete before verification.
- No direct UI mutation of release/database state.
- No silent destructive recovery.
- No dashboard before rollback is proven.

## Definition of done

The hackathon release is complete only when:

- a stranger can install it on clean Linux,
- `doctor` catches unsafe setup,
- a live SQLite snapshot is consistent and verified,
- a broken migration leaves production unchanged,
- a broken candidate leaves the old application running,
- a successful release updates application and database,
- a failed pre-traffic cutover restores application and database,
- manual restore warns and backs up the current state,
- interrupted deployment is recoverable or safely escalated,
- release history contains checksums, logs, state transitions, and health results,
- secrets are redacted,
- all required tests pass,
- the demo is deterministic and uses no manual state manipulation,
- documentation states limitations honestly.

When time is short, cut dashboard, notifications, schema visualization, extra providers, and extra runners first. Never cut backup verification, rehearsal, locking, durable state, rollback, restore warnings, failure tests, or clear errors.
