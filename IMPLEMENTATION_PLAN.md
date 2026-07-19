# DployDB Hackathon Implementation Plan

## 1. Mission

Build **DployDB**, a production-useful deployment safety tool for applications that use one SQLite database on one Linux server.

DployDB must protect the currently working application while a new release is checked. It must create and verify a consistent database snapshot, rehearse migrations on a copy, test the candidate application, perform a controlled production cutover, and recover the previous working release when the cutover fails before production traffic is enabled.

The hackathon goal is not to support every platform. The goal is to make one common setup work reliably from end to end:

> One Linux VPS + one Docker Compose application + one SQLite database file + one migration command + one HTTP health endpoint.

The product must be useful after the hackathon. Do not build fake progress screens, hard-coded demo results, or a workflow that only works for one prepared database.

### Execution status

- **Completed milestone:** Milestone 0 — Repository, contract, and deterministic demo fixture (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 0A — Package and CLI bootstrap (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 0B — deterministic Docker Compose demo and four release fixtures (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1A — shared contracts and stable failures (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1B — in-memory secret registry and redaction boundary (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1C — strict configuration and safe `init` (`COMPLETE` on 2026-07-18).
- **Delivered architecture:** Shared standard-library HTTP/SQLite runtime, immutable release directories, one Docker Compose application service, host-visible ignored demo state, and a typed Python demo controller.
- **Delivered fixtures:** Working v1, data-preserving v2 migration, deterministic transactional migration failure, deterministic HTTP health failure, example configuration, documentation, and real integration tests.
- **Completed slice:** Milestone 1D — atomic operation state and append-only events (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1E — durable deployment lock and stale-owner diagnosis (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1F — bounded, redacted subprocess execution (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 1G — `doctor`, `status`, and the Milestone 1 integration gate (`COMPLETE` on 2026-07-18).
- **Completed milestone:** Milestone 1 — Configuration, locking, state, subprocess safety, and host diagnostics (`COMPLETE` on 2026-07-18).
- **Completed slices:** Milestones 2A through 2D — SQLite verification, immutable local backup, backup/verify CLI orchestration, and safe internal restore (`COMPLETE` on 2026-07-18).
- **Completed milestone:** Milestone 2 — consistent backup, verification, and basic stopped-application restore (`COMPLETE` on 2026-07-18).
- **Completed slices:** Milestones 3A and 3B — disposable rehearsal engine,
  durable verified-snapshot orchestration, and the production-isolation gate
  (`COMPLETE` on 2026-07-18).
- **Completed milestone:** Milestone 3 — migration rehearsal against a verified
  disposable snapshot (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 4A — isolated Docker Compose candidate runner,
  live isolation inspection, bounded logs, and proven idempotent cleanup
  (`COMPLETE` on 2026-07-18).
- **Completed slice:** Milestone 4B — bounded HTTPX readiness and optional
  process-group smoke checks (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 4C — durable candidate-validation orchestration
  and the real old-application-continuity gate (`COMPLETE` on 2026-07-19).
- **Completed milestone:** Milestone 4 — candidate application validation against
  the checked rehearsal database (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 5A — deploy-only production topology, atomic
  release state, generic application health boundary, and caller-owned
  pre-cutover candidate stage (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 5B — bounded command-based maintenance and
  traffic controller (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 5C — Docker Compose production application
  lifecycle and exact previous-application preservation (`COMPLETE` on
  2026-07-19).
- **Completed slice:** Milestone 5D — stopped-writer final verified backup,
  production migration, and caller-owned verified restore transaction
  (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 5E — internal deployment coordinator and
  complete pre-traffic rollback matrix (`COMPLETE` on 2026-07-19).
- **Completed slice:** Milestone 5F — public deploy CLI, real-Docker success and
  rollback flows, traffic-isolation proof, packaging, and final resource audit
  (`COMPLETE` on 2026-07-19).
- **Completed milestone:** Milestone 5 — controlled cutover and automatic
  pre-traffic application/database rollback (`COMPLETE` on 2026-07-19).
- **Completed milestone:** Milestone 6 — release history, public manual restore,
  and interrupted-operation recovery (`COMPLETE` on 2026-07-19).
- **Completed Milestone 6 slices:** 6A release history; 6B durable crash markers
  and read-only recovery diagnosis; 6C restore selection and preview; 6D
  controlled public manual restore; 6E idempotent recovery execution; and 6F
  real crash/restore integration gate, documentation, and packaging.
- **Milestone 4 slices:** 4A isolated Docker Compose runner, 4B bounded
  HTTP readiness and optional smoke checks, and 4C durable candidate-validation
  orchestration plus the real old-application-continuity gate.
- **Completed milestone:** Milestone 7 — S3-compatible verified backup and
  retention (`COMPLETE` on 2026-07-19). All slices 7A through 7G and the live
  Cloudflare R2 acceptance gate passed.
- **Current milestone:** Milestone 8 — real-world usability and packaging
  (`COMPLETE` on 2026-07-19). Slices 8A through 8C, the complete regression
  suite, installed-wheel audit, and clean-Linux README-only gate passed;
  Milestone 9 is the next allowed work.
- **Current release-readiness slice:** DployDB 0.1.0 Alpha publication
  (`LOCAL GATE COMPLETE; PUBLICATION PENDING` on 2026-07-19). This bounded post-Milestone-8 slice owns the
  Apache-2.0 license, public package metadata, distribution-content boundary,
  community/release policies, release verification, and least-privilege
  GitHub/TestPyPI/PyPI workflows. It does not change deployment behavior,
  durable state, rollback rules, or any public CLI/JSON contract.
- **Dependency workflow:** Use uv for project dependencies and development commands. Support and verify `pipx install .` as the isolated end-user installation path.
- **Repository outcome:** Every existing `.gitignore` rule remains, including `IMPLEMENTATION_PLAN.md`; `demo/.state/` was added for generated demo databases.

#### Milestone 0A acceptance evidence

Observed on 2026-07-18:

- `uv lock && uv sync --locked` — passed; resolved 26 packages and installed the project with Python 3.12.11.
- `uv lock --check && uv sync --locked --check` — passed; lockfile and environment require no changes.
- `uv run pytest -q tests/test_cli.py` — passed (`4 passed`).
- `uv run pytest -q` — passed (`4 passed`).
- `uv run ruff check .` — passed (`All checks passed!`).
- `uv run ruff format --check .` — passed (`5 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 3 source files`).
- `uv build` — passed; built `dist/dploydb-0.1.0.tar.gz` and `dist/dploydb-0.1.0-py3-none-any.whl`.
- `uv run python scripts/verify_pipx_install.py` — passed; a temporary isolated `pipx install .` exposed a working `dploydb` executable.
- Console and module entry points — passed; `version` and `--version` both printed `dploydb 0.1.0`.
- `.gitignore` SHA-256 remained `dfcdedf367ab02b1f26ebe8b44208d37bc9109de7517017c7adcab19ce4ddce6` before and after implementation.

Milestone 0 remained open after 0A and is completed by the 0B evidence below.

#### Milestone 0B acceptance evidence

Observed on 2026-07-18 with uv 0.11.15, Docker 29.4.0, and Docker Compose v5.1.2:

- `uv lock --check && uv sync --locked --check` — passed; the lockfile and environment require no changes.
- `uv run pytest -q tests/integration/test_demo_runtime.py` — passed (`6 passed`), proving real migrations, HTTP writes/reads, process-restart persistence, v2 data preservation, atomic broken migration, broken health, schema mismatch handling, and request validation.
- `uv run pytest -q tests/integration/test_demo_docker.py` — passed (`3 passed`), proving one-command v1 startup, bind-mounted persistence, deterministic reset, v2 migration, broken migration recovery, broken-health HTTP 503, and Compose cleanup.
- `uv run pytest -q` — passed (`13 passed`).
- `uv run ruff check .` — passed (`All checks passed!`).
- `uv run ruff format --check .` — passed (`13 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 3 source files`).
- `uv run mypy demo` — passed (`Success: no issues found in 6 source files`).
- `uv build` — passed; rebuilt `dist/dploydb-0.1.0.tar.gz` and `dist/dploydb-0.1.0-py3-none-any.whl`.
- `uv run python scripts/verify_pipx_install.py` — passed; isolated `pipx install .` still exposes working help and version commands.
- `uv run dploydb --version` and `uv run python -m dploydb version` — both printed `dploydb 0.1.0`.
- Manual temporary-state flow — `start-v1` reached healthy HTTP, POST returned `201` with note ID 1, GET returned the same persisted SQLite row, and `stop` cleaned up Compose resources.
- V2 tests observed `PRAGMA user_version = 2`, preserved the v1 row with category `general`, and wrote a new explicitly categorized row.
- Broken migration tests observed exit `1` and `no such table: deliberate_missing_table`; SHA-256, schema, `user_version`, and rows remained unchanged across repeated failures.
- Broken-health tests observed a running application returning HTTP `503` with `fixture_broken_health`; controller health returned exit `1` with the same reason.
- Docker cleanup verification found no remaining `dploydb-demo` containers or networks.
- `git check-ignore -v` confirmed `IMPLEMENTATION_PLAN.md` remains ignored by its existing rule and `demo/.state/` is ignored by the new additive rule.
- `pyproject.toml` and `uv.lock` were not changed during 0B; the demo uses only the Python standard library.
- The Docker runtime is pinned to the verified multi-platform OCI index `python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b`.

Milestone 0 is complete. Milestone 1 is the next allowed work; no later deployment-safety behavior is claimed.

#### Milestone 1 implementation scope

Planned on 2026-07-18:

- **Owned modules:** `dploydb/config.py`, `errors.py`, `models.py`, `state.py`, `locking.py`, `redaction.py`, `subprocesses.py`, and the Milestone 1 additions to `cli.py`.
- **CLI boundary:** add `init`, `doctor [--deep]`, and `status`, with stable human and JSON failures. Do not add a public `deploy`, backup, restore, or recovery command in this milestone.
- **Configuration boundary:** strict duplicate/unknown-key rejection, `${VARIABLE}` interpolation, secret registration without persisting resolved values, absolute production paths, argument-array commands, and side-effect-free structural validation before environment or database checks.
- **Durable-state boundary:** generic operation manifests and append-only events under the configured state directory. Reserve release manifests for later operations that can produce real release evidence.
- **Locking boundary:** use a durable `fcntl.flock` lock for exclusion and separate atomic owner metadata for diagnosis; PID metadata never replaces the kernel lock.
- **Subprocess boundary:** mandatory timeouts, no shell execution, bounded captured output, redaction before persistence/display, process-group termination, and preserved cleanup failures.
- **Doctor boundary:** Milestone 1 checks configuration, paths, dependencies, ports, locks, and interrupted state. SQLite integrity checks, remote storage connectivity, migrations, application health, and traffic execution remain assigned to later milestones and must not be reported as passed.
- **Gate tests:** real multiprocess lock contention, abrupt termination followed by explanatory `status`, invalid configuration proving no database/state access, subprocess timeout/process-tree cleanup, and full secret scans across terminal, JSON, manifests, events, lock metadata, and logs.
- **Evidence status:** complete; slices 1A through 1G and the cross-cutting Milestone 1 gate passed on 2026-07-18.

#### Milestone 1 stepwise execution plan

Complete these slices in order. Each slice must include its focused tests and recorded evidence before the next slice starts. A slice passing does not make Milestone 1 complete; the final cross-cutting gate still has to pass.

Progress tracker:

- [x] Milestone 0 — Repository, contract, and deterministic demo fixture (`COMPLETE` on 2026-07-18).
- [x] Milestone 1 overall — `COMPLETE` on 2026-07-18 after slices 1A through 1G and the final integration gate passed.
  - [x] 1A — Shared contracts and stable failures (`COMPLETE` on 2026-07-18).
  - [x] 1B — Secret registry and redaction boundary (`COMPLETE` on 2026-07-18).
  - [x] 1C — Strict configuration and safe `init` (`COMPLETE` on 2026-07-18).
  - [x] 1D — Atomic operation state and append-only events (`COMPLETE` on 2026-07-18).
  - [x] 1E — Durable deployment lock and stale-owner diagnosis (`COMPLETE` on 2026-07-18).
  - [x] 1F — Bounded, redacted subprocess execution (`COMPLETE` on 2026-07-18).
  - [x] 1G — `doctor`, `status`, and Milestone 1 integration gate (`COMPLETE` on 2026-07-18).

Tracking rule: change a slice from `[ ]` to `[x]` only after its slice gate passes, then add the completion date and exact validation evidence below that slice. Mark Milestone 1 overall complete only after the final cross-cutting gate passes.

##### 1A — Shared contracts and stable failures

Status on 2026-07-18: `COMPLETE`. This first Milestone 1 slice owns
`dploydb/models.py`, `dploydb/errors.py`, the failure-rendering boundary in
`dploydb/cli.py`, and their unit tests. It does not implement configuration,
state persistence, locking, subprocess execution, or deployment behavior.
Milestone 1 overall remains `IN PROGRESS`; slices 1B through 1G and the final
cross-cutting gate are still pending.

- Add `dploydb/errors.py` and the minimal shared types in `dploydb/models.py`.
- Define the stable exit-code mapping and one structured failure payload containing what failed, whether production changed, whether the previous application is running, whether recovery is required, the relevant log path, and the next safe action.
- Define operation identifiers, UTC timestamp serialization, and the required deployment-state enum without implementing deployment behavior.
- Keep rendering separate from the error model so the same failure can produce human-readable or stable JSON output.

Stable process exit-code contract introduced by this slice:

- `0` — success.
- `2` — command-line usage error (Typer/Click contract).
- `10` — invalid or missing configuration.
- `20` — failed safety or host precondition.
- `30` — another operation holds the deployment lock.
- `40` — an external command failed, timed out, or could not start.
- `50` — an operation failed safely without requiring recovery.
- `60` — state is uncertain or recovery is required.
- `70` — unexpected internal failure converted at the CLI boundary.

Slice gate:

- Unit tests prove every error maps to the documented exit code and required failure fields.
- Terminal and JSON rendering contain the same safety facts and never emit a traceback for expected failures.

Slice 1A acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/test_errors.py tests/test_cli.py` — passed (`23 passed`).
- `.venv/bin/python -m pytest -q` — passed outside the filesystem/network sandbox (`32 passed`), including all existing real SQLite/HTTP and Docker Compose Milestone 0 tests.
- `.venv/bin/ruff check .` — passed (`All checks passed!`).
- `.venv/bin/ruff format --check .` — passed (`16 files already formatted`).
- `.venv/bin/mypy dploydb` — passed (`Success: no issues found in 5 source files`).
- `.venv/bin/mypy demo` — passed (`Success: no issues found in 6 source files`).
- `.venv/bin/dploydb --help`, `.venv/bin/dploydb --version`, and `.venv/bin/python -m dploydb version` — passed; both version paths printed `dploydb 0.1.0`.
- Expected-failure tests exercised both human and stable JSON rendering, returned exit code `10`, included every required safety fact, and emitted no traceback.

##### 1B — Secret registry and redaction boundary

Status on 2026-07-18: `COMPLETE`. This slice owns `dploydb/redaction.py` and
`tests/test_redaction.py`. It adds an in-memory-only, non-serializable secret
registry; longest-first exact-value redaction; sensitive assignment, command
option, authorization header, URL credential, and signed-URL redaction; and
recursive JSON-compatible value redaction without mutating source values.
Configuration interpolation, durable state, locking, and subprocess execution
remain in slices 1C through 1F.

- Add `dploydb/redaction.py` before implementing any component that stores command or environment output.
- Register resolved secret environment values in memory only; do not serialize them into configuration fingerprints, operation records, events, lock metadata, or logs.
- Redact exact secret values and sensitive key/value forms in strings and nested JSON-compatible values.
- Make redaction idempotent and apply it before display or persistence, not afterward.

Slice gate:

- Unit tests cover tokens, credentials, signed URLs, nested values, repeated redaction, empty values, and overlapping secrets.
- A repository test scans all produced terminal, JSON, and file output and finds none of the test secrets.

Slice 1B acceptance evidence observed on 2026-07-18:

- `.venv/bin/python -m pytest -q tests/test_redaction.py` — passed (`25 passed`).
- `.venv/bin/python -m pytest -q` — passed (`57 passed`), including all existing real SQLite/HTTP and Docker Compose Milestone 0 integration tests.
- `.venv/bin/ruff check .` — passed (`All checks passed!`).
- `.venv/bin/ruff format --check .` — passed (`18 files already formatted`).
- `.venv/bin/mypy dploydb` — passed (`Success: no issues found in 6 source files`).
- `.venv/bin/mypy demo` — passed (`Success: no issues found in 6 source files`).
- `.venv/bin/dploydb --version` and `.venv/bin/python -m dploydb version` — both printed `dploydb 0.1.0`; slice 1B intentionally adds no public CLI command.
- Leakage coverage registered exact, empty, repeated, overlapping, and marker-like secrets; exercised raw token/credential forms, authorization headers, command options, URL user information, AWS-style signed URLs, nested JSON, terminal capture, JSON serialization, and file-bound output; and found none of the test secrets in produced output.
- Registry `repr` exposes only the secret count, serialization is refused, source mappings are not mutated, redacted key collisions preserve both evidence values, and applying redaction repeatedly produces the same result.

##### 1C — Strict configuration and safe `init`

Status on 2026-07-18: `COMPLETE`. This slice owns
`dploydb/config.py`, the bounded `init` additions to `dploydb/cli.py`,
Pydantic/PyYAML dependency metadata, `tests/test_config.py`, and the related
CLI tests. It updates the shipped configuration example and documentation only
where the new behavior changes their truth. Durable state, locking,
subprocess execution, `doctor`, and `status` remain assigned to slices 1D
through 1G.

Implemented behavior:

- Parse YAML with duplicate-key detection, then validate strict nested models
  with unknown keys forbidden at every level.
- Keep structural parsing side-effect-free and separate it from environment
  interpolation and in-memory secret registration; defer all host, path,
  database, state, socket, Docker, and command checks to `doctor`.
- Validate absolute production paths, local HTTP candidate URLs, argument-array
  commands, positive timeouts, ports, retention, remote-backup structure, and
  environment-variable names without resolving or touching host resources.
- Add an exclusive, mode-`0600` `dploydb init` path whose generated commented
  starter configuration passes the same parser and which preserves any
  existing path unchanged.
- Run focused configuration/CLI tests, the full suite and static checks, and a
  manual temporary-directory `init`/parse/preserve flow before marking 1C
  complete.

- Add Pydantic and PyYAML dependencies, then implement `dploydb/config.py` with nested models matching the documented YAML contract.
- Reject duplicate YAML keys, unknown keys, invalid types, empty argument arrays, non-positive timeouts, invalid retention values, unsafe URLs, and relative production paths.
- Split loading into phases: parse and structural validation first, environment interpolation and secret registration second, and host/path checks later in `doctor`.
- Ensure structural configuration failures perform no database, state-directory, subprocess, socket, or Docker access.
- Implement `dploydb init` with a valid commented starter configuration, restrictive file creation, and refusal to overwrite an existing file unless an explicit future overwrite contract is added.

Slice gate:

- Table-driven tests cover valid configuration plus every invalid field family, duplicate and unknown keys, missing environment variables, and secret interpolation.
- A spy-based test proves invalid configuration causes no database or state access.
- `init` creates a configuration that parses successfully and preserves an existing file.

Slice 1C acceptance evidence observed on 2026-07-18:

- `.venv/bin/python -m pytest -q tests/test_config.py tests/test_cli.py` — passed
  (`85 passed`), covering strict valid configuration, every nested invalid-field
  family, duplicate and unknown keys, required fields, malformed and missing
  interpolation variables, secret registration, post-resolution revalidation,
  enabled/disabled remote structure, the shipped demo configuration, mode-0600
  creation, write-failure cleanup, and byte-for-byte existing-file preservation.
- The invalid-configuration spy gate replaced database, state/filesystem probe,
  subprocess, and socket entry points with failing spies; structural rejection
  completed with zero operational calls.
- `.venv/bin/python -m pytest -q` — passed (`135 passed in 48.70s`) with real
  loopback HTTP and Docker Compose access, preserving every Milestone 0 through
  1B integration and safety test.
- `.venv/bin/ruff check .` — passed (`All checks passed!`).
- `.venv/bin/ruff format --check .` — passed (`20 files already formatted`).
- `.venv/bin/mypy dploydb` — passed
  (`Success: no issues found in 7 source files`).
- `.venv/bin/mypy demo` — passed
  (`Success: no issues found in 6 source files`).
- `uv lock --check && uv sync --locked --check` — passed; 32 packages resolved,
  31 checked, and the locked environment required no changes.
- `uv build` — passed; rebuilt `dist/dploydb-0.1.0.tar.gz` and
  `dist/dploydb-0.1.0-py3-none-any.whl` with Pydantic and PyYAML dependencies.
- `uv run python scripts/verify_pipx_install.py` — passed; an isolated
  `pipx install .` exposed the CLI with its new runtime dependencies.
- Manual temporary-directory CLI flow — `dploydb init --config ...` exited 0,
  created a mode-`0600` file, and the normal loader resolved it as project
  `example-app`; a second `init --json` exited 10, reported stable configuration
  safety facts, and preserved the existing file.
- `.venv/bin/dploydb init --help`, `.venv/bin/dploydb --version`, and
  `.venv/bin/python -m dploydb version` — passed; the new options were visible
  and both version paths printed `dploydb 0.1.0`.
- Configuration model `repr`/`str`, validation failures, terminal output, JSON
  failures, and existing-file errors were checked not to expose interpolated or
  file-contained test secrets.

Milestone 1 remains `IN PROGRESS`. Slice 1D is the next allowed work; no durable
state, locking, subprocess orchestration, host diagnosis, or deployment behavior
is claimed by 1C.

##### 1D — Atomic operation state and append-only events

Status on 2026-07-18: `COMPLETE`. This slice owns
`dploydb/state.py`, the narrowly required durable contracts in
`dploydb/models.py` and `dploydb/errors.py`, and focused state persistence and
interruption tests. It adds generic operation manifests and append-only
events only. Locking, subprocess execution, `doctor`, `status`, deployment,
backup, restore, and release manifests remain assigned to later slices.

- Add `dploydb/state.py` using generic operation manifests for Milestone 1. Do not create fake release manifests before a real deployment or backup exists.
- Write manifests through a same-directory temporary file, flush and `fsync`, apply restrictive permissions, use `os.replace`, and sync the containing directory where supported.
- Store append-only JSON Lines events with sequence, UTC timestamp, operation ID, state, and redacted evidence.
- Enforce allowed state transitions and preserve the last durable record after failures.
- Make readers treat malformed, contradictory, or incomplete state as `recovery_required` instead of guessing.

Slice gate:

- Tests cover transition validation, atomic replacement failure, append ordering, malformed/truncated state, permissions, UTC timestamps, and redaction before persistence.
- An interrupted writer leaves either the previous complete manifest or the new complete manifest, never partial JSON.

Slice 1D acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_state.py tests/fault_injection/test_state_interruption.py`
  — passed (`39 passed in 0.44s`). Coverage includes strict schemas and lifecycle
  invariants, private permissions, append-only ordering, a 1 MiB event bound,
  recursive redaction, malformed/truncated/contradictory state, sequence and
  timestamp corruption, symlink/path rejection, latest-operation inspection,
  and injected write, file-sync, replace, and directory-sync failures.
- Real child processes were killed immediately before atomic replacement and
  immediately after replacement but before directory sync. `manifest.json`
  remained parseable as either the complete previous record or complete new
  record; an event/manifest mismatch and abandoned temporary file produced
  `recovery_required` without repair or evidence deletion.
- `uv run pytest -q` — passed (`174 passed in 49.44s`), including the existing
  real SQLite/HTTP and Docker Compose integration coverage.
- `uv run ruff check .` — passed (`All checks passed!`).
- `uv run ruff format --check .` — passed (`23 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 8 source files`).
- Manual temporary-directory state flow — created an operation, recorded an
  in-progress transition and same-stage evidence, completed it successfully,
  reread four consistent events, and selected the same operation as latest.

Milestone 1 remains `IN PROGRESS`. Slice 1E is the next allowed work; 1D adds
no deployment lock, subprocess runner, host diagnostics, CLI state commands,
release manifest, or production behavior.

##### 1E — Durable deployment lock and stale-owner diagnosis

Status on 2026-07-18: `COMPLETE`. This slice owns
`dploydb/locking.py`, the narrowly required lock-owner contracts in
`dploydb/models.py`, `tests/unit/test_locking.py`, and
`tests/fault_injection/test_lock_interruption.py`. It provides a persistent
`fcntl.flock` lock file, atomic redacted owner metadata, explicit stale-owner
inspection, and idempotent release. It will not add `doctor`, `status`, a
deployment coordinator, subprocess execution, or any production mutation.

- Add `dploydb/locking.py` using Linux `fcntl.flock` for exclusion.
- Store redacted owner metadata atomically in a separate diagnostic file; PID metadata must never replace or override the kernel lock.
- Distinguish an actively held lock from stale metadata left after a killed process.
- Make acquisition/release context-managed and cleanup idempotent while preserving stale evidence needed by `status`.

Slice gate:

- A real multiprocess test proves only one holder can enter the protected section.
- Killing the holder releases the kernel lock, preserves enough metadata to diagnose interruption, and allows a later safe acquisition.
- PID reuse or unreadable metadata never causes destructive stale-lock cleanup.

Slice 1E acceptance evidence observed on 2026-07-18:

- `.venv/bin/python -m pytest -q tests/unit/test_locking.py tests/fault_injection/test_lock_interruption.py`
  — passed (`22 passed in 0.17s`). Coverage includes secure path modes,
  symlink rejection, strict owner lifecycles, UTC timestamps, redaction,
  atomic replacement failures, idempotent cleanup, read-only inspection,
  kernel-authoritative contention, stale-token acknowledgement, and PID-reuse
  and malformed-metadata safety.
- Real forked-process gates proved a second process receives stable exit code
  `30` without entering the protected section. Killing the holder with
  `SIGKILL` released the kernel lock, left the active owner record byte-for-byte
  intact, and allowed a later process to acquire and explicitly replace that
  stale owner by its exact token.
- `.venv/bin/python -m pytest -q` — passed with required loopback and Docker
  access (`196 passed in 49.67s`), preserving all real SQLite/HTTP and Docker
  Compose Milestone 0 integration coverage.
- `.venv/bin/ruff check .` — passed (`All checks passed!`).
- `.venv/bin/ruff format --check .` — passed (`26 files already formatted`).
- `.venv/bin/mypy dploydb` — passed
  (`Success: no issues found in 9 source files`).
- `.venv/bin/mypy demo` — passed
  (`Success: no issues found in 6 source files`).
- Manual temporary-directory flow observed `active=active`, blocked a second
  acquisition, atomically recorded `released`, and finished with `final=idle`.

Milestone 1 remains `IN PROGRESS`. Slice 1F is the next allowed work; 1E adds
no subprocess runner, host diagnostics, CLI state commands, deployment
coordinator, release manifest, or production behavior.

##### 1F — Bounded, redacted subprocess execution

Status on 2026-07-18: `COMPLETE`. This slice owns
`dploydb/subprocesses.py`, `tests/unit/test_subprocesses.py`, and
`tests/fault_injection/test_subprocess_tree_cleanup.py`. It adds a typed,
synchronous subprocess runner with exact environment construction, mandatory
timeouts, bounded concurrent stdout/stderr capture, redaction before results
leave the runner, cancellation, and complete POSIX process-group cleanup.
Expected command outcomes remain structured internal evidence so later
orchestrators can attach accurate production safety facts. This slice does not
add `doctor`, `status`, deployment orchestration, or any production mutation.

- Add `dploydb/subprocesses.py` with argument-array commands, mandatory positive timeouts, explicit environment construction, bounded output capture, duration, and exit metadata.
- Never use `shell=True`. Start a separate process group, terminate the complete group on timeout or cancellation, escalate to forced termination after a bounded grace period, and report cleanup failures.
- Redact captured output and command/environment diagnostics before returning them to any caller that can display or persist them.

Slice gate:

- Tests cover success, non-zero exit, missing executable, timeout, cancellation, large output truncation, secret output, and a child process that would otherwise outlive its parent.
- No subprocess or descendant remains after timeout tests complete.

Slice 1F acceptance evidence observed on 2026-07-18:

- `.venv/bin/python -m pytest -q tests/unit/test_subprocesses.py tests/fault_injection/test_subprocess_tree_cleanup.py`
  — passed (`32 passed in 1.23s`). Coverage includes exact environment
  construction, no-shell/new-session spawn arguments, success and non-zero
  results, missing executables, validation before spawn, timeout, cancellation,
  TERM-resistant forced termination, concurrent large stdout/stderr, head/tail
  truncation, invalid UTF-8, secret scans, and injected cleanup failure.
- The real process-tree fault test started a parent and descendant in the
  managed process group, timed the command out, and proved both PIDs were gone.
  The TERM-resistant test escalated to `SIGKILL`; no subprocess or output-reader
  thread remained after successful cleanup.
- An initial focused run exposed a blocking cross-thread buffered-pipe close.
  The implementation was corrected to keep both termination phases bounded and
  to report an unclosed reader as cleanup failure rather than wait indefinitely;
  the regression is covered by the focused suite.
- `.venv/bin/python -m pytest -ra --junitxml=/private/tmp/dploydb-full-suite.xml`
  — passed with required loopback and Docker access (`228 passed`, `0 failures`,
  `0 errors`, `50.421s`), preserving all real SQLite/HTTP and Docker Compose
  Milestone 0 integration coverage.
- `.venv/bin/ruff check .` — passed (`All checks passed!`).
- `.venv/bin/ruff format --check .` — passed (`29 files already formatted`).
- `.venv/bin/mypy dploydb` — passed
  (`Success: no issues found in 10 source files`).
- `.venv/bin/mypy demo` — passed
  (`Success: no issues found in 6 source files`).
- `uv lock --check` and `uv sync --locked --check` — passed; 32 packages were
  resolved, 31 checked, and the locked environment required no changes.
- Manual runner flow observed a redacted successful command and a bounded
  timeout with process-group cleanup: `secret_absent=true`,
  `success=succeeded`, `timeout=timed_out`, `timeout_cleanup=true`, and exit code
  `-15`. `.venv/bin/dploydb --version` still printed `dploydb 0.1.0`; 1F adds no
  public CLI command.

Milestone 1 remains `IN PROGRESS`. Slice 1G is the next allowed work; 1F adds no
host diagnostics, CLI state commands, deployment coordinator, release manifest,
or production behavior.

##### 1G — `doctor`, `status`, and Milestone 1 integration gate

Status on 2026-07-18: `COMPLETE`. This slice owns a new typed diagnostics
service in `dploydb/diagnostics.py`, the narrowly required report contracts in
`dploydb/models.py`, the `doctor`/`status` additions to `dploydb/cli.py`, focused
and real-process gate tests, and truthful README updates. It integrates the
existing configuration, redaction, state, lock, and subprocess boundaries
without adding deployment, backup, restore, recovery, SQLite-integrity, remote
storage, migration-execution, application-health, or traffic-execution behavior.

- Add `dploydb doctor [--deep]` and `dploydb status`, using the contracts above and stable human/JSON failure rendering.
- In Milestone 1, `doctor` checks configuration, required paths and executables, directory writability, Docker/Compose availability, candidate-port availability, lock ownership, and unresolved/interrupted operation state.
- The Milestone 1 `--deep` path may run the complete Milestone 1 host checks, but must not claim SQLite integrity, remote storage, migration, application-health, or traffic checks passed. Those checks remain in their assigned later milestones; Milestone 8 may extend deep diagnostics.
- `status` must be read-only and explain idle, active-lock, stale-owner, interrupted, and contradictory/recovery-required states with the next safe action.
- Do not add public `deploy`, `backup`, `restore`, or `recover` commands in this slice.

Final Milestone 1 gate:

- Run focused tests for every slice and the full real-process integration tests for lock contention, abrupt termination plus `status`, subprocess-tree cleanup, and secret leakage across every output sink.
- Exercise `init`, `doctor`, `doctor --deep`, and `status` manually against a temporary configuration based on the demo paths.
- Run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy dploydb`, and the existing demo validation needed to prove Milestone 0 still works.
- Record exact commands and observed results here before changing Milestone 1 from pending to complete.

Slice 1G and final Milestone 1 acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_diagnostics.py tests/integration/test_milestone1_gate.py tests/test_cli.py`
  — passed (`26 passed in 2.38s`). Coverage includes standard/deep check
  composition, stable JSON and human contracts, configuration-directory command
  resolution, occupied ports, missing database files, explicit future-check
  skips, write-probe cleanup, secret redaction, and the idle, active,
  interrupted, stale-owner mismatch, corrupt, safe-terminal, and
  recovery-required status matrix.
- The real-process gate spawned a holder that atomically recorded an operation
  and lock owner, killed it abruptly, and proved that `status` returned exit
  `60` with matching stale-owner and unfinished-operation evidence. Every state
  file remained byte-for-byte unchanged and the injected secret appeared in no
  terminal, JSON, manifest, event, or lock output.
- `uv run pytest -q` — passed with required loopback and Docker access
  (`243 passed in 53.10s`), preserving all real SQLite/HTTP, Docker Compose,
  multiprocess locking, state interruption, subprocess-tree cleanup, and prior
  milestone coverage.
- `uv lock --check && uv sync --locked --check` — passed; 32 packages resolved,
  31 checked, and the locked environment required no changes.
- `uv run ruff check .` — passed (`All checks passed!`).
- `uv run ruff format --check .` — passed (`32 files already formatted`).
- `uv run mypy dploydb` — passed
  (`Success: no issues found in 11 source files`).
- `uv run mypy demo` — passed
  (`Success: no issues found in 6 source files`).
- `uv build` — passed; rebuilt `dist/dploydb-0.1.0.tar.gz` and
  `dist/dploydb-0.1.0-py3-none-any.whl`.
- `uv run python scripts/verify_pipx_install.py` — passed; the isolated install
  exposed the CLI with `doctor` and `status`.
- Manual temporary configuration flow — `init` created a mode-`0600` starter;
  standard `doctor` reported 14 passed and five explicitly skipped checks;
  read-only JSON `status` reported `idle`; and deep `doctor` reported 20 passed
  and five skipped checks after supplying the demo Compose fixture environment.
  An initial deep run without those required Compose variables exited `40` and
  identified `compose_service`, proving that missing Compose interpolation is
  not silently accepted. The successful deep run verified Docker 29.4.0,
  Compose v5.1.2, daemon access, the configured `app` service, cleaned-up write
  probes, and disk-space evidence.

Milestone 1 is complete. The following section records the independently gated
Milestone 2 implementation and evidence.

#### Milestone 2 implementation scope

Planned on 2026-07-18:

- **Owned modules:** `dploydb/sqlite_checks.py`, `backup.py`, `restore.py`,
  `storage/base.py`, `storage/local.py`, the backup models in `models.py`, and
  the bounded Milestone 2 additions to `diagnostics.py` and `cli.py`.
- **Slice boundary:** complete 2A SQLite verification, 2B verified local backup,
  2C public backup/verify orchestration, and 2D the internal stopped-application
  restore engine in order. A later slice starts only after focused and full
  validation for the earlier slice pass.
- **CLI boundary:** add local `backup` and backup-ID `verify`. Defer public
  release restore to Milestone 6 and `--upload`/S3 behavior to Milestone 7.
- **Safety boundary:** live snapshots use only SQLite's online backup API;
  metadata is published last as the success marker; restore verifies both the
  selected backup and a pre-restore snapshot before production replacement.
- **Evidence boundary:** every mutating backup/restore workflow uses the
  durable deployment lock and operation trail. Read-only verification does not
  create competing in-progress state.

Progress tracker:

- [x] Milestone 2 overall — `COMPLETE` on 2026-07-18 after slices 2A through
  2D and the final gate passed.
  - [x] 2A — bounded read-only SQLite verification and doctor integration
    (`COMPLETE` on 2026-07-18).
  - [x] 2B — immutable verified local backup engine and storage contracts
    (`COMPLETE` on 2026-07-18).
  - [x] 2C — `backup` and `verify` CLI orchestration (`COMPLETE` on
    2026-07-18).
  - [x] 2D — safe internal restore and the Milestone 2 integration gate
    (`COMPLETE` on 2026-07-18).

Tracking rule: mark a slice complete only after its focused tests, full suite,
Ruff, formatting, and mypy pass; then record exact commands and observed
results below this section before starting the next slice.

Slice 2A acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_sqlite_checks.py tests/unit/test_diagnostics.py`
  — passed (`23 passed in 2.11s`).
- `uv run pytest -q` — passed (`252 passed in 53.36s`), preserving the real
  SQLite/HTTP and Docker Compose integration coverage from earlier milestones.
- `uv run ruff check dploydb tests/unit/test_sqlite_checks.py
  tests/unit/test_diagnostics.py` — passed (`All checks passed!`).
- `uv run ruff format --check dploydb tests/unit/test_sqlite_checks.py
  tests/unit/test_diagnostics.py` — passed (`14 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 12 source files`).
- Coverage proves read-only standard and deep checks, malformed/missing/directory/
  symlink rejection, foreign-key failure, lock timeout, progress-handler timeout,
  unchanged database bytes, and `doctor` evidence with only later milestone
  checks still skipped.

Slice 2B acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_backup.py
  tests/integration/test_backup_engine.py tests/unit/test_sqlite_checks.py` —
  passed (`17 passed in 0.17s`).
- `uv run pytest -q` — passed (`260 passed in 53.74s`).
- `uv run ruff check dploydb tests/unit/test_backup.py
  tests/integration/test_backup_engine.py` — passed (`All checks passed!`).
- `uv run ruff format --check dploydb` — passed (`16 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 16 source files`).
- A real WAL-mode writer committed continuously during `sqlite3.Connection.backup`;
  the resulting immutable artifact reopened, contained committed rows, passed
  SQLite checks, and matched its recorded SHA-256. Focused tests also proved
  checksum corruption, metadata tampering, symlink/path traversal, unsafe
  directory modes, and injected metadata-publication failure are rejected
  without a committed success marker.

Slice 2C acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/integration/test_backup_cli.py
  tests/unit/test_backup.py tests/test_cli.py` — passed (`26 passed in 0.29s`).
- `uv run pytest -q` — passed (`268 passed in 53.27s`).
- `uv run ruff check dploydb tests/integration/test_backup_cli.py
  tests/test_cli.py` — passed (`All checks passed!`).
- `uv run ruff format --check dploydb tests/integration/test_backup_cli.py` —
  passed (`17 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 16 source files`).
- Human and stable JSON `backup`/`verify` flows passed. Evidence proves an
  exclusive lock, terminal `snapshot_verified` operation state, read-only
  verification with byte-identical state, durable failed-safe database/storage
  failures, contention and interrupted-state refusal, corruption and unknown-ID
  rejection, and cross-sink secret redaction.

Slice 2D and final Milestone 2 acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/integration/test_demo_runtime.py
  tests/unit/test_restore.py` — passed (`13 passed in 53.67s`).
- The final targeted real-demo flow — `uv run pytest -q
  tests/integration/test_demo_runtime.py::test_stopped_demo_application_restores_verified_backup_end_to_end`
  — passed (`1 passed in 16.06s`). It started the real HTTP demo, wrote data,
  stopped it, ran the public backup and verify CLI paths, wrote later data,
  stopped it again, restored internally, restarted healthy, and proved both the
  selected rows and preserved pre-restore rows.
- Restore fault injection proved pre-commit failures leave the current database
  unchanged, post-commit failures restore and verify the previous database,
  rollback failures become durable `recovery_required`, regular stopped-state
  WAL/SHM sidecars are handled, and an unstopped application is refused.
- Final `uv run pytest -q` — passed (`275 passed in 69.60s`), including real
  SQLite/HTTP and Docker Compose coverage.
- Final `uv run ruff check .` — passed (`All checks passed!`).
- Final `uv run ruff format --check .` — passed (`43 files already formatted`).
- Final `uv run mypy dploydb` and `uv run mypy demo` — passed (`17` package
  source files and `6` demo source files with no issues).
- `uv lock --check && uv sync --locked --check` — passed; 32 packages resolved,
  31 checked, and the locked environment required no changes.
- `uv build` — passed; rebuilt `dist/dploydb-0.1.0.tar.gz` and
  `dist/dploydb-0.1.0-py3-none-any.whl`.
- `uv run python scripts/verify_pipx_install.py` — passed; the isolated install
  exposes the Milestone 2 CLI.
- `dploydb backup --help`, `dploydb verify --help`, console `--version`, and
  module `version` all passed; both version paths printed `dploydb 0.1.0`.

Milestone 2 is complete. Milestone 3 is the next allowed work; candidate
orchestration, production cutover, application rollback, public release restore,
crash recovery, remote storage, and retention remain intentionally unclaimed.

#### Milestone 3 implementation scope

Planned on 2026-07-18:

- **Owned modules:** a new typed `dploydb/migration.py` rehearsal service, the
  narrowly required rehearsal result contract in `models.py`, and focused unit,
  integration, timeout, cleanup, and redaction tests. Existing backup,
  subprocess, locking, and state interfaces will be reused rather than replaced.
- **Slice boundary:** complete 3A's context-managed disposable rehearsal engine
  before 3B adds the exclusive lock, verified rehearsal backup, durable state
  transitions, and the cross-cutting Milestone 3 gate.
- **CLI boundary:** do not expose an incomplete `deploy --version` command or a
  new public rehearsal command. The configured migration command is currently
  static and there is no trustworthy version-to-release resolver; Milestone 3
  remains an internal deployment stage that Milestone 4 can consume directly.
- **Workspace boundary:** materialize only a reverified immutable snapshot into
  a private operation workspace, override only the configured database-path
  environment variable, run from the configuration directory, and remove the
  disposable database and SQLite sidecars idempotently after its consumer exits.
- **Evidence boundary:** use the bounded process-group runner and persist its
  redacted command outcome, exit code, duration, timeout/cleanup facts, stdout,
  and stderr in the append-only operation log. A truncated stream is an evidence
  failure and cannot produce `rehearsal_passed`, so every accepted rehearsal has
  complete captured output within the established bound.
- **Safety boundary:** production is read only through preflight and SQLite's
  online backup API. Migration non-zero exit, timeout, cancellation, output
  truncation, post-migration SQLite failure, or workspace cleanup failure must
  become `failed_safe` with `production_changed=false` and an exact next action.

Progress tracker:

- [x] Milestone 3 overall — `COMPLETE` on 2026-07-18 after slices 3A and 3B
  and the final production-unchanged integration gate passed.
  - [x] 3A — disposable rehearsal workspace, migration execution, complete
    bounded output evidence, post-migration verification, and cleanup
    (`COMPLETE` on 2026-07-18).
  - [x] 3B — verified rehearsal snapshot orchestration, durable lock/state
    evidence, stable failures, and the Milestone 3 integration gate
    (`COMPLETE` on 2026-07-18).

Slice 3A gate:

- A verified backup is revalidated, copied to a private disposable path, and
  migrated only through the configured database environment variable.
- Success yields a checked rehearsed database while the context is active;
  non-zero exit, timeout, cancellation, truncated output, and post-migration
  SQLite failure are typed failures with redacted evidence.
- The process group is gone and the workspace is removed after success and every
  expected failure; cleanup failure is reported instead of hidden.

Slice 3A acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_migration.py` — passed (`7 passed in
  0.59s`). Coverage proves exact rehearsal-path injection, a checked migrated
  database available only inside the context, byte-identical production and
  immutable snapshot files, non-zero output evidence, bounded timeout cleanup,
  pre-cancellation, truncation refusal, post-migration foreign-key rejection,
  and surfaced workspace-cleanup failure.
- `uv run pytest -q` — passed (`282 passed in 69.54s`), preserving all real
  SQLite/HTTP, Docker Compose, backup/restore, locking, interruption, and
  process-tree coverage from Milestones 0 through 2.
- `uv run ruff check dploydb/migration.py dploydb/models.py
  tests/unit/test_migration.py` — passed (`All checks passed!`).
- `uv run ruff format --check .` — passed (`45 files already formatted`).
- `uv run mypy dploydb` — passed (`Success: no issues found in 18 source
  files`).

Slice 3A is complete. Slice 3B is the next allowed work; no public deployment,
candidate application, production cutover, or release-history behavior is
claimed by 3A.

Slice 3B and final Milestone 3 gate:

- The operation lock and clean-state checks run before work; durable transitions
  record `preflight_passed`, `snapshot_verified`, and `rehearsal_passed`, or a
  terminal `failed_safe`/`recovery_required` result with the required safety facts.
- Real demo migrations prove working v2 passes, broken migration fails, a hung
  migration is terminated, and an exit-zero migration that leaves invalid
  database state is rejected.
- Production file checksum, schema, `user_version`, and rows are identical before
  and after every failed rehearsal gate; stored state and logs contain no secret.
- Focused tests, the full suite, Ruff, format check, mypy for package and demo,
  packaging, isolated install verification, and a direct real-demo rehearsal flow
  pass before the milestone and both slices are marked complete.

Slice 3B and final Milestone 3 acceptance evidence observed on 2026-07-18:

- `uv run pytest -q tests/unit/test_migration.py
  tests/integration/test_migration_rehearsal.py` — passed (`13 passed in
  2.01s`). The real-process integration gate applied the actual demo v2 and
  broken migrations to disposable copies, terminated a timed-out parent and
  descendant, rejected an exit-zero foreign-key violation, blocked lock and
  unfinished-state conflicts before backup, and scanned all returned and
  persisted evidence for an interpolated secret.
- The gate compared production SHA-256, file size, complete `sqlite_schema`,
  `PRAGMA user_version`, and application rows before and after broken, timed-out,
  and post-check-failing rehearsals; every comparison was identical. Successful
  rehearsal also left production at v1 while its disposable result reached v2.
- Durable evidence showed `created -> preflight_passed -> snapshot_verified ->
  rehearsal_passed` on success. Command outcomes were appended while the
  operation remained at `snapshot_verified`; failures then ended at
  `failed_safe` with `production_changed=false`, an exact log path, and the next
  safe action. Verified rehearsal backups remained immutable and normal
  disposable workspaces were empty after every outcome.
- `uv run pytest -q
  tests/integration/test_migration_rehearsal.py::test_real_v2_migration_passes_on_copy_with_durable_evidence`
  — passed (`1 passed in 0.26s`) as the direct deterministic real-demo rehearsal
  flow. Milestone 3 intentionally has no incomplete public deployment command.
- Final `uv run pytest -q` — passed (`288 passed in 70.76s`), including real
  SQLite/HTTP, Docker Compose, online backup, restore rollback, OS locking,
  interruption, process-tree cleanup, and all Milestone 3 paths.
- Final `uv run ruff check .` — passed (`All checks passed!`); `uv run ruff
  format --check .` — passed (`46 files already formatted`).
- Final `uv run mypy dploydb` and `uv run mypy demo` — passed (`18` package
  source files and `6` demo source files with no issues).
- `uv lock --check` and `uv sync --locked --check` — passed; 32 packages were
  resolved, 31 checked, and the locked environment required no changes.
- `uv build` — passed; rebuilt `dist/dploydb-0.1.0.tar.gz` and
  `dist/dploydb-0.1.0-py3-none-any.whl` with the rehearsal module included.
- `uv run python scripts/verify_pipx_install.py` — passed; the isolated install
  still exposes the supported CLI. `uv run dploydb --help` and `uv run dploydb
  --version` passed, with version `0.1.0` and no misleading partial `deploy`
  command.

Milestone 3 is complete. Milestone 4 is the next allowed work. Candidate
application validation, production cutover, rollback, release history, public
restore/recovery, remote storage, and retention remain intentionally unclaimed.

#### Milestone 4 implementation scope

Planned on 2026-07-18:

- **Execution decision:** implement Milestone 4 as three independently gated
  slices, not as one large change. Docker resource isolation, readiness/smoke
  semantics, and durable orchestration have different failure and cleanup
  contracts and must be proven separately before the final gate.
- **Owned modules:** new typed `dploydb/runners/base.py` and
  `dploydb/runners/docker_compose.py`, a new `dploydb/health.py`, a new
  candidate coordinator in `dploydb/candidate.py`, narrowly required candidate
  evidence models, minimal application-configuration additions, and focused
  unit, integration, cleanup, timeout, continuity, and redaction tests. The
  existing `migration.py` may receive only the small refactor needed to let the
  candidate run while the checked rehearsal context is still alive.
- **CLI boundary:** do not expose a partial `deploy --version` command in this
  milestone. A successful Milestone 4 operation proves only that the candidate
  is safe to proceed to cutover; the public deployment command belongs to the
  complete Milestone 5 workflow.
- **Runner boundary:** use a unique, operation-derived Docker Compose project
  and candidate container; start only the configured service with no
  dependencies, publish only the configured loopback candidate port, mount only
  the disposable rehearsal directory at the configured database volume target,
  and inject the database path plus test-mode values without editing the user's
  Compose file. Pass the requested application version to Compose through the
  reserved `DPLOYDB_VERSION` interpolation value rather than inventing an image
  registry or release resolver.
- **Isolation boundary:** inspect the created container before readiness checks
  and reject a candidate whose mounts expose the production database or whose
  port binding is not the configured loopback port. The runner must never issue
  stop, recreate, or down commands against the current application's Compose
  project.
- **Health boundary:** use HTTPX for real bounded loopback requests, a monotonic
  overall startup deadline, bounded per-request timeouts and response capture,
  retry evidence, and an optional smoke command executed through the existing
  process-group runner. A successful but truncated smoke-command capture is not
  sufficient passing evidence.
- **Cleanup boundary:** collect bounded redacted Compose/application logs before
  cleanup, then remove the candidate container and isolated Compose resources
  idempotently on success, failure, timeout, cancellation, and interruption.
  If cleanup cannot prove that the candidate is gone, preserve the evidence and
  end in `recovery_required`; never hide the cleanup failure behind the primary
  health or startup failure.
- **State boundary:** one locked operation owns snapshot, migration, candidate,
  and cleanup evidence. It transitions through `preflight_passed`,
  `snapshot_verified`, `rehearsal_passed`, and terminal `candidate_healthy` only
  after candidate checks and both candidate/rehearsal cleanup succeed. Expected
  rejection ends `failed_safe` with `production_changed=false`; contradictory
  resource state or unproven cleanup ends `recovery_required`.

Progress tracker:

- [x] Milestone 4 overall — `COMPLETE` on 2026-07-19 after slices 4A through 4C
  and the final real-Docker continuity/cleanup gate passed.
  - [x] 4A — isolated Docker Compose candidate runner and lifecycle evidence
    (`COMPLETE` on 2026-07-18).
  - [x] 4B — bounded HTTP readiness, optional smoke command, and health evidence
    (`COMPLETE` on 2026-07-19).
  - [x] 4C — durable rehearsal-plus-candidate orchestration and Milestone 4 gate
    (`COMPLETE` on 2026-07-19).

Tracking rule: complete each slice only after its focused tests, the full suite,
Ruff, formatting, and mypy pass and exact evidence is recorded here. Do not begin
4B until 4A's cleanup gate passes, and do not begin 4C until both lower-level
contracts are stable.

##### 4A — Isolated Docker Compose runner

Status on 2026-07-18: `COMPLETE`. This slice owns new
`dploydb/runners/base.py` and `dploydb/runners/docker_compose.py`, the minimal
candidate container-port/database-volume-target additions in `dploydb/config.py`,
the shipped demo configuration/Compose interpolation needed to exercise the
runner, and focused unit plus real-Docker lifecycle tests. It reuses the existing
bounded subprocess/redaction boundary and does not implement HTTP readiness,
smoke checks, durable candidate orchestration, or a public deployment command.

- Add the smallest typed candidate lifecycle interface needed now: start,
  inspect, collect logs, stop, and prove cleanup. Milestone 5 will extend the
  application-runner contract for current/new production application control.
- Add explicit candidate container-port and database-volume-target configuration
  with backward-compatible documented defaults for the shipped configuration.
- Build every Docker/Compose invocation as an argument array with a mandatory
  timeout and the existing redaction boundary. Derive resource names from the
  project and operation ID, never from unchecked version text. Validate the
  requested version as a bounded release identifier before exposing it through
  `DPLOYDB_VERSION`, so path separators, traversal, NULs, and option-like values
  cannot reach Compose interpolation.
- Start the configured service as an isolated one-off Compose candidate with
  `--no-deps`, an explicit loopback port mapping, the rehearsal workspace mount,
  the configured database environment variable, test-mode environment, and
  `DPLOYDB_VERSION` available for Compose interpolation.
- Inspect actual mounts, port bindings, project labels, and running state before
  handing the candidate to the health layer. Preserve bounded redacted command
  and application-log evidence.

Slice 4A gate:

- Unit tests prove exact command/environment construction, safe resource names,
  no shell execution, timeouts, redaction, version handling, invalid inspection
  rejection, bounded logs, idempotent cleanup, and surfaced cleanup failures.
- A real Docker Compose test starts an isolated candidate against a disposable
  SQLite directory, proves its inspected mount and loopback binding, and proves
  no candidate container or isolated network remains after normal and failed
  startup paths.

Slice 4A acceptance evidence observed on 2026-07-18:

- `.venv/bin/python -m pytest -q tests/unit/test_docker_compose_runner.py
  tests/integration/test_candidate_runner.py` — passed (`29 passed in 4.71s`).
  Unit coverage proves exact argument-array/environment construction, mandatory
  timeouts, project/operation-derived resource names, bounded release validation,
  reserved `DPLOYDB_VERSION` handling, hard-link/workspace alias rejection before
  Compose execution, real subprocess redaction, live-inspection rejection for
  stopped/wrong-label/wildcard-port/extra-port/wrong-mount and production-exposing
  states, bounded logs, exact-name idempotent cleanup, startup timeout, and both
  proven and unproven cleanup outcomes.
- The real Docker gate started v2 against a disposable migrated SQLite database,
  inspected one writable bind from the rehearsal directory to `/data`, inspected
  exactly one `127.0.0.1` candidate-port binding, observed real HTTP v2 health,
  and kept the production database byte-identical at schema version 1. Cleanup
  passed twice, proving idempotency.
- The failed-start Docker gate held the candidate port with a different isolated
  candidate project, observed the second Compose start fail, and proved its
  operation-labeled container and isolated Compose network were absent before
  the blocker was also cleaned up.
- `.venv/bin/python -m pytest -q tests/integration/test_candidate_runner.py
  tests/integration/test_demo_docker.py` — passed (`5 passed in 15.69s`), proving
  the `DPLOYDB_VERSION` fixture change preserves every existing deterministic
  Docker demo flow.
- Final `.venv/bin/python -m pytest -q` — passed (`328 passed in 74.82s`),
  preserving all real SQLite/HTTP, Docker Compose, backup/restore, OS-locking,
  interruption, process-tree, and migration-rehearsal gates.
- `.venv/bin/ruff check .` — passed (`All checks passed!`);
  `.venv/bin/ruff format --check .` — passed (`51 files already formatted`).
- `.venv/bin/mypy dploydb` and `.venv/bin/mypy demo` — passed with no issues in
  `21` package source files and `6` demo source files.
- `uv lock --check` and `uv sync --locked --check` — passed; 32 packages were
  resolved, 31 checked, and the environment required no changes. `uv build`
  rebuilt both distribution artifacts, and
  `uv run python scripts/verify_pipx_install.py` passed. Wheel inspection found
  `dploydb/runners/base.py` and `dploydb/runners/docker_compose.py` in the built
  package.
- `.venv/bin/dploydb --help`, `.venv/bin/dploydb --version`, and
  `.venv/bin/python -m dploydb version` passed. The CLI remains version `0.1.0`
  and intentionally exposes no incomplete `deploy` command.
- Final read-only Docker audits returned no `io.dploydb.role=candidate`
  containers and no network with the Milestone 4A project-name prefix.

Slice 4A is complete. Slice 4B is the next allowed work; HTTP readiness, smoke
commands, durable candidate orchestration, production cutover, and rollback
remain intentionally unclaimed.

##### 4B — HTTP readiness and optional smoke checks

Status on 2026-07-19: `COMPLETE`. This slice owns a new typed
`dploydb/health.py`, the HTTPX runtime dependency, and focused health/smoke
tests. It uses the existing validated loopback URL, secret registry, and
bounded process-group runner. It does not own durable state, Docker lifecycle,
production cutover, or a public deployment command. Milestone 4A was audited
before this work and its runner, real-Docker isolation checks, configuration
contract, and cleanup gate remain complete with no repair identified.

Planned 4B evidence:

- Deterministic HTTPX-transport and real-loopback tests for delayed readiness,
  refusal, unhealthy responses, redirects without following them, bounded
  oversized bodies, the monotonic overall deadline, cancellation, and secret
  redaction.
- Real bounded subprocess tests for smoke success, non-zero exit, start failure,
  timeout plus descendant cleanup, complete-capture refusal, and proof that the
  smoke command cannot run before readiness succeeds.
- Focused tests followed by the full suite, Ruff, formatting, and mypy before
  4C begins.

- Add a typed health checker with injected HTTPX client/transport and monotonic
  clock boundaries, bounded retry cadence, an overall startup deadline, and
  bounded response evidence. Accept only a real HTTP 2xx response from the
  configured loopback URL; the response body is diagnostic evidence rather than
  an undocumented JSON contract. A running container or Compose health label
  alone is not passing evidence.
- Retry connection failures and unhealthy HTTP responses only until the fixed
  deadline, record the last safe reason and attempt count, and never follow an
  untrusted redirect away from the validated loopback health URL.
- Run the optional smoke command only after readiness passes, using the existing
  process-group runner, candidate URL/version/database environment, mandatory
  timeout, complete bounded capture, and redaction.

Slice 4B gate:

- Real loopback and deterministic-transport tests cover delayed readiness,
  connection refusal, HTTP 500/503, redirect, bounded oversized-response
  evidence, deadline expiry, cancellation, and secret redaction.
- Smoke tests cover success, non-zero exit, start failure, timeout, descendant
  cleanup, output truncation, and proof that smoke never runs before readiness.

Slice 4B acceptance evidence observed on 2026-07-19:

- `uv run pytest -q tests/unit/test_health.py` — passed (`11 passed in 3.51s`).
  Coverage includes a real loopback server progressing through HTTP 500 and 503
  to 204, real connection refusal, deterministic redirect refusal without an
  external request, a fixed monotonic deadline, cancellation before the first
  request, bounded oversized-body evidence, and response/transport secret
  redaction.
- Smoke coverage used the real bounded process-group runner for successful
  environment injection, non-zero exit, executable start failure, complete-
  capture refusal, timeout, and a spawned descendant. The timed-out parent and
  child were both gone before the result returned, and a failing readiness check
  proved the smoke executor was never invoked.
- `uv run pytest -q` — passed (`339 passed in 79.41s`), preserving all existing
  real SQLite/HTTP, Docker Compose, backup/restore, locking, interruption,
  process-tree, migration-rehearsal, and candidate-runner coverage.
- `uv run ruff check .` and `uv run ruff format --check .` — passed (`All checks
  passed!`; `53 files already formatted`).
- `uv run mypy dploydb` and `uv run mypy demo` — passed with no issues in `22`
  package source files and `6` demo source files.
- `uv lock --check` and `uv sync --locked --check` — passed with `38` resolved
  packages and `37` checked packages; the locked environment required no
  changes. HTTPX 0.28.1 is now an explicit runtime dependency.

Slice 4B is complete. Slice 4C is the next allowed work; no durable candidate
operation, production cutover, traffic mutation, or rollback is claimed by 4B.

##### 4C — Durable candidate validation and final Milestone 4 gate

Status on 2026-07-19: `COMPLETE`. This slice owns a new typed
`dploydb/candidate.py`, the minimal shared evidence contracts/refactor needed to
keep the verified migrated database alive during candidate checks, and focused
unit plus real-Docker continuity/cleanup tests. One deployment lock and one
durable operation covers preflight, verified snapshot, migration,
inspection, health/smoke, bounded logs, candidate cleanup, and rehearsal
cleanup. No production file, current-application Compose project, or traffic
hook will be mutated.

- Compose the existing verified snapshot and migration-rehearsal primitives with
  the runner and health checker while the rehearsed database context is alive.
  Avoid duplicating backup or migration safety logic and keep production
  read-only throughout this milestone.
- Persist redacted startup, inspection, readiness, smoke, application-log, and
  cleanup evidence before the terminal operation transition. Preserve the exact
  events log path in every stable failure.
- Treat candidate startup/health/smoke rejection with proven cleanup as
  `failed_safe`. Treat unproven container/process/resource cleanup or
  contradictory inspection as `recovery_required`, even though production was
  not changed.

Slice 4C and final Milestone 4 gate:

- Working v2 passes migration, starts against the rehearsed database, passes
  readiness and optional smoke checks, cleans up, and ends at
  `candidate_healthy` with production byte/schema/row evidence unchanged.
- Broken-health, candidate-startup, occupied-port, smoke-failure, timeout, and
  cleanup-failure scenarios have stable durable outcomes and preserve redacted
  evidence.
- During successful and rejected candidate checks, repeated real HTTP requests
  prove the existing v1 application continues serving its production database.
- After every proven-clean outcome, Docker inspection finds no candidate
  container or isolated network and the private rehearsal workspace is empty.
- Run focused tests, the full suite, Ruff, format check, mypy for package and
  demo, packaging, isolated install verification, and a direct real-demo
  candidate flow before marking Milestone 4 complete.

Slice 4C and final Milestone 4 acceptance evidence observed on 2026-07-19:

- `uv run pytest -q tests/unit/test_candidate.py
  tests/integration/test_candidate_validation.py
  tests/integration/test_candidate_runner.py tests/unit/test_health.py` — passed
  (`25 passed in 21.64s`). The unit gate proves one lock/operation owns snapshot,
  migration, candidate checks, logs, and cleanup; startup/readiness/smoke
  rejection with proven cleanup ends `failed_safe`; contradictory inspection,
  unproven candidate cleanup, and unproven rehearsal-workspace cleanup end
  `recovery_required`.
- The real-Docker gate ran working v2, deterministic broken health, an occupied
  candidate port, and a failing smoke command. Each outcome preserved the
  production file checksum, schema, `user_version`, and rows. Success ended at
  `candidate_healthy`; expected health/start/smoke rejection ended durably at
  `failed_safe` with the exact events path and redacted stage evidence.
- A real current v1 Compose application remained up during every successful and
  rejected candidate scenario. A concurrent monitor repeatedly received v1
  health and read the pre-existing visible SQLite row without one failed request
  while the isolated candidate used the migrated rehearsal database.
- `uv run pytest -q
  tests/integration/test_candidate_validation.py::test_real_v2_candidate_passes_while_current_v1_serves_and_all_evidence_is_clean
  -vv` — passed (`1 passed in 3.40s`) as the direct deterministic real-demo
  candidate flow, including optional smoke checks and cross-sink secret scans.
- Candidate startup, inspection, readiness/smoke, bounded application logs, and
  cleanup proof are append-only events before the terminal manifest transition.
  Success is recorded only after both the candidate resources and private
  rehearsal workspace are proven cleaned. No production Compose, traffic, or
  cutover command exists in this milestone.
- Final `uv run pytest -q` — passed (`351 passed in 92.58s`), including all real
  SQLite/HTTP, Docker Compose, online backup/restore, locking, interruption,
  process-tree cleanup, migration rehearsal, runner isolation, and Milestone 4
  continuity paths.
- `uv run ruff check .` and `uv run ruff format --check .` — passed (`All checks
  passed!`; `56 files already formatted`). `uv run mypy dploydb` and `uv run
  mypy demo` passed with no issues in `23` package source files and `6` demo
  source files.
- `uv lock --check && uv sync --locked --check` passed with `38` resolved and
  `37` checked packages. `uv build` rebuilt the sdist and wheel, wheel inspection
  found both `dploydb/candidate.py` and `dploydb/health.py`, and `uv run python
  scripts/verify_pipx_install.py` passed.
- `uv run dploydb --help`, console `--version`, and module `version` passed; both
  version paths printed `dploydb 0.1.0`, and the CLI intentionally exposes no
  partial `deploy` command before Milestone 5.
- Final read-only Docker audits returned no container labeled
  `io.dploydb.role=candidate` and no network with the Milestone 4 project prefix.

Milestone 4 is complete. Milestone 5 is the next allowed work; production
migration, maintenance/traffic hooks, application cutover, and automatic
pre-traffic rollback remain intentionally unclaimed.

#### Milestone 5 implementation scope

Planned on 2026-07-19:

- **Execution decision:** implement Milestone 5 as six independently gated
  slices, not as one large change. This is the first milestone allowed to stop
  the current application, modify the production database, or change traffic.
  The command hooks, production Compose lifecycle, database transaction,
  rollback state machine, and public CLI must therefore earn separate gates.
- **Current-interface audit:** Milestones 2 through 4 already provide the
  verified online backup, disposable migration rehearsal, isolated candidate,
  bounded subprocess, HTTP health, durable operation, and exclusive lock
  primitives. The current application runner intentionally controls candidates
  only; the health configuration identifies only the candidate endpoint; the
  stopped-database restore wrapper owns its own lock and operation; and no
  durable release manifest exists yet. Milestone 5 will extend these boundaries
  rather than bypass or duplicate them.
- **Owned modules:** bounded changes to `dploydb/config.py`, `models.py`,
  `state.py`, `candidate.py`, `health.py`, `restore.py`, `cli.py`, and
  `runners/base.py`; production lifecycle additions under `dploydb/runners/`;
  new typed `dploydb/traffic.py`, `dploydb/releases.py`, and `dploydb/deploy.py`;
  plus focused unit, fault-injection, CLI, and real-Docker integration tests.
- **Configuration boundary:** add a deploy-only production Compose project,
  loopback production port/health URL, and a bounded traffic-hook timeout.
  Existing Milestone 0 through 4 configurations must continue to parse; a
  deployment must fail before mutation when the new production topology is not
  configured. Candidate and production ports must be distinct, and both health
  URLs must remain local HTTP endpoints whose ports match their configured
  bindings.
- **Application rollback boundary:** preserve the exact stopped previous
  container instead of assuming its version can be reconstructed. A first
  deployment discovers and validates the configured production Compose service;
  later deployments use the active release manifest's stored application
  handle. The new release runs in its own release-derived Compose project on the
  configured production port only after the previous container is proven
  stopped. The previous container is not deleted during this milestone.
- **Database rollback boundary:** create and verify the final backup only after
  application writers are proven stopped. Production migration uses the same
  configured argument-array command, environment contract, timeout, complete
  redacted capture, and SQLite checks as rehearsal. Extract a caller-owned,
  stopped-database restore primitive from the existing restore engine so the
  deployment coordinator can restore the final backup without taking a nested
  lock or creating a second operation.
- **Traffic boundary:** developer hooks are bounded argument arrays executed by
  the shared subprocess runner with complete redacted evidence. A successful
  maintenance-on hook is the stored proof that normal traffic remains blocked
  during cutover. `traffic_activated` is persisted immediately after the
  activation hook succeeds. No database rollback is permitted after that state.
- **State boundary:** one deployment lock, one operation event trail, and one
  atomically updated release manifest cover rehearsal, candidate validation,
  cutover, and rollback. Candidate validation must become a reusable
  caller-owned stage while its existing standalone Milestone 4 wrapper keeps the
  same behavior. Release manifests store requested/previous application
  identities, rehearsal and final backup IDs/checksums, health and hook evidence,
  the production-changed and traffic-activated facts, and the operation log path.
- **CLI boundary:** do not expose `dploydb deploy` until the automatic rollback
  gate passes. The final command is `deploy --version <version> [--json]
  [--non-interactive]`; non-interactive mode must never wait for input, and human
  and JSON results must carry the same safety facts.

Progress tracker:

- [x] Milestone 5 overall — controlled cutover and automatic pre-traffic
  application/database rollback (`COMPLETE` on 2026-07-19).
  - [x] 5A — production topology, durable release contracts, and a reusable
    caller-owned pre-cutover stage (`COMPLETE` on 2026-07-19).
  - [x] 5B — bounded command-based maintenance and traffic controller
    (`COMPLETE` on 2026-07-19).
  - [x] 5C — Docker Compose production application lifecycle and exact previous
    application preservation (`COMPLETE` on 2026-07-19).
  - [x] 5D — final backup, production migration, and caller-owned verified
    database restore transaction (`COMPLETE` on 2026-07-19).
  - [x] 5E — internal deployment coordinator and complete pre-traffic rollback
    fault matrix (`COMPLETE` on 2026-07-19).
  - [x] 5F — public deploy CLI, real-Docker integration gate, packaging, and
    recorded Milestone 5 evidence (`COMPLETE` on 2026-07-19).

Tracking rule: a later slice starts only after the earlier slice's focused
tests, full suite, Ruff, formatting, and mypy pass and exact evidence is recorded
here. The public deployment command remains absent until 5E proves rollback.

##### 5A — Production topology, release state, and reusable pre-cutover stage

Status on 2026-07-19: `COMPLETE`. This slice added no production mutation and
did not expose `dploydb deploy`.

- Extend configuration with backward-compatible optional production topology
  fields and a defaulted positive hook timeout. `deploy` requires the complete
  topology and rejects missing, contradictory, non-loopback, or colliding port
  settings before acquiring a mutation-capable stage.
- Add strict release/application-handle models and an atomic private release
  store. A release record may advance only through the required deployment
  states, terminal records are immutable, every update references the same
  operation, and malformed/contradictory state becomes `recovery_required`.
- Extract the checked rehearsal-plus-candidate work into a caller-owned stage
  that appends to an existing in-progress operation and returns only after
  candidate and rehearsal cleanup are proven. Keep
  `validate_configured_candidate` as the standalone locked Milestone 4 wrapper.
- Generalize the bounded health boundary only as needed to check a supplied,
  prevalidated production URL/database path without weakening redirect,
  deadline, smoke-command, complete-capture, cancellation, or redaction rules.

Slice 5A gate:

- Existing configurations and all Milestone 4 tests remain green; deploy-only
  validation fails side-effect-free when production topology is absent or
  unsafe.
- Atomic release tests cover legal/illegal transitions, permissions, redaction,
  interruption, terminal immutability, active/previous selection, and
  contradictory handles/backups/traffic facts.
- One locked test operation reaches `candidate_healthy` and remains in progress
  for cutover, while the existing standalone candidate operation still ends
  successfully at `candidate_healthy` with production unchanged.

Slice 5A acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_candidate.py
  tests/unit/test_health.py tests/unit/test_releases.py tests/test_config.py` —
  passed (`129 passed in 4.51s`). Configuration coverage proves complete local
  production topology, distinct production/candidate ports, partial-topology
  rejection, a defaulted hook timeout, pre-Milestone-5 configuration
  compatibility, and deploy-only validation with zero filesystem, database,
  subprocess, or socket access.
- The caller-owned candidate test created a real `deploy` operation, recorded
  its matching kernel-lock owner, ran a real SQLite snapshot and migration with
  injected application/health boundaries, proved candidate and rehearsal
  cleanup, and observed the operation still `in_progress` at
  `candidate_healthy`. The production checksum remained identical. The existing
  standalone wrapper still ended terminal `succeeded` at the same state.
- Release tests prove strict application handles, legal and illegal deployment
  transitions, terminal immutability, rehearsal/final backup pairing,
  application/release identity consistency, traffic/production invariants,
  mode-0700 directories, mode-0600 manifests, recursive redaction, atomic
  active/previous selection, malformed/truncated/wrong-mode rejection, and
  replacement-failure preservation of the prior complete manifest.
- `.venv/bin/python -m pytest -q
  tests/fault_injection/test_release_state_interruption.py
  tests/unit/test_releases.py` — passed (`12 passed in 0.30s`). Real child
  processes were killed immediately before manifest replacement and immediately
  after replacement but before directory sync. The durable manifest was always
  complete old or complete new state; an abandoned pre-replacement temporary
  file became explicit `recovery_required` evidence rather than being ignored.
- The generic application health boundary reused the existing fixed deadline,
  loopback HTTP, redirect refusal, bounded response, smoke timeout/process-group
  cleanup, complete capture, and redaction behavior. Its focused production-mode
  test used the supplied production URL/database and proved candidate test-mode
  environment was not injected; the candidate adapter preserved its public
  Milestone 4 contract.
- Final `.venv/bin/python -m pytest -q` — passed with required loopback and
  Docker access (`378 passed in 94.41s`), preserving every real SQLite/HTTP,
  Docker Compose, online backup/restore, locking, interruption, process-tree,
  rehearsal, and candidate-continuity gate.
- `.venv/bin/ruff check .` — passed (`All checks passed!`); `.venv/bin/ruff
  format --check .` — passed (`59 files already formatted`).
- `.venv/bin/mypy dploydb` and `.venv/bin/mypy demo` — passed with no issues in
  `24` package source files and `6` demo source files.
- `uv lock --check` and `uv sync --locked --check` — passed with `38` resolved
  and `37` checked packages; the locked environment required no changes.

Slice 5A is complete. Slice 5B is the next allowed work. No maintenance or
traffic hook has run, no production application lifecycle or database cutover
exists, and the public deployment command remains intentionally absent.

##### 5B — Command-based traffic controller

Status on 2026-07-19: `COMPLETE`. This slice owns `dploydb/traffic.py`, the
defaulted positive `traffic.timeout_seconds` configuration added in 5A, and
focused unit plus real-process hook tests. It does not stop applications, touch
SQLite, change durable deployment state, or expose a deployment command.

- Add a narrow `TrafficController` interface and command implementation for
  maintenance on/off and new/old target activation.
- Execute every hook as an argument array with its configured positive timeout,
  exact environment and working directory, process-group cleanup, bounded
  complete output, and redaction before evidence leaves the controller.
- Return typed evidence for every terminal outcome. Never infer that a hook's
  side effect occurred from a running process or hide a timeout, cancellation,
  truncated output, or cleanup failure.

Slice 5B gate:

- Unit and real-process tests cover exact commands, ordering, success, non-zero
  exit, missing executable, timeout/descendant cleanup, cancellation,
  truncation, secret output, and retry-safe evidence.
- A maintenance-on failure proves no application or database method was called;
  a maintenance-off or target-activation failure remains explicit for the later
  rollback state machine.

Slice 5B acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_traffic.py` — passed (`8 passed
  in 1.27s`). All four hooks used their exact configured argument arrays,
  positive timeout, exact caller environment, absolute working directory, and
  cancellation event through the shared no-shell subprocess boundary.
- Success requires exit zero plus complete stdout and stderr evidence. Non-zero
  exit, missing executable, timeout, pre-cancellation, cleanup failure metadata,
  or truncated success remains a typed `passed=false` result for the deployment
  coordinator; the controller never guesses that an external side effect
  occurred.
- The real timeout hook started a parent and descendant, exceeded its one-second
  bound, and returned only after both processes were gone. The real redaction
  hook printed a sensitive environment value and retained only `[REDACTED]`;
  the real oversized-output hook exited zero but was correctly refused as
  passing because complete evidence was unavailable.
- Final `.venv/bin/python -m pytest -q` — passed with required loopback and
  Docker access (`386 passed in 93.89s`), preserving every Milestone 0 through
  5A gate.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` — passed (`All
  checks passed!`; `61 files already formatted`). `.venv/bin/mypy dploydb` and
  `.venv/bin/mypy demo` passed with no issues in `25` package source files and
  `6` demo source files.

Slice 5B is complete. Slice 5C is the next allowed work. Hook execution is not
yet called by any public or production-mutating path.

##### 5C — Docker Compose production application lifecycle

Status on 2026-07-19: `COMPLETE`. This slice owns the production lifecycle
contracts in `dploydb/runners/base.py`, the new
`dploydb/runners/docker_compose_production.py`, strict durable production
application handles, and focused unit plus real-Docker rollback-lifecycle tests.
It does not run traffic hooks or mutate the production database.

- Add a separate typed production lifecycle rather than weakening candidate
  isolation: discover/inspect current, stop and prove stopped, start the new
  release against production, inspect it, collect logs, stop/remove the failed
  new release, restart the exact previous container, and prove its running state.
- Validate actual Compose project/service labels, container identity, production
  database bind, loopback production port, and running/stopped state before a
  transition is accepted. Production startup must not inject candidate test-mode
  values.
- Preserve the exact previous container while starting the new release in a
  release-derived Compose project. Cleanup targets only recorded container and
  project identities and is idempotent; ambiguous identity or unproven cleanup
  becomes `recovery_required`.

Slice 5C gate:

- Unit tests prove exact no-shell command/environment construction, timeouts,
  inspection rejection, bounded logs, exact-target cleanup, previous-container
  preservation, restart, idempotency, and secret redaction.
- A real-Docker lifecycle test stops a healthy v1 container, starts v2 against a
  disposable production database while v1 remains preserved, removes v2,
  restarts that exact v1 container, verifies health, and leaves no test release
  resources behind.

Slice 5C acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q
  tests/unit/test_docker_compose_production_runner.py` — passed (`19 passed in
  0.16s`). Coverage proves unique configured-service discovery, exact durable
  container identity, current-app stop without removal, stopped-state port
  configuration inspection, release-derived resource names, production database
  mount/environment injection, loopback production-port validation, DployDB
  operation/release/role labels, and removal of candidate URL/test-mode values
  from the production startup environment.
- Unit fault coverage rejects missing/multiple/unsafe discovery, wrong running
  state, project/service/release/operation identity, wrong database mount or
  environment, wildcard/wrong/additional ports, invalid release IDs, startup
  failure, bounded log truncation, and unproven cleanup. Exact-target cleanup is
  idempotent and never issues remove/down against the preserved previous
  container or project.
- `.venv/bin/python -m pytest -q
  tests/integration/test_production_runner.py -vv` — passed (`1 passed in
  3.95s`). The real Docker gate started healthy v1 with a visible SQLite row,
  discovered its full container identity, stopped but preserved it, migrated the
  stopped fixture to v2, started and inspected a separate production-release
  project on the same loopback production port, observed healthy v2, collected
  complete logs, removed v2 twice idempotently, restored the stopped v1 fixture,
  restarted the exact original container ID, and proved healthy v1 plus the
  preserved row.
- Final `.venv/bin/python -m pytest -q` — passed with required loopback and
  Docker access (`406 passed in 99.58s`), preserving every Milestone 0 through
  5B gate.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` — passed (`All
  checks passed!`; `64 files already formatted`). `.venv/bin/mypy dploydb` and
  `.venv/bin/mypy demo` passed with no issues in `26` package source files and
  `6` demo source files.
- Final read-only Docker audits found no container labeled
  `io.dploydb.role=production_release` and no network with the Milestone 5C
  production-release project prefix.

Slice 5C is complete. Slice 5D is the next allowed work. No final backup,
production migration, automatic database restore, coordinator, or public deploy
command exists yet.

##### 5D — Cutover database transaction

Status on 2026-07-19: `COMPLETE`. This slice owns the database mutation and
rollback primitives in `dploydb/cutover.py`, the caller-owned restore addition
to `dploydb/restore.py`, reusable migration command evidence, and their focused
tests. It does not invoke traffic hooks, stop or restart applications itself,
coordinate a deployment, or expose `dploydb deploy`.

- After caller-supplied proof that every managed database user is stopped,
  create a `FINAL` backup through the existing online backup API and reverify its
  checksum/SQLite evidence before migration may start.
- Run the configured migration directly against production with the rehearsed
  database environment variable, mandatory timeout, complete redacted command
  capture, and post-migration SQLite checks. Persist command evidence before
  accepting `production_migrated`.
- Extract and test a low-level verified restore transaction that stages the
  selected final backup, reverifies it, removes only safe SQLite sidecars while
  users are stopped, atomically replaces and fsyncs production, and verifies the
  resulting bytes and SQLite contents. The existing restore wrapper continues
  to provide its own lock, operation, and pre-restore backup behavior.
- Require an explicit `traffic_activated=false` guard for automatic database
  restore; the primitive refuses post-traffic rollback.

Slice 5D gate:

- Tests cover final-backup failure, production migration non-zero exit, timeout,
  cancellation, truncated evidence, post-migration SQLite failure, restore
  staging/replacement/fsync failures, unsafe sidecars, checksum mismatch, and
  the post-traffic rollback guard.
- Forced migration and post-check failures restore the final backup byte for
  byte when rollback prerequisites hold; a restore that cannot be proven ends
  `recovery_required` with the application still stopped.

Slice 5D acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_cutover.py
  tests/unit/test_restore.py tests/unit/test_migration.py` — passed (`29 passed
  in 2.11s`). The cutover tests use real SQLite files and real bounded migration
  subprocesses. A `FINAL` snapshot is accepted only with complete stopped-app
  command and inspection proof, is tied to the active operation ID, and is
  reverified before either migration or rollback.
- The production migration matrix covers success, a partial migration followed
  by non-zero exit, a partial migration followed by timeout and process-group
  cleanup, started cancellation, complete-output truncation, durable evidence
  persistence failure, unproven process cleanup, and a successful command that
  leaves a foreign-key violation. Command evidence is emitted before the
  migration can be accepted, and post-command uncertainty carries conservative
  `production_changed` and `recovery_required` facts.
- Each real non-zero, timeout, truncation, and post-SQLite-check failure restored
  the operation-bound final backup through the caller-owned transaction. The
  restored production file matched the final SHA-256 byte for byte and returned
  to `user_version=1`, the original schema, and the original row.
- Restore fault coverage proves pre-replacement staging and atomic-replacement
  failures preserve the current database; post-replacement and directory-fsync
  failures become `recovery_required`; unsafe WAL/SHM entries are refused;
  tampered final-backup bytes are rejected; and automatic restore after the
  stored traffic-activation boundary is forbidden without touching production.
- Final `.venv/bin/python -m pytest -q` — passed with required loopback and
  Docker access (`422 passed in 99.68s`), preserving every Milestone 0 through
  5C gate.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` — passed (`All
  checks passed!`; `66 files already formatted`). `.venv/bin/mypy dploydb` and
  `.venv/bin/mypy demo` passed with no issues in `27` package source files and
  `6` demo source files.
- `uv lock --check` and `uv sync --locked --check` — passed with `38` resolved
  and `37` checked packages; the locked environment required no changes.

Slice 5D is complete. Slice 5E is the next allowed work. The safe database
transaction is not yet reachable from a coordinator or public command, and no
claim is made yet that application-plus-database rollback is complete.

##### 5E — Deployment coordinator and pre-traffic rollback matrix

Status on 2026-07-19: `COMPLETE`. The internal coordinator, complete injected
fault matrix, full loopback/Docker regression gate, and static checks pass.
The public CLI remains absent until slice 5F.

- Compose the existing preflight, verified rehearsal snapshot, migration
  rehearsal, candidate validation, traffic controller, production runner,
  final-backup/migration transaction, health checker, release store, and state
  store under one lock and operation.
- Follow the required order exactly: candidate cleanup; maintenance on; current
  application stop proof; final verified backup; production migration; new
  application start/inspection; final database, HTTP, and optional smoke checks;
  traffic activation; maintenance off; then durable `active`.
- Before traffic activation, a failure enters `rollback_started`, stops the new
  application if present, restores the final backup only if production may have
  changed, restarts the exact previous container, activates the old target,
  disables maintenance, checks the previous application/database, and reaches
  `rolled_back` only when every proof passes. A failure before production
  mutation still restores application/maintenance state without needlessly
  replacing the database.
- Any contradictory stage, unproven application state, failed database restore,
  failed old-target activation, failed maintenance cleanup, or unhealthy
  previous application ends `recovery_required` with exact manual instructions.
  After `traffic_activated`, never restore the old database automatically; keep
  the checked new application/database and escalate the remaining traffic or
  maintenance action.

Slice 5E gate:

- An injected failure at every cutover call boundary has a stable expected
  state and safety payload. Repeated cleanup is idempotent.
- Forced production-migration and final-health failures restore the final backup
  checksum/schema/rows, restart the exact previous application, reactivate its
  target, disable maintenance, pass previous health, and finish `rolled_back`.
- Traffic activation and post-activation maintenance failures obey the stored
  traffic fact and never perform an unsafe automatic database rollback.
- Every release manifest and operation event explains what ran, what changed,
  what was restored, which application is running, and the next safe action;
  cross-sink secret scans pass.

Slice 5E acceptance evidence observed on 2026-07-19:

- `dploydb/deploy.py` now composes the existing caller-owned candidate stage,
  exact production runner, command traffic controller, final-backup/migration
  transaction, application health checker, release store, operation store, and
  kernel-backed lock. Narrow injectable adapters live in
  `dploydb/deployment_dependencies.py`, and compact release/full-event evidence
  conversion lives in `dploydb/deployment_evidence.py`. The coordinator persists
  every required state from `created` through `active`, or through
  `rollback_started` to `rolled_back`/`recovery_required`.
- The coordinator stores the traffic point of no return in memory immediately
  after a passing activation hook and durably before continuing. A failed
  activation that may have run and every post-activation failure preserve the
  checked new database/application and forbid automatic database rollback.
  Only a start-failed or pre-start-cancelled activation has sufficient evidence
  to enter pre-traffic rollback.
- `.venv/bin/python -m pytest -q tests/unit/test_deploy.py` — passed (`23 passed
  in 1.71s`). The matrix covers a
  successful deployment, exact active-release reuse, candidate rejection,
  maintenance enable/cleanup, current stop, final backup, migration, new-app
  start/log/health, activation, post-activation maintenance, new-app cleanup,
  verified database restore, previous restart, old-target activation,
  maintenance cleanup, previous health, durable release-sink failure, and
  cross-sink secret redaction.
- Forced production-migration and final-health failures restored the recorded
  final backup to the original schema, `user_version=1`, and row set; removed a
  started new release when applicable; restarted the exact previous handle;
  activated the old target; disabled maintenance; passed previous SQLite and
  application health; and ended with release `rolled_back` plus operation
  `failed_safe` evidence.
- `.venv/bin/python -m pytest -q tests/unit/test_deploy.py
  tests/unit/test_candidate.py tests/unit/test_releases.py
  tests/unit/test_cutover.py tests/unit/test_traffic.py
  tests/unit/test_docker_compose_production_runner.py
  tests/fault_injection/test_release_state_interruption.py` — passed (`88 passed
  in 5.39s`).
- `.venv/bin/python -m pytest -q tests --ignore=tests/integration
  --ignore=tests/unit/test_health.py --ignore=tests/unit/test_diagnostics.py` —
  passed (`386 passed in 8.19s`). The omitted files are the known tests that
  require loopback sockets or Docker, not selected test failures.
- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff
  --check`, `.venv/bin/mypy dploydb`, and `.venv/bin/mypy demo` passed (`70`
  files formatted; no type errors in `30` package and `6` demo source files).
- Final `.venv/bin/python -m pytest -q` — passed with required loopback and
  Docker access (`445 passed in 100.26s`), preserving every Milestone 0 through
  5D gate while exercising the new coordinator and rollback matrix.

Slice 5E is complete. Slice 5F is the next allowed work. No public deployment
command is exposed until the real-Docker success and rollback flows below pass.

##### 5F — Public deploy CLI and final Milestone 5 gate

Status on 2026-07-19: `COMPLETE`. The public command, stable output contracts,
three real-Docker flows, continuous traffic-isolation proof, complete regression
suite, packaging/install checks, documentation, and final Docker audit pass.

- Add `dploydb deploy --version <version> [--json] [--non-interactive]` only
  after 5E passes. Render successful active and rolled-back outcomes plus all
  expected failures with stable machine-readable fields and no traceback.
- Run the deterministic real-Docker flows for successful v2, forced
  production-migration failure, and forced final-health failure. Use real
  SQLite rows/schema/checksums, real containers, real HTTP checks, and real
  command hooks; mocks remain limited to unit fault injection.
- Monitor the traffic-visible endpoint through cutover and prove the new release
  receives no normal request before final health passes and activation succeeds.

Final Milestone 5 gate:

- A successful release changes the expected application and schema, preserves
  existing rows, leaves a verified final backup, records the active release, and
  keeps the previous application/backup recoverable.
- Forced production-migration and final-health failures restore both database
  and application and leave the previous release healthy; final manifests and
  event logs contain the complete redacted trail.
- Focused tests, full `pytest`, Ruff check, Ruff format check, mypy for package
  and demo, build, isolated `pipx` verification, CLI help/JSON checks, a direct
  real-demo deployment flow, and final Docker resource audits all pass before
  Milestone 5 is marked complete.

Slice 5F and final Milestone 5 acceptance evidence observed on 2026-07-19:

- `dploydb deploy --version <version> [--json] [--non-interactive]` is public.
  Active and rolled-back terminal results share stable release/operation IDs,
  requested version, production/application/recovery facts, traffic activation,
  final backup ID/checksum, log path, and non-interactive mode. Expected
  `recovery_required` failures reuse the stable failure payload and exit `60`;
  rolled-back results preserve the original stable failure class, such as exit
  `40` for a production migration command failure and `50` for failed final
  health.
- `.venv/bin/python -m pytest -q tests/unit/test_deploy_cli.py
  tests/test_cli.py` — passed (`18 passed in 0.26s`). Coverage proves human and
  JSON active/rolled-back output, recovery-required JSON, required `--version`,
  stable exits, and that `--non-interactive` performs no terminal read.
- The deterministic demo gained a production-only health-failure fixture that
  passes the real isolated candidate under `DPLOYDB_TEST_MODE=1` and returns
  HTTP `503 fixture_final_production_health_failure` only in production. A
  separate real migration fault command commits a partial schema mutation only
  when its database target is the configured production file; rehearsal still
  runs the normal v2 migration. These fixtures reach the intended late cutover
  boundaries without mocking Docker, SQLite, hooks, subprocesses, or HTTP.
- `.venv/bin/python -m pytest -q tests/integration/test_deploy_end_to_end.py` —
  passed (`3 passed in 34.44s`). The public executable completed a healthy v2
  deployment, rolled back a partial production-migration failure, and rolled
  back a final-production-health failure after candidate health passed. Both
  failures restored `user_version=1`, the exact v1 columns and row set, restarted
  healthy v1, activated the old target, disabled maintenance, retained final
  backup evidence, and ended `rolled_back` without recovery uncertainty.
- A continuous real HTTP client sent normal `GET /notes` traffic through an
  atomic traffic-state proxy during each cutover. It observed old responses
  before maintenance, only maintenance responses while production was stopped,
  and no new-schema response unless the stored target was `new` and maintenance
  was already off. The successful path recorded exactly maintenance-on,
  activate-new, maintenance-off; rollback paths recorded maintenance-on,
  activate-old, maintenance-off.
- Final `.venv/bin/python -m pytest -q` passed with required loopback and Docker
  access (`456 passed in 144.01s`), including every Milestone 0 through 5 gate.
  `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff
  --check`, `.venv/bin/mypy dploydb`, and `.venv/bin/mypy demo` passed (`74`
  files formatted; no type errors in `30` package and `8` demo source files).
- `uv lock --check` and `uv sync --locked --check` passed with `38` resolved and
  `37` checked packages and no changes. `uv build` rebuilt the sdist and wheel;
  wheel inspection found the CLI, coordinator, dependency/evidence adapters,
  cutover, traffic, and release modules. `uv run python
  scripts/verify_pipx_install.py` passed in a fresh temporary isolated install.
- `.venv/bin/dploydb --help`, `.venv/bin/dploydb deploy --help`, console and
  module version commands passed. Deploy help exposes required `--version` plus
  `--json` and `--non-interactive`; a missing-config JSON probe exited `10` with
  the complete stable redacted failure payload and no traceback. `README.md`
  now documents the public cutover, automatic pre-traffic rollback, and the
  post-activation no-database-rollback boundary.
- Final read-only Docker audits returned no container labeled
  `io.dploydb.role=candidate` or `io.dploydb.role=production_release` and no
  DployDB network. The end-to-end gate also performed exact project-token
  cleanup and asserted no matching container remained after every scenario.

At this Milestone 5 checkpoint, Milestone 6 was the next allowed work;
off-server backup and retention remained intentionally unclaimed.

#### Milestone 6 implementation scope

Planned on 2026-07-19:

- **Owned modules:** release enumeration and reporting in `dploydb/releases.py`;
  durable crash intent in `dploydb/deploy.py` and the shared state contracts;
  read-only diagnosis and idempotent execution in a narrow
  `dploydb/recovery.py`; release-aware restore planning and execution in
  `dploydb/restore.py` plus a separate coordinator if needed; public command
  wiring in `dploydb/cli.py`; and focused unit, fault-injection, and real-Docker
  integration tests.
- **Restore boundary:** the public command accepts a release ID, not a raw
  backup ID. During the hackathon it restores only the immediately previous
  protected release. The preview must prove the selected application identity,
  the active release's final backup that represents the previous database, and
  every required path before confirmation. Older history remains readable but
  is not claimed restorable without protected evidence.
- **Crash boundary:** persist intent before starting production migration and
  before attempting new-traffic activation. Recovery treats a crash inside
  either side-effect window conservatively; uncertain traffic activation can
  never trigger automatic database rollback.
- **Mutation boundary:** release history, restore preview, and recovery
  diagnosis are read-only. `restore` and `recover` acquire the deployment lock,
  require explicit confirmation, store every transition, re-inspect live
  application state, and either prove a healthy terminal state or stop with
  exact manual instructions.
- **Gate boundary:** real crash tests cover interruption after maintenance,
  after the current application stops, and after production migration. A real
  manual restore proves the current state is backed up first and restores the
  protected previous application and database end to end.

Progress tracker:

- [x] 6A — Release history and read-only CLI contracts (`COMPLETE` on
  2026-07-19).
- [x] 6B — Durable crash markers and recovery diagnosis matrix (`COMPLETE` on
  2026-07-19).
- [x] 6C — Restore selection and non-mutating preview (`COMPLETE` on
  2026-07-19).
- [x] 6D — Controlled public manual restore with backup-first behavior
  (`COMPLETE` on 2026-07-19).
- [x] 6E — Idempotent `recover` execution and safe refusal (`COMPLETE` on
  2026-07-19).
- [x] 6F — Real crash/restore gate, full validation, docs, and packaging
  (`COMPLETE` on 2026-07-19).

Every slice passed its focused gate before the next began. The complete 6F
real-process, real-Docker, packaging, and regression gate is recorded below.

##### 6A — Release history and read-only CLI contracts

Status on 2026-07-19: `COMPLETE`. `ReleaseStore.read_history` validates the
private state/release directories, exact release directory contents, every
manifest, and active/previous pointers without creating state. Unknown entries,
incomplete releases, symlinks, malformed records, and pointer contradictions
become explicit recovery-required failures. `dploydb releases` and `dploydb
release show <release-id>` expose stable redacted human and JSON views; invalid
or absent user-selected IDs fail safely without a traceback.

Acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_releases.py
  tests/unit/test_release_cli.py tests/test_cli.py` — passed (`32 passed`).
- `.venv/bin/ruff check` passed for the changed implementation/tests after
  formatting, and `.venv/bin/mypy dploydb` passed with no issues in `30` source
  files.

Slice 6A is complete. Slice 6B is the next allowed work; no restore or recovery
mutation is public yet.

##### 6B — Durable crash markers and recovery diagnosis matrix

Status on 2026-07-19: `COMPLETE`. New releases declare recovery protocol 2.
Before production migration or new-traffic activation can run, the release and
operation stores now durably record a monotonic intent marker. An interruption
inside migration therefore conservatively permits only verified pre-traffic
restore; an interruption inside traffic activation forbids database rollback
unless durable command evidence proves the hook never started.

`dploydb/recovery.py` contains a pure decision matrix that reconciles the
operation, full event trail, release manifest, exact previous/new application
state, final-backup verification, and current/final database checksums. Plans
can recover the previous release, complete an already activated checked release,
report no action, or require manual action. The Docker production boundary now
supports read-only exact live inspection and deterministic proof that an
operation-derived release resource is absent.

Acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_recovery.py
  tests/unit/test_releases.py tests/unit/test_deploy.py
  tests/unit/test_docker_compose_production_runner.py` — passed (`69 passed`).
- The matrix covers interruption after maintenance, after current-app stop,
  after migration intent, during rollback, before traffic, an unresolved
  traffic attempt, durable activation success, and proven activation
  start-failure. It proves idempotent checksum-based restore skipping and legacy
  state refusal.
- `.venv/bin/ruff check dploydb ...` and `.venv/bin/mypy dploydb` passed with no
  issues in `31` source files.

Slice 6B is complete. Slice 6C is the next allowed work. Diagnosis remains
read-only; no public restore or recovery mutation is exposed yet.

##### 6C — Restore selection and non-mutating preview

Status on 2026-07-19: `COMPLETE`. The selector accepts a release ID and permits
only the immediately previous protected release. It validates active/previous
pointers and lineage, both exact application handles, and the active release's
operation-bound `FINAL` backup. This mapping is deliberate: the active
release's final backup represents the previous release's database at cutover;
the selected release's own final backup represents an even older state.

`dploydb restore <release-id>` without confirmation is read-only and renders a
specific data-loss warning, exact current/selected releases and containers,
the selected checksum, and the mandatory current-state backup promise. JSON
preview never prompts or mutates.

##### 6D — Controlled public manual restore with backup-first behavior

Status on 2026-07-19: `COMPLETE`. `dploydb restore <release-id> [--yes]` now
re-resolves the preview under the deployment lock, inspects exact current and
selected containers, enables maintenance, stops the current writer, creates a
verified `PRE_RESTORE` backup, restores and verifies the selected database,
restarts and checks the selected application, activates its target, disables
maintenance, and atomically swaps active/previous pointers. Human execution
requires confirmation unless `--yes` is supplied; JSON without `--yes` remains
a preview.

Failures before selected traffic activation restore the pre-restore database
and exact current application and finish `failed_safe` only after health proof.
Unproven application lifecycle, hook cleanup, or any failure after normal
traffic may be enabled becomes `recovery_required` and never automatically
restores an older/newer database across the traffic boundary.

Acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_restore.py
  tests/unit/test_manual_restore.py tests/unit/test_restore_cli.py
  tests/unit/test_releases.py tests/unit/test_deploy.py
  tests/unit/test_recovery.py` — passed (`64 passed`).
- Focused success uses real SQLite backup/restore bytes, proves the pre-restore
  backup contains the replaced current schema, restores the selected schema,
  swaps release pointers, and records `manual_restore_completed`. Fault tests
  prove safe pre-traffic rollback and post-traffic no-database-rollback.
- `.venv/bin/ruff check dploydb ...` and `.venv/bin/mypy dploydb` passed with no
  issues in `32` source files.

Slices 6C and 6D are complete. Slice 6E is next; public interrupted-deployment
recovery execution remains unclaimed.

##### 6E — Idempotent `recover` execution and safe refusal

Status on 2026-07-19: `COMPLETE`. `dploydb recover [--yes]` first produces a
read-only plan from durable and live evidence. JSON without `--yes` never
prompts or mutates; human execution requires confirmation. Manual-required
plans exit `60` with the standard safety payload and never execute an action.

Confirmed recovery acquires the deployment lock, regenerates the plan, marks
the interrupted source operation and release recovery-required, and creates a
separate recovery operation. It executes only the plan's ordered actions and
stores intent before each. Previous-release recovery can remove a known new
container, restore a reverified final backup, restart the exact previous
container, restore hooks, and verify SQLite/HTTP health. A durably successful
new-traffic hook instead keeps the new database, disables maintenance, verifies
the exact new application, and completes it active.

Recovery resolution preserves the original failure, recovered timestamp, and
recovery operation ID. It never rewrites a failed release to look as if no
failure occurred. Repeated recovery re-inspects live containers and checksums;
tests interrupt recovery after database restoration and prove the retry skips
that replacement, completes the remaining actions, and records a healthy
terminal result. Unrelated unfinished operations and incomplete cross-store
resolution refuse automatic action.

Acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q tests/unit/test_state.py
  tests/unit/test_releases.py tests/unit/test_recovery.py
  tests/unit/test_manual_restore.py tests/unit/test_recover_cli.py
  tests/unit/test_restore_cli.py tests/unit/test_deploy.py tests/test_cli.py` —
  passed (`114 passed`).
- Success tests prove previous rollback after a production-migration crash,
  retry after a second recovery interruption, completion of a checked new
  release after durable activation success, no-action after resolution, stable
  refusal, and explicit confirmation behavior.
- `.venv/bin/ruff check dploydb ...` and `.venv/bin/mypy dploydb` passed with no
  issues in `32` source files.

Slice 6E is complete. Slice 6F evidence follows.

##### 6F — Real crash/restore gate, documentation, and packaging

Status on 2026-07-19: `COMPLETE`. The deployment coordinator exposes bounded
test-only hard-crash checkpoints immediately after its durable maintenance,
current-app-stopped, and production-migrated records. Separate Python
processes terminate with `os._exit` at those checkpoints, leaving the normal
exception/rollback path unable to run. Recovery then correlates the exact dead
OS-lock owner with the interrupted operation, refuses unrelated owner tokens,
rechecks live Docker and backup/checksum state, and restores verified v1.

The real manual-restore gate performs two successful public deployments so the
release store contains an active and protected previous release. Preview is
read-only. Confirmed restore preserves the current state in a verified
`PRE_RESTORE` backup, restores the previous release's final backup, restarts
and verifies its exact application, switches traffic, and swaps pointers. The
test proves data written after the selected snapshot is absent from restored
production but remains present in the pre-restore backup.

The real Docker gate also exposed that a preserved older release can retain a
stale published-port route when a newer stopped container exists. The
production runner now validates that the selected release is stopped, refreshes
only its exact Compose network endpoints with bounded disconnect/reconnect
commands and preserved aliases, stores that evidence, and starts it only after
every reconnect succeeds. An unproven reconnect refuses startup. This makes
the public restore self-contained rather than relying on manual Docker state
changes.

Acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q
  tests/integration/test_deploy_end_to_end.py` — passed (`7 passed in 56.19s`).
  It proves a healthy deployment, production-migration and final-health
  rollback, three abrupt process crashes, and backup-first public manual
  restore. Each scenario removes its exact Docker resources.
- The three crash processes exited at the durable checkpoints after
  maintenance enable, current-app stop, and production migration. `status`
  returned exit `60`; read-only `recover --json` selected `recover_previous`;
  `recover --yes --json` completed `rolled_back`; v1 HTTP health, exact schema,
  row data, recovery operation ID, and preserved original failure were proven.
- The public restore test passed independently (`1 passed in 8.72s`). Its JSON
  preview changed nothing, its result recorded `manual_restore_completed`,
  selected/replaced release IDs and swapped pointers, production contained only
  the selected snapshot's rows, and the verified pre-restore backup retained
  the later row.
- Focused runner/coordinator/recovery tests passed (`65 passed in 2.59s`),
  including exact stale-owner acknowledgement, unrelated owner refusal,
  network endpoint refresh evidence, and refusal to start after an unproven
  reconnect.
- Final `.venv/bin/python -m pytest -q` passed with required loopback and Docker
  access (`504 passed in 167.48s`), covering all Milestone 0 through 6 tests.
  After the final help-text adjustment, the affected diagnostics/CLI tests
  passed again (`25 passed in 1.91s`).
- `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `git diff
  --check`, `.venv/bin/mypy dploydb`, and `.venv/bin/mypy demo` passed (`81`
  files formatted; no type issues in `32` package or `8` demo source files).
- `uv lock --check` and `uv sync --locked --check` passed with `38` resolved
  and `37` checked packages and no changes. `uv build` rebuilt
  `dist/dploydb-0.1.0.tar.gz` and the wheel; the wheel contains release,
  restore, recovery, runner, and CLI modules. `uv run python
  scripts/verify_pipx_install.py` passed after the final source adjustments.
- Root, `releases`, `release show`, `restore`, and `recover` help commands
  exited `0` and expose the required IDs, `--json`, and confirmation flags.
  Console and module version commands both printed `dploydb 0.1.0`.
- Final read-only Docker audits found no candidate container, no
  production-release container, and no DployDB network.
- `README.md` now documents release history, restore preview/confirmation,
  backup-first behavior, recovery preview/confirmation, exact safe refusal,
  idempotent retry, and the no-automatic-database-rollback traffic boundary.

At this Milestone 6 checkpoint, Milestone 7 was the next allowed work; no remote
backup or retention behavior was yet claimed.

#### Milestone 7 implementation scope

Planned on 2026-07-19:

- **Execution decision:** implement Milestone 7 as seven independently gated
  slices. Remote configuration, network storage, restore hydration, deployment
  failure semantics, and destructive retention have separate safety boundaries.
- **Owned modules:** bounded additions to `dploydb/config.py`, `models.py`,
  `backup.py`, `restore.py`, `manual_restore.py`, `deploy.py`,
  `deployment_dependencies.py`, `diagnostics.py`, and `cli.py`; a new
  `dploydb/storage/s3.py` and `dploydb/retention.py`; the storage contracts and
  local deletion behavior under `dploydb/storage/`; dependency metadata,
  documentation, and focused unit/integration/fault tests.
- **Compatibility boundary:** support AWS S3 and S3-compatible services,
  including Cloudflare R2 with `region_name: auto`, a custom HTTPS endpoint,
  path-style addressing, and a relative object prefix. Credentials are resolved
  only from named environment variables, registered for redaction, and never
  serialized.
- **Remote commit boundary:** upload a previously reverified local database
  object first and immutable metadata last. A remote backup is committed only
  when both objects can be read back and their size/checksum/identity evidence
  agrees. Every request uses bounded botocore timeouts and bounded retry/backoff.
- **Restore boundary:** download into a private local temporary file, verify the
  remote metadata, size, SHA-256, and SQLite contents, then publish the hydrated
  artifact through the existing immutable local store before any restore can
  inspect or mutate production.
- **Deployment boundary:** when remote storage is required, the stopped-writer
  final backup must be remotely committed before production migration starts.
  A failed upload leaves the database unchanged and enters the existing
  pre-traffic application rollback path.
- **Retention boundary:** derive one immutable protected set from the active and
  previous release manifests, preserve every referenced rehearsal/final backup,
  keep the newest configured unprotected backups, and make partial local/remote
  deletion safe to retry. Retention runs only after the active release and
  pointers are durable.
- **Credential boundary:** real service credentials are runtime-only acceptance
  inputs. Tests, examples, logs, operation events, release manifests, reports,
  and this plan contain only environment-variable names and synthetic secrets.

Progress tracker:

- [x] Milestone 7 overall — S3-compatible verified backup, restore hydration,
  required-remote deployment policy, and protected retention (`COMPLETE` on
  2026-07-19).
  - [x] 7A — remote configuration, storage/evidence contracts, dependency, and
    side-effect-free validation (`COMPLETE` on 2026-07-19).
  - [x] 7B — S3-compatible adapter with metadata-last commit, bounded retry,
    download, listing, verification, idempotent cleanup, and redaction
    (`COMPLETE` on 2026-07-19).
  - [x] 7C — verified `backup --upload` orchestration and stable human/JSON
    output (`COMPLETE` on 2026-07-19).
  - [x] 7D — verified remote hydration and restore/recovery fallback when the
    protected local artifact is absent (`COMPLETE` on 2026-07-19).
  - [x] 7E — required final-backup upload before production migration and the
    failed-upload application-continuity gate (`COMPLETE` on 2026-07-19).
  - [x] 7F — idempotent local/remote retention with active/previous release
    protection, applied only after durable activation (`COMPLETE` on
    2026-07-19).
  - [x] 7G — real Cloudflare R2 compatibility gate, full regression/static/
    packaging validation, CLI/demo exercise, documentation, and final audit
    (`COMPLETE` on 2026-07-19).

Tracking rule: a later slice starts only after the earlier slice's focused
tests pass. Update this section with exact commands and observed results before
marking any slice or the overall milestone complete.

##### 7A — Remote contracts and configuration

Status on 2026-07-19: `COMPLETE`. Configuration now distinguishes enabled and
required remote policy; accepts an HTTPS endpoint value or named endpoint
environment variable, `region_name: auto`, Standard/Standard-IA storage class,
relative normalized prefixes, and positive request/retry bounds; and preserves
disabled-remote compatibility. Credentials remain named references. Strict
remote record/artifact models and a separate off-server replica protocol avoid
weakening the local online-snapshot storage boundary. Boto3 is a locked runtime
dependency.

Acceptance evidence:

- `.venv/bin/python -m pytest -q tests/test_config.py` — passed (`105 passed`).
  Coverage includes required/enabled contradictions, endpoint exclusivity and
  HTTPS/loopback rules, bucket/prefix validation, region/storage defaults,
  positive bounds, credential references, and side-effect-free parsing.
- `.venv/bin/ruff check ...`, format check, and `.venv/bin/mypy ...` passed for
  the configuration, models, storage contract, and focused tests.
- `uv lock && uv sync --locked` resolved and installed Boto3 1.43.51 and
  Botocore 1.43.51 without storing any service credential.

##### 7B — S3-compatible storage adapter

Status on 2026-07-19: `COMPLETE`. `dploydb/storage/s3.py` rechecks the local
checksum and SQLite database, uploads database bytes first, reads back and
hashes the remote bytes, and publishes immutable JSON metadata last. Downloads
require a caller-owned mode-0600 staging file and pass size, SHA-256, and SQLite
verification. The adapter implements paginated listing, strict metadata/object
identity checks, safe repair of an uncommitted partial object, idempotent
metadata-first deletion, redacted SDK failures, S3v4 path-style addressing,
Cloudflare R2's `auto` region, and bounded standard retries plus connect/read
timeouts.

Acceptance evidence:

- `.venv/bin/python -m pytest -q tests/unit/test_s3_storage.py` — passed
  (`8 passed`). Coverage proves metadata-last ordering, full remote readback,
  verified download, idempotent exact retry, contradictory identity refusal,
  corrupt-download cleanup/refusal, partial-upload repair, deletion order and
  repeatability, runtime-only credential registration, R2 client settings,
  missing-credential refusal before client creation, and error redaction.
- `.venv/bin/ruff check ...` and `.venv/bin/mypy ...` passed for the adapter,
  models, and focused tests.

##### 7C — Verified standalone upload orchestration

Status on 2026-07-19: `COMPLETE`. The public `backup --upload` command creates
and reverifies the local online snapshot before invoking remote storage. A
required remote policy uploads even without the explicit flag. Local-only
success remains backward compatible; uploaded results include stable non-secret
provider/bucket/object/timestamp evidence. Remote failure ends the operation
`failed_safe`, preserves the verified local artifact, reports production
unchanged, and never prints a registered credential.

Acceptance evidence:

- `.venv/bin/python -m pytest -q tests/integration/test_backup_cli.py
  tests/unit/test_backup.py tests/unit/test_restore.py` — passed (`25 passed`).
  Coverage proves explicit upload, automatic required-policy upload, stable JSON
  and human output, durable `remote_snapshot_verified`, preserved local backup
  after remote failure, redaction, and disabled-remote refusal before local/state
  mutation.

##### 7D — Verified remote hydration and restore fallback

Status on 2026-07-19: `COMPLETE`. A missing local artifact can be resolved from
enabled remote storage into a fresh mode-0700 temporary directory and
mode-0600 staging file. The S3 adapter checks object metadata, stream size,
SHA-256, and SQLite contents; the bytes are then published and reverified with
the existing immutable local storage implementation inside that temporary
scope. Manual restore preview copies only verified metadata, confirmed restore
re-downloads under the deployment lock before maintenance, and recovery
diagnosis/execution use the same fallback. Present-but-corrupt local evidence
never silently falls back to remote storage.

Acceptance evidence:

- `.venv/bin/python -m pytest -q tests/unit/test_s3_storage.py` — passed
  (`10 passed`). The added gate removes the local artifact, hydrates the real
  SQLite bytes from the S3 protocol peer, restores them through the production
  restore transaction, proves the expected row/checksum, and proves temporary
  cleanup. A corrupt present local artifact produces no remote request.
- `.venv/bin/python -m pytest -q` over backup, S3, manual restore, restore,
  recovery, both CLI suites, and root CLI tests — passed (`67 passed`). Ruff,
  format check, and strict mypy passed for all `33` package source files.

##### 7E — Required final backup before production migration

Status on 2026-07-19: `COMPLETE`. Required remote storage is now an explicit
deployment dependency. After writers stop, DployDB creates and persists the
verified final local backup, commits and verifies its matching off-server
record with the release identity, and only then writes production-migration
intent or invokes the migration. A missing, failed, or contradictory remote
commit enters the existing pre-traffic rollback path: no database restore is
attempted because production never changed, while the exact previous
application, old traffic target, maintenance state, database checks, and health
are all restored and proven.

Acceptance evidence:

- `.venv/bin/pytest -q tests/unit/test_deploy.py -k required_remote` — passed
  (`2 passed`). The success gate proves durable remote evidence precedes the
  `production_migration_started` event. The failure gate proves no migration
  call or migration-intent event, an unchanged production checksum/schema,
  `failed_safe` with `production_changed=false`, and a healthy previous
  application after old-target and maintenance restoration.
- `.venv/bin/ruff check ...`, format check, and strict `.venv/bin/mypy dploydb`
  passed for the deployment boundary and all `33` package source files.

##### 7F — Protected local and remote retention

Status on 2026-07-19: `COMPLETE`. Retention takes one fully validated release
history snapshot after the new release manifest and active/previous pointers
are durable. It protects both rehearsal and final backup IDs from the active
and immediately previous releases, keeps the newest configured count of
additional project backups, never deletes records from another project, and
plans local and remote inventories before deletion. Local deletion is now
metadata-first and safely resumable when only database bytes remain; remote
deletion already has the same commit-marker-first retry property. A retention
failure after traffic activation is recorded as failed-safe housekeeping and
never triggers a forbidden database rollback.

Acceptance evidence:

- `.venv/bin/pytest -q tests/unit/test_retention.py tests/unit/test_backup.py`
  — passed (`11 passed`). Coverage proves old active/previous backups survive a
  `keep_last` of two locally and remotely, the newest unprotected backups are
  retained, foreign-project records are preserved, repeated cleanup is a
  no-op, a local-success/remote-failure pass resumes safely, and a metadata-
  first local unlink interruption resumes without advertising missing bytes.
- Focused retention, required-remote, S3, backup, deploy coordinator, backup
  CLI, and non-Docker integration coverage passed (`60 passed`). The seven real
  Docker tests could not access the sandboxed OrbStack socket in that run and
  remain part of the final unrestricted validation gate.
- Ruff, format check, and strict mypy passed for all `34` package source files.

##### 7G — Real service, regression, packaging, and documentation gate

Status on 2026-07-19: `COMPLETE`. Doctor
now validates enabled remote runtime credentials in normal mode and performs a
bounded read-only prefix-scoped bucket probe in deep mode. The README documents
Cloudflare R2/AWS configuration, required-remote cutover behavior, remote
hydration, retention protection, credential handling, and limitations. A
generic acceptance helper prompts without echo, creates a unique child prefix,
and proves upload, verified listing, verified download, SQLite restore, and
idempotent cleanup without storing credentials.

Acceptance evidence completed:

- Final post-audit `.venv/bin/pytest -q` with unrestricted loopback and
  OrbStack access — passed (`537 passed in 169.75s`). This includes the real
  Docker success, production-
  migration rollback, final-health rollback, three abrupt-process recovery
  points, and backup-first manual restore gates.
- `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` — passed (`86`
  files formatted); strict `.venv/bin/mypy dploydb` and `.venv/bin/mypy demo` —
  passed (`34` and `8` source files).
- `uv lock --check`, `uv sync --locked --check`, `uv build`, and
  `.venv/bin/python scripts/verify_pipx_install.py` — passed; both sdist and
  wheel built and isolated `pipx` installation verification passed.
- `git diff --check` — passed. No credential value is present in configuration,
  documentation, durable evidence, test fixtures, or the acceptance helper.

Live-service acceptance evidence:

- `.venv/bin/python scripts/verify_s3_compatibility.py` against the supplied
  Cloudflare R2 standard endpoint, `auto` region, `dploydb-backups` bucket, and
  a unique child of the configured `dploydb/learn` prefix — passed. The safe
  result recorded `upload_verified=true`, `download_verified=true`,
  `sqlite_restore_verified=true`, and `cleanup_verified=true`. The access-key
  pair was entered only through no-echo prompts; the unrelated Cloudflare API
  token was not used. The metadata commit marker and database object were both
  deleted and independently proven absent.

Milestone 7 is complete. Milestone 8 is the next allowed work; no dashboard or
other later-scope feature was started.

#### Milestone 8 implementation scope

Planned on 2026-07-19:

- **Owned modules and artifacts:** `dploydb/cli.py`, narrowly related CLI and
  packaging tests, `pyproject.toml`, installation/acceptance helpers under
  `scripts/`, real-user documentation under `docs/` and `examples/`, and
  `README.md`.
- **Preserved safety boundary:** Milestone 8 does not redesign the deployment
  coordinator, durable state model, backup/restore engine, configuration
  schema, or rollback rules. Any change at those boundaries requires a newly
  identified acceptance gap and focused regression evidence before use.
- **CLI boundary:** provide an explicit global `--no-color` option and standard
  `NO_COLOR` support, keep JSON output as one ANSI-free document, prove all
  machine-readable paths avoid terminal prompts, and audit every required
  command's help text.
- **Evidence-size boundary:** preserve append-only failure evidence while
  proving that each release has a finite event count and every persisted
  command, health response, container log, release manifest, and individual
  event has a tested byte bound. Do not rotate or delete active failure
  evidence.
- **Packaging boundary:** build both wheel and source distribution, verify the
  installed console entry point from an isolated `pipx` environment, and add
  sufficient project metadata for a real distributable artifact.
- **Documentation boundary:** provide one README-first installation and demo
  path, clear first-run production setup, complete configuration examples, one
  Nginx maintenance/traffic integration, security guidance, honest supported
  limits and post-traffic rollback boundary, and uninstall instructions that
  preserve state and backups.
- **Final gate:** a clean Linux environment must install the built artifact and
  run the documented deterministic demo without relying on an existing virtual
  environment. The gate must also parse a real JSON deployment result and
  audit the complete required help surface.

Progress tracker:

- [x] Milestone 8 overall — real-world usability and packaging (`COMPLETE` on
  2026-07-19).
  - [x] 8A — CLI, bounded evidence, help, and package usability (`COMPLETE` on
    2026-07-19).
  - [x] 8B — first-run, Nginx, security, limitations, and uninstall docs
    (`COMPLETE` on 2026-07-19).
  - [x] 8C — clean-Linux README-only installation and demo acceptance gate
    (`COMPLETE` on 2026-07-19).

Tracking rule: complete 8A and 8B with focused tests before starting the clean
Linux gate. Mark Milestone 8 complete only after the exact final commands and
observed results are recorded here; a local development-environment pass is
not evidence for the clean-install claim.

##### 8A — CLI, bounded evidence, help, and package usability

Status on 2026-07-19: `COMPLETE`. The root CLI now exposes `--no-color`, honors
the standard `NO_COLOR` environment marker, and uses a plain help/error renderer
so JSON and help output cannot contain ANSI escapes. Every required command and
nested release command has an installed help audit. Existing JSON and
confirmation tests plus the new contract prove that machine-readable preview
paths do not read the terminal and confirmed automation uses explicit
`--non-interactive` or `--yes` behavior.

Append-only operation evidence now has three independent finite bounds: at
most 1 MiB per event, 256 events, and 32 MiB for the complete operation log.
Existing command, container-log, smoke, health-response, release-manifest, and
backup-metadata limits remain enforced; over-limit durable evidence is treated
as recovery-required corruption rather than truncated or repaired silently.

Packaging metadata now names the README, supported Python/Linux environment,
console purpose, and searchable topics. The isolated installer accepts either
source or a built wheel and audits every help command, version aliases,
ANSI-free JSON initialization, and `pipx uninstall` preservation of user-owned
configuration and backup sentinel bytes.

Acceptance evidence:

- `.venv/bin/pytest -q tests/test_cli.py tests/unit/test_deploy_cli.py
  tests/unit/test_restore_cli.py tests/unit/test_recover_cli.py
  tests/unit/test_release_cli.py tests/unit/test_diagnostics.py
  tests/integration/test_backup_cli.py tests/unit/test_milestone8_cli.py
  tests/unit/test_state.py` — passed (`118 passed in 3.72s`) with required
  loopback access.
- `.venv/bin/ruff check ...`, format check, and `.venv/bin/mypy dploydb` —
  passed; strict mypy checked all `34` package modules.
- `uv lock --check && uv build` — passed; rebuilt the source distribution and
  wheel from the updated metadata.
- `.venv/bin/python scripts/verify_pipx_install.py
  dist/dploydb-0.1.0-py3-none-any.whl` — passed the isolated wheel install,
  complete help/JSON/version audit, uninstall, and preservation checks.

##### 8B — First-run, Nginx, security, limitations, and uninstall docs

Status on 2026-07-19: `COMPLETE`. The README now starts with an installed-CLI
demo path that prepares private absolute-path inputs, runs `doctor --deep`,
performs the real v1-to-v2 deployment, parses its JSON result, and preserves
generated evidence during normal stop. A production first-run guide covers
host ownership, install, configuration, runtime credentials, baseline backup,
diagnostics, and result handling. Separate security, supported-limit, rollback,
post-traffic data-loss, and backup-preserving uninstall guides make the trust
and recovery boundaries explicit.

The Nginx example uses a fixed loopback production port and a per-request
maintenance marker; it never routes users to the candidate port. Its Python
hook is bounded and idempotent, rejects target changes outside maintenance,
rejects symlinks and malformed/unsafe evidence, atomically publishes a private
old/new target record, and syncs managed filesystem changes. Its complete YAML
example passes the production configuration parser.

Acceptance evidence:

- `.venv/bin/pytest -q tests/integration/test_demo_prepare.py
  tests/integration/test_nginx_hook_example.py
  tests/unit/test_milestone8_docs.py tests/unit/test_milestone8_cli.py
  tests/unit/test_state.py` — passed as part of the focused Milestone 8 gate
  (`68 passed in 1.01s`).
- `.venv/bin/ruff check ...` and format check passed for the demo preparer,
  executable Nginx hook, and documentation tests; `.venv/bin/mypy demo` passed
  all `9` demo modules.
- Documentation tests proved every local link resolves, required quick-start
  commands and safety warnings are present, the uninstall guide contains no
  recursive deletion shortcut, and the Nginx site has no candidate upstream.

##### 8C — Clean-Linux README-only installation and demo acceptance gate

Status on 2026-07-19: `COMPLETE`. The acceptance helper builds a source bundle
from the current tracked/untracked non-ignored worktree and boots a uniquely
named disposable privileged Docker-in-Docker Linux host. It copies in only that
source bundle and the reviewed wheel, installs Linux prerequisites and the wheel
with `pipx`, audits all installed help/version/color interfaces, and follows the
README's real v1-to-v2 workflow. It parses `doctor --deep` and deployment JSON,
verifies final HTTP health, active release history, SQLite schema version 2,
and absence of DployDB Compose containers/networks after documented stop.

The demo controller's stop/reset cleanup was extended after the gate exposed
that a successfully deployed application is an operation-created Compose
one-off rather than the bootstrap project. Cleanup now selects a release
container only when its DployDB role, derived project/name, exact demo database
mount, loopback address, and production port all agree. It then removes only
that proven container and its exactly labeled project network. This makes the
README cleanup repeatable without broad name filters or unrelated mutation.

Acceptance evidence:

- `.venv/bin/python scripts/verify_clean_linux.py --wheel
  dist/dploydb-0.1.0-py3-none-any.whl` — passed in `docker:29.1-dind`, resolved
  to `docker@sha256:3a33fc81fa4d38360f490f5b900e9846f725db45bb1d9b1fe02d849bd42a5cf2`.
- The clean host reported Python `3.12.13` and nested Docker `29.1.5`; the
  source bundle contained `121` files and the wheel SHA-256 was
  `c1ea21acd1d330661fcd968ee3ecf881156ddf42bf5b8c17a7c8c37082b27f5b`.
- The shipped `examples/nginx/site.conf` passed the real Alpine Nginx
  configuration parser with `nginx -t` inside the same clean host.
- The real JSON result was `outcome=active` for
  `release_f5bdb593936944feb1b3a05d94eced9d`; traffic activation, HTTP v2
  health, release pointer, and SQLite v2 schema were independently checked.
- After normal demo stop there were no Compose-labeled application/candidate
  containers and no DployDB network. `pipx uninstall` removed the console
  entry point while preserving all `14` generated database, configuration,
  backup, release, manifest, and event files byte-for-byte. The outer
  disposable Linux container was removed successfully.

#### Milestone 8 final acceptance evidence

Observed on 2026-07-19 after all slice changes:

- `.venv/bin/pytest -q` with unrestricted loopback and Docker access — passed
  (`569 passed in 171.56s`). This includes real backup, candidate, successful
  cutover, production-migration rollback, final-health rollback, abrupt crash
  recovery, manual restore, remote retention, demo, and Milestone 8 gates.
- `.venv/bin/ruff check .` — passed (`All checks passed!`);
  `.venv/bin/ruff format --check .` — passed (`93 files already formatted`).
- `.venv/bin/mypy dploydb` and `.venv/bin/mypy demo` — passed in strict mode
  for all `34` package and `9` demo modules. Both Milestone 8 verification
  helpers also passed strict mypy.
- `uv lock --check`, `uv sync --locked --check`, and `uv build` — passed after
  refreshing the ignored editable environment for the final package metadata;
  both source and wheel artifacts were built. Final SHA-256 values were
  `c1ea21acd1d330661fcd968ee3ecf881156ddf42bf5b8c17a7c8c37082b27f5b`
  for the wheel and
  `20ca91149ad99a6b11753b1d6a1304bc51731234293e5ea0792a25bf0d5ed9a9`
  for the source distribution.
- `.venv/bin/python scripts/verify_pipx_install.py
  dist/dploydb-0.1.0-py3-none-any.whl` — passed against the final wheel,
  including every installed help command, version, ANSI-free JSON, uninstall,
  and preservation audit.
- `git diff --check` — passed. No generated demo state, Linux acceptance
  container, inner Compose container/network, credential, or package-manager
  environment is included in the worktree.

Milestone 8 is complete. Milestone 9 presentation polish is the next allowed
work; no dashboard, generated release report, schema visualization, or other
Milestone 9 feature was started.

#### DployDB 0.1.0 Alpha publication readiness

Planned on 2026-07-19:

- **Owned artifacts:** `pyproject.toml`, the public license and community
  policies, release documentation, package/distribution verification helpers,
  and GitHub CI/release workflows.
- **License boundary:** publish the original DployDB work under Apache-2.0,
  copyright 2026 RecursiveWay, without adding per-file headers or claiming
  ownership of third-party dependencies.
- **Package boundary:** retain version `0.1.0` and the Alpha classifier, add
  PEP 639 metadata and public project links, include license evidence in wheel
  and source distribution, and exclude local agent settings and internal
  planning instructions from published artifacts.
- **Release boundary:** build one reviewed wheel/source pair, verify it on a
  clean Linux host, publish those exact bytes through protected OIDC Trusted
  Publishing environments, and finalize a GitHub prerelease only after public
  installation succeeds. Long-lived PyPI credentials are prohibited.
- **Public contract boundary:** this slice changes no deployment behavior,
  command, option, exit code, JSON shape, configuration schema, durable state,
  recovery protocol, or backup format.
- **Acceptance gate:** focused metadata/documentation/distribution tests, the
  complete real Docker/loopback suite, Ruff, format check, strict mypy for the
  package and demo, fresh build validation, isolated pipx installation, and
  the clean-Linux README deployment gate must pass before a release tag.

Local acceptance evidence observed on 2026-07-19:

- `.venv/bin/python -m pytest -q` with unrestricted Docker and loopback access
  passed (`588 passed in 172.25s`), including all prior deployment, rollback,
  restore, recovery, retention, and clean-state safety coverage plus 19 new
  release-readiness tests.
- Focused metadata, public-documentation, workflow, version, and distribution
  tests passed (`40 passed`). Ruff check and format check passed for all `98`
  Python files; strict mypy passed for all `34` package modules, `9` demo
  modules, and `6` release-verification scripts.
- `uv lock --check` passed with `65` resolved packages. `uv build` produced a
  wheel from the source distribution, and Twine accepted both artifacts.
- `scripts/verify_distribution.py --tag v0.1.0` passed. The final wheel contains
  `40` files with SHA-256
  `1235050bdcee0688b6bc34b5a642d9f13e235de100ec086bcdb693eb72771d1d`;
  the allowlisted source distribution contains `131` files with SHA-256
  `a882f434f0d3238f38eff2a1eb5d5cfc15525fd99292f17c6a253aeda7803532`.
  Apache-2.0 metadata, repository license/notice bytes, author, Alpha
  classifier, Python requirement, and project links matched exactly; local
  agent settings, internal plans, databases, generated state, and credentials
  were absent.
- The isolated pipx audit installed the final wheel, verified every required
  help/version/JSON path, uninstalled it, and preserved configuration and
  backup bytes. Actionlint `v1.7.7` accepted both pinned GitHub workflows.
- The disposable `docker:29.1-dind` clean-Linux gate passed with Python 3.12.13
  and Docker 29.1.5. It installed the final wheel, completed a real deployment
  with `outcome=active`, preserved all `14` database/state/backup/release files
  byte-for-byte through uninstall, and removed the inner and outer Docker
  resources. The verified wheel hash matched the final hash above.
- `git diff --check` passed. No deployment behavior, command, option, exit code,
  JSON shape, configuration schema, durable state, recovery protocol, or
  backup format changed.

GitHub preparation evidence observed on 2026-07-19:

- The release changes are committed on `codex/alpha-release-readiness` and the
  draft release PR is [recursiveway/dployDB#1](https://github.com/recursiveway/dployDB/pull/1).
- The `recursiveway` GitHub CLI session was reauthenticated. The repository is
  public with Issues, secret scanning, push protection, Dependabot alerts and
  automated security fixes, and private vulnerability reporting enabled.
- Protected `testpypi` and `pypi` environments require review by a
  RecursiveWay maintainer. Active GitHub tag ruleset `19168357` protects
  creation, update, deletion, and non-fast-forward changes for `v*` tags.
- The first Linux PR gate exposed a platform-dependent SQLite test expectation:
  the GitHub runner returned an immediate safe `database is locked` error where
  the local SQLite build exhausted `busy_timeout`. The test now accepts either
  explicit safe result and still proves the operation returns within its bound;
  production behavior remains unchanged.
- GitHub Actions run
  [`29684025591`](https://github.com/recursiveway/dployDB/actions/runs/29684025591)
  passed the complete `Safety and package gate` on Ubuntu in 2m52s: locked
  dependency sync, Ruff, format, strict mypy, all `588` tests, and the clean
  wheel/source build and distribution audit.
- `main` now requires that exact successful check with strict up-to-date branch
  enforcement. Protection includes administrators, requires linear history and
  resolved review conversations, and prohibits force-pushes and deletion.

Status: `LOCAL GATE COMPLETE; PUBLICATION PENDING`. No signed `v0.1.0` tag
exists. The dedicated SSH signing key, matching TestPyPI/PyPI pending Trusted
Publishers, release PR merge, registry uploads, and GitHub prerelease still
require their ordered external gates. Registry URLs, public hashes, signed-tag
evidence, and GitHub prerelease evidence must be appended here after those
gates pass. The release must not be called published before that verification.

---

## 2. Product promise

A developer should be able to run:

```bash
dploydb deploy --version v2
```

DployDB should then:

1. Check the host, configuration, application, and database.
2. Create and verify a consistent SQLite snapshot.
3. Rehearse the migration against a temporary copy.
4. Start the candidate application against the rehearsed copy.
5. Run health and smoke checks.
6. Put production into a controlled maintenance state.
7. Stop the old application from writing.
8. Create a final production snapshot.
9. Apply the already-rehearsed migration to production.
10. Start and verify the new application before enabling traffic.
11. Activate the new release and record all evidence.
12. Restore the previous database and application automatically if a pre-traffic cutover step fails.

The main user-facing guarantee is:

> A failed rehearsal never touches production. A failed pre-traffic cutover restores the last working application and database.

Do not claim universal zero downtime, protection from every hardware failure, or safe automatic database rollback after the new release has accepted production writes.

---

## 3. Supported scope for the hackathon

### Required environment

- Linux host.
- One SQLite database file.
- One Docker Compose application service.
- One application version active at a time.
- A developer-supplied migration command.
- An HTTP health endpoint.
- Optional developer-supplied smoke-test command.
- Local backup storage.
- One optional S3-compatible remote backup target.

### Required implementation stack when starting from an empty repository

Use this stack unless the repository already has a clear alternative:

- Python 3.12 or newer.
- Typer for the CLI.
- Rich for terminal output.
- Pydantic for configuration validation.
- PyYAML for configuration files.
- Python `sqlite3` backup API for live snapshots.
- HTTPX for health checks.
- Boto3 for optional S3-compatible storage.
- Pytest for tests.
- Ruff for linting and formatting.
- Mypy for type checking.
- `pyproject.toml` with an installable `dploydb` console command.

### Explicit non-goals

Do not implement these before all required milestones pass:

- Kubernetes.
- Multi-host orchestration.
- SQLite replication.
- Continuous point-in-time recovery.
- Multi-tenant accounts, authentication, billing, or teams.
- Windows or macOS production support.
- A custom migration language.
- Automatic migration generation.
- Support for every framework or deployment platform.
- Universal zero-downtime schema changes.
- Automatic database restore after production traffic has been enabled.
- A large web dashboard.
- Desktop application deployment.

---

## 4. Non-negotiable safety rules

Every agent and every code path must follow these rules.

1. **Never modify production before the rehearsal passes.**
2. **Never treat a backup as successful until it opens, passes a SQLite check, and has a recorded checksum.**
3. **Never use a plain file copy for a live SQLite snapshot.** Use SQLite's online backup API. A normal copy is allowed only for an already-created, verified snapshot or while all database users are stopped.
4. **Never run two deployments at the same time.** Use an operating-system-backed lock and detect stale/interrupted state.
5. **Never overwrite the only known-good backup.** Backups and release manifests are immutable after completion.
6. **Never delete the current release's backup during retention cleanup.**
7. **Never run an unbounded subprocess.** Migration, application, health, storage, and hook commands require timeouts.
8. **Never use `shell=True` by default.** Commands are argument arrays. Shell execution requires an explicit opt-in and warning.
9. **Never print secrets.** Redact credentials, tokens, signed URLs, and sensitive environment values from logs and errors.
10. **Never report success from a mocked check in the real deployment path.** Mocks belong only in unit tests.
11. **Never automatically restore an old database after production traffic is active.** That can discard newly written data. Require an explicit manual restore with a data-loss warning.
12. **Never remove failure evidence.** Preserve release manifests and logs even after cleanup.
13. **Never replace a production database with an unverified restore file.** Verify first, stop all users, restore through a temporary path, then atomically replace it.
14. **Never leave maintenance mode enabled silently.** Every failure path must attempt safe cleanup and clearly report any manual action still required.
15. **Prefer stopping safely over guessing.** Unknown state becomes `recovery_required`, not automatic destructive action.

---

## 5. Required CLI experience

Implement these commands:

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

### Required behavior

- Human-readable terminal output is the default.
- `--json` returns stable machine-readable results for CI systems.
- `--non-interactive` fails rather than waiting for input.
- Commands return useful exit codes.
- Every error explains:
  - what failed,
  - whether production changed,
  - whether the old application is still running,
  - what the user should do next.

### Exit codes

Use these unless a stronger convention already exists:

```text
0  success
1  operation failed safely; production is known to be usable
2  invalid configuration or usage
3  recovery is required because production state is uncertain
4  restore refused because confirmation was missing
5  dependency or environment failure
```

---

## 6. Configuration contract

Create `dploydb.yaml` with strict validation and useful defaults.

Example:

```yaml
project: example-app

state_directory: /srv/example/.dploydb

database:
  path: /srv/example/data/app.db
  path_env: DATABASE_PATH
  minimum_free_space_multiplier: 3

migration:
  command: ["python", "scripts/migrate.py"]
  timeout_seconds: 120

application:
  runner: docker_compose
  compose_file: /srv/example/compose.yaml
  service: app
  candidate_port: 4511
  candidate_container_port: 8080
  database_volume_target: /data
  candidate_health_url: http://127.0.0.1:4511/health
  startup_timeout_seconds: 45
  smoke_command: ["python", "scripts/smoke_test.py"]
  test_mode_env:
    DPLOYDB_TEST_MODE: "1"

traffic:
  maintenance_on_command: ["/srv/example/ops/maintenance", "on"]
  maintenance_off_command: ["/srv/example/ops/maintenance", "off"]
  activate_new_command: ["/srv/example/ops/activate", "candidate"]
  activate_old_command: ["/srv/example/ops/activate", "current"]

backup:
  local_directory: /srv/dploydb/backups/example-app
  keep_last: 10
  remote:
    enabled: false
    provider: s3
    bucket: example-backups
    prefix: dploydb/example-app
    endpoint_url_env: S3_ENDPOINT_URL
    access_key_env: S3_ACCESS_KEY_ID
    secret_key_env: S3_SECRET_ACCESS_KEY
```

Requirements:

- Support environment-variable interpolation without storing resolved secrets in release manifests.
- Reject relative production database paths unless explicitly allowed.
- Reject candidate ports already in use.
- Validate that required commands and files exist.
- Validate all timeout values and retention values.
- Preserve unknown configuration keys only if there is a documented extension mechanism; otherwise reject them to catch typos.

---

## 7. Architecture boundaries

Keep orchestration separate from integrations so the project can grow after the hackathon.

Recommended layout:

```text
dploydb/
  __init__.py
  cli.py
  config.py
  errors.py
  models.py
  state.py
  locking.py
  redaction.py
  subprocesses.py
  sqlite_checks.py
  backup.py
  migration.py
  health.py
  deploy.py
  restore.py
  recovery.py
  retention.py
  reporting.py
  runners/
    base.py
    docker_compose.py
  storage/
    base.py
    local.py
    s3.py

tests/
  unit/
  integration/
  fault_injection/

demo/
  app/
  compose.yaml
  migrations/
  scripts/
```

### Required interfaces

Create narrow interfaces for:

- `ApplicationRunner`
  - start candidate against a supplied database path,
  - stop candidate,
  - stop current application,
  - start previous application,
  - start new application against production,
  - inspect running state,
  - collect logs.
- `BackupStorage`
  - put,
  - get,
  - exists,
  - list,
  - delete,
  - verify metadata.
- `TrafficController`
  - enable maintenance,
  - disable maintenance,
  - activate new release,
  - reactivate previous release.
- `HealthChecker`
  - HTTP readiness check,
  - optional smoke-test command.

Only implement the Docker Compose runner, command-based traffic hooks, local storage, and S3-compatible storage for the hackathon. The interfaces exist to prevent the implementation from becoming a one-off script.

---

## 8. Persistent release state

Every operation creates or updates a release manifest. Write manifests atomically through a temporary file plus `os.replace`.

Required states:

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

Required manifest information:

```json
{
  "schema_version": 1,
  "release_id": "20260717T143012Z-v2",
  "project": "example-app",
  "requested_version": "v2",
  "status": "active",
  "previous_release_id": "20260717T121005Z-v1",
  "database_path": "/srv/example/data/app.db",
  "rehearsal_backup_path": "/srv/dploydb/backups/example-app/...db",
  "final_backup_path": "/srv/dploydb/backups/example-app/...db",
  "backup_sha256": "...",
  "backup_size_bytes": 123456,
  "migration_command_fingerprint": "...",
  "configuration_fingerprint": "...",
  "migration_status": "passed",
  "candidate_health_status": "passed",
  "production_health_status": "passed",
  "production_changed": true,
  "traffic_enabled_for_new_release": true,
  "started_at": "2026-07-17T14:30:12Z",
  "completed_at": "2026-07-17T14:31:04Z",
  "failure": null,
  "logs": {
    "migration": "migration.log",
    "candidate": "candidate.log",
    "health": "health.log"
  }
}
```

Also record an append-only event log for each release. The final manifest is a summary; the event log is the recovery trail.

---

## 9. Exact deployment algorithm

The implementation must follow this order unless a reviewed design change proves a safer sequence.

### Rehearsal stage: production remains untouched

1. Parse and validate configuration.
2. Acquire the deployment lock.
3. Inspect previous state for interrupted operations.
4. Run preflight checks.
5. Create a consistent rehearsal snapshot using SQLite's backup API.
6. Verify the snapshot with `PRAGMA quick_check`, `PRAGMA foreign_key_check`, file size, and SHA-256.
7. Copy the verified snapshot to a disposable rehearsal path.
8. Run the migration command against the rehearsal path by setting the configured database environment variable.
9. Capture exit code, stdout, stderr, duration, and timeout status.
10. Run post-migration database checks.
11. Start the candidate application against the rehearsed database in test mode.
12. Run HTTP readiness and optional smoke checks.
13. Stop and remove the rehearsal candidate.
14. Abort safely if any rehearsal step fails. Production must remain unchanged.

### Controlled cutover stage

1. Enable maintenance mode or otherwise block normal traffic.
2. Stop the current application and background jobs so no process can write to SQLite.
3. Confirm the database is no longer in use by the managed application.
4. Create a final consistent snapshot of production.
5. Verify the final snapshot and checksum.
6. Run the same migration command against the production database.
7. Run production database checks.
8. Start the new application against production while traffic is still blocked.
9. Run its health and smoke checks.
10. Activate traffic for the new release.
11. Disable maintenance mode.
12. Mark the release active and preserve the previous release for recovery.
13. Apply retention only after the active release is durable.

### Automatic rollback stage

Automatic database rollback is allowed only before step 10 above completes.

When a cutover step fails before traffic activation:

1. Mark `rollback_started`.
2. Stop the failed new application.
3. Verify the final backup again.
4. Restore it through a temporary file and atomic replacement.
5. Remove stale SQLite `-wal` and `-shm` files only while all application processes are stopped.
6. Run SQLite checks on the restored production database.
7. Start the previous application version.
8. Reactivate the previous traffic target.
9. Disable maintenance mode.
10. Run the previous application's health check.
11. Mark `rolled_back` only when all rollback checks pass.
12. Mark `recovery_required` and print exact manual steps if rollback cannot be proven successful.

After traffic activation, do not automatically restore an older database. Record the problem and require `dploydb restore` with explicit confirmation.

---

## 10. Build order and milestone gates

Build in this exact order. Do not start a later milestone because it looks more impressive. Each gate must pass before moving forward.

### Milestone 0 — Repository, contract, and deterministic demo fixture

Build:

- Installable package skeleton.
- `pyproject.toml` and console entry point.
- Basic CLI help and version command.
- Example `dploydb.yaml`.
- A tiny Docker Compose demo application.
- Four deterministic demo releases:
  - working v1,
  - working v2 with a valid migration,
  - broken migration release,
  - broken health-check release.
- `IMPLEMENTATION_PLAN.md` generated from these milestones and updated as work proceeds.

Gate:

- A new developer can start v1 with one documented command.
- The demo app writes and reads real SQLite data.
- The broken versions fail for known, repeatable reasons.

### Milestone 1 — Configuration, locking, state, and error model

Build:

- `init`, `doctor`, and `status` commands.
- Strict configuration validation.
- Environment interpolation and secret redaction.
- Deployment lock with stale-state detection.
- Atomic release manifests and append-only events.
- Structured error types and stable exit codes.
- Subprocess wrapper with timeouts, captured logs, and cancellation cleanup.

Gate:

- Two concurrent deploy attempts cannot proceed.
- Invalid configuration fails before any database operation.
- Killing the process leaves enough state for `status` to explain what happened.
- Secrets do not appear in terminal output, JSON output, or logs.

### Milestone 2 — Consistent backup, verification, and basic restore

Build:

- SQLite preflight checks.
- Online backup API snapshot.
- SHA-256 calculation.
- Snapshot `quick_check` and `foreign_key_check`.
- Local backup metadata.
- `backup` and `verify` commands.
- Restore to a stopped demo application through a temporary file and atomic replacement.

Gate:

- Backing up while a test writer is active produces a database that opens and passes checks.
- A deliberately corrupted backup is rejected.
- A restored database contains the expected records.
- Restore failure cannot destroy the current database.

Do not proceed until backup and restore are proven by integration tests. This is the foundation of every later safety claim.

### Milestone 3 — Migration rehearsal

Build:

- Rehearsal database creation from a verified snapshot.
- Migration environment injection.
- Migration timeout and cancellation.
- Complete stdout/stderr log capture.
- Post-migration SQLite checks.
- Clear failure reporting showing that production was not changed.

Gate:

- The valid migration passes on the copy.
- The broken migration fails.
- The production database checksum and schema remain unchanged after the failed rehearsal.
- A timed-out migration is terminated and production remains unchanged.

### Milestone 4 — Candidate application validation

Build:

- Docker Compose runner.
- Candidate startup on an isolated port.
- Candidate database path injection.
- Test-mode environment variables.
- HTTP readiness with retries and timeout.
- Optional smoke-test command.
- Candidate logs and cleanup.

Gate:

- Working v2 starts against the rehearsed database and passes checks.
- The broken-health release is rejected.
- The existing application keeps serving while the candidate is tested.
- Candidate containers and temporary files are removed after success and failure.

### Milestone 5 — Controlled cutover and automatic pre-traffic rollback

Build:

- Maintenance-mode hooks.
- Current application stop/start operations.
- Final verified backup after writes stop.
- Production migration.
- New application startup against production while traffic remains blocked.
- Health check before traffic activation.
- Traffic activation hooks.
- Automatic database and application rollback before traffic activation.
- Idempotent cleanup for each failure point.

Gate:

- A successful release changes the application and expected schema.
- A forced production migration failure restores the previous database and application.
- A forced final health failure restores the previous database and application.
- The previous application passes health checks after rollback.
- The release manifest and event log explain every action.
- No normal user traffic reaches the new release before its final health check passes.

This is the most important hackathon milestone. Do not trade it for a dashboard.

### Milestone 6 — Manual restore and interrupted-operation recovery

Build:

- `releases` and `release show`.
- Manual restore preview.
- Required data-loss warning.
- Backup of the current state before manual restore.
- `recover` command for interrupted cutovers.
- Recovery decision logic based on durable state and live process inspection.
- `recovery_required` output when automatic proof is impossible.

Gate:

- A selected old release can be restored end to end.
- The current state is backed up before restoring an older release.
- A simulated crash after maintenance mode, after current-app stop, and after production migration can be diagnosed.
- `recover` either returns to a proven healthy release or refuses destructive action and provides exact manual steps.

Milestones 0 through 6, plus installation and quick-start documentation, are the minimum production-useful release.

### Milestone 7 — Off-server backup and retention

Build:

- S3-compatible `BackupStorage` implementation.
- Upload only after local verification.
- Metadata including checksum, size, release ID, and creation time.
- Download to a temporary file and verify before restore.
- Retry policy with bounded backoff.
- Local and remote retention.
- Protection for active and previous release backups.

Gate:

- A backup uploads to an S3-compatible test service such as MinIO.
- A downloaded backup matches its recorded checksum and restores correctly.
- Failed remote upload stops deployment when remote backup is configured as required.
- Retention never deletes protected backups.

Build this before any web dashboard. Off-server recovery adds more real value.

### Milestone 8 — Real-world usability and packaging

Build:

- `pipx install .` or equivalent clean installation.
- Clear first-run setup.
- `doctor --deep`.
- `--json`, `--non-interactive`, and `--no-color` support.
- Log rotation or bounded per-release logs.
- Helpful configuration examples.
- README quick start that a stranger can follow.
- Security and limitations documentation.
- Uninstall instructions that do not remove backups.
- One example integration for Caddy or Nginx maintenance/traffic hooks.

Gate:

- A clean Linux environment can install the CLI and run the demo from documentation alone.
- All commands have useful `--help` output.
- CI can parse a JSON deployment result.
- Documentation honestly explains rollback limits and possible post-traffic data loss.

### Milestone 9 — Hackathon-winning presentation polish

Build only after all core gates pass:

- Rich deployment timeline.
- Human-readable failure summaries.
- One generated deployment report per release.
- Optional schema diff using `sqlite_schema` before and after rehearsal.
- A small read-only dashboard only if time remains.
- A deterministic `make demo` or `./demo/run.sh` flow.
- A recorded backup demo in case live infrastructure fails.

Gate:

- The live demo shows a failure in under two minutes.
- Judges can see that the old app stays healthy and production data remains unchanged.
- The successful deployment follows immediately afterward.
- No demo step depends on manually editing state or pretending a check passed.

---

## 11. Required preflight checks

`dploydb doctor` and every deployment must check:

- Configuration parses and validates.
- State directory exists or can be created.
- State and backup directories are writable.
- Database file exists and is a regular file.
- Database can be opened read-only for checks.
- `PRAGMA quick_check` returns `ok`.
- `PRAGMA foreign_key_check` returns no rows.
- Deep mode runs `PRAGMA integrity_check`.
- Available disk space is enough for the rehearsal copy, final backup, temporary restore, logs, and safety margin.
- Docker and Docker Compose are available.
- Compose file and service exist.
- Migration executable exists.
- Candidate port is available.
- Health URL is local or explicitly allowed.
- Maintenance and traffic hook commands exist.
- Backup storage is reachable when configured as required.
- No deployment lock or unresolved recovery state blocks the operation.

Report warnings separately from failures. Do not silently change SQLite PRAGMA settings in a user's production database.

---

## 12. Test requirements

### Unit tests

Cover:

- Configuration parsing and invalid values.
- Secret redaction.
- State transitions.
- Atomic manifest writes.
- Stale lock handling.
- Subprocess timeout behavior.
- Checksum verification.
- Retention protection rules.
- Failure-message formatting.

### Integration tests

At minimum, automate these scenarios:

| Scenario | Required result |
|---|---|
| Database path missing | Stop before backup |
| Database cannot be opened | Stop before deployment |
| Backup directory unwritable | Stop safely |
| Live writer during snapshot | Verified backup remains valid |
| Backup corrupted after creation | Verification fails |
| Migration exits non-zero | Production unchanged |
| Migration hangs | Process terminated; production unchanged |
| Post-migration check fails | Production unchanged |
| Candidate cannot start | Old app remains active |
| Candidate returns HTTP 500 | Old app remains active |
| Smoke command fails | Old app remains active |
| Maintenance hook fails | Cutover does not start |
| Final backup fails | Old app/database remain recoverable |
| Production migration fails | Final backup restored; old app healthy |
| New app final health fails | Final backup restored; old app healthy |
| Traffic activation fails | Restore old target or mark recovery required |
| Second deployment starts | Lock blocks it |
| Process crashes during cutover | `recover` diagnoses durable state |
| Manual restore selected | Current state backed up first |
| S3 download changed/corrupt | Restore refused |
| Retention runs | Active and previous backups kept |
| Secret appears in subprocess output | Stored output is redacted |

### Fault injection

Provide test hooks that can fail each deployment stage deliberately. Fault injection must be enabled only in tests or the demo environment, never by default in production.

### Required validation commands

Use equivalent commands if the repository changes tools:

```bash
python -m pytest -q
ruff check .
ruff format --check .
mypy dploydb
```

Run all validation commands before marking a milestone complete. Fix failures instead of documenting them as acceptable.

---

## 13. Demo design

The demo must prove safety, not merely show a successful deployment.

### Demo screen

Keep two visible areas:

1. A browser or terminal loop calling the current application every second.
2. The DployDB deployment timeline.

### Demo sequence

#### Failure 1: broken migration

- Start working v1.
- Create visible user data.
- Deploy the broken migration release.
- Show rehearsal failure.
- Show that v1 still responds.
- Show that the production checksum/schema did not change.

#### Failure 2: broken application

- Deploy a release whose migration passes but health endpoint fails.
- Show candidate rejection.
- Show that v1 still responds and production remains unchanged.

#### Success

- Deploy working v2.
- Show backup, rehearsal, candidate validation, maintenance, final backup, migration, health, activation, and release manifest.
- Show the new feature and preserved user data.

#### Recovery proof

- Trigger a final-health failure during controlled cutover.
- Show automatic restoration of both application and database.
- Show the previous application healthy again.

### Required final summary

Example:

```text
DployDB release: 20260717T143012Z-v2

Configuration          PASSED   0.1s
Database preflight     PASSED   0.4s
Rehearsal snapshot     PASSED   1.2s
Migration rehearsal    PASSED   2.6s
Candidate application  PASSED   3.1s
Maintenance mode       PASSED   0.2s
Final snapshot         PASSED   1.0s
Production migration   PASSED   2.1s
Final health check     PASSED   0.6s
Traffic activation     PASSED   0.2s

Release active: v2
Previous release retained: v1
Verified backup: local + S3
```

Failure output must include plain language such as:

```text
Deployment stopped because the candidate application could not read the users table.

The current application is still running.
The production database was not changed.
See: .dploydb/releases/<release-id>/candidate.log
```

---

## 14. Features that improve the chance of winning

After correctness, prioritize visible proof in this order:

1. Automatic pre-traffic application-and-database rollback.
2. A live request monitor proving the old application stays available during rehearsal failure.
3. Verified off-server backup and restore through an S3-compatible service.
4. Clear release timeline and failure explanation.
5. Release history with checksums and evidence.
6. One-command deterministic demo.
7. Schema diff from rehearsal.
8. Read-only dashboard.

Do not build the dashboard before items 1 through 6 work.

---

## 15. Rules for Codex agents

### Before changing code

1. Read this file completely.
2. Inspect the repository and current tests.
3. Open or create `IMPLEMENTATION_PLAN.md`.
4. Identify the current milestone and acceptance gate.
5. State the files you expect to change.
6. Reuse existing code and conventions where sensible.

### While implementing

- Work on one milestone or one clearly bounded part of a milestone.
- Keep critical-path code real; no placeholders or hard-coded success values.
- Add or update tests in the same change as behavior.
- Keep public interfaces small and typed.
- Preserve backward compatibility for configuration once documented.
- Use dependency injection around subprocesses, clocks, storage, and runners where it improves fault testing.
- Store timestamps in UTC and display local time only as an optional presentation choice.
- Use `pathlib` and explicit file permissions for sensitive backup/state files.
- Capture command output, but redact it before persistence.
- Clean temporary resources in `finally` blocks or context managers.
- Make cleanup idempotent because recovery may rerun it.
- Never swallow exceptions without recording the stage and safe next action.

### Before marking work complete

1. Run focused tests for the changed module.
2. Run the full test suite.
3. Run lint, format check, and type check.
4. Exercise the relevant CLI path manually against the demo app.
5. Update `IMPLEMENTATION_PLAN.md` with evidence, not just a checkbox.
6. Report exact commands run and their results.
7. Call out any remaining safety gap clearly.

### Parallel-agent rules

- Define interfaces and manifest/config schemas before parallel implementation begins.
- Give each agent ownership of separate modules and tests.
- Do not have multiple agents edit `deploy.py`, `models.py`, or the configuration schema simultaneously.
- Suggested parallel ownership after Milestone 1:
  - Agent A: SQLite checks, backup, restore tests.
  - Agent B: migration rehearsal and subprocess handling.
  - Agent C: Docker Compose runner and health checks.
  - Agent D: CLI/reporting/demo app.
  - Agent E: S3 storage and retention, only after local backup contracts are stable.
- Merge in milestone order and rerun all integration tests after every merge.

### Prohibited shortcuts

- Do not replace the database safety path with shell scripts that bypass state tracking.
- Do not use sleeps as the only readiness check.
- Do not hide failed tests to keep a demo green.
- Do not mark backup complete before verification.
- Do not mutate release history to make a failed deployment appear successful.
- Do not add a UI that directly edits database or release state.
- Do not broaden platform support until the supported path is reliable.

---

## 16. Definition of done

The hackathon release is complete only when all statements below are true:

- A stranger can install DployDB on a clean Linux environment from the README.
- `dploydb init` creates a valid starting configuration.
- `dploydb doctor` identifies missing dependencies and unsafe conditions.
- A consistent SQLite backup can be created while the demo app is running.
- Every backup is checked and checksummed before being called valid.
- A broken migration leaves production unchanged.
- A broken candidate application leaves the current application running.
- A successful release updates both application and database.
- A failed pre-traffic production cutover restores both application and database.
- Manual restore warns about possible data loss and backs up the current state first.
- An interrupted deployment can be diagnosed and recovered or safely escalated.
- Release history includes the application version, database backup, checksums, logs, state transitions, and health results.
- Secrets are absent from terminal output and stored logs.
- The full required test suite passes.
- The live demo is deterministic and does not rely on manual state manipulation.
- Documentation states the product's limitations honestly.

A polished dashboard is not part of the definition of done. Verified recovery is.

---

## 17. Scope-cutting rule

When time is short, cut features in this order:

1. Dashboard.
2. Notifications.
3. Schema-diff visualization.
4. Extra storage providers.
5. Extra application runners.
6. Advanced retention policies.

Never cut:

- Backup verification.
- Migration rehearsal.
- Candidate health testing.
- Deployment locking.
- Durable release state.
- Controlled cutover.
- Pre-traffic rollback.
- Manual restore warning.
- Integration tests for failure paths.
- Clear error reporting.

The winning version is a narrow tool that genuinely protects one real deployment setup, not a broad platform whose safety path is simulated.
