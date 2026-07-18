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
- **Next slice:** Milestone 4B — bounded HTTP readiness and optional smoke checks.
- **Planned Milestone 4 slices:** 4A isolated Docker Compose runner, 4B bounded
  HTTP readiness and optional smoke checks, and 4C durable candidate-validation
  orchestration plus the real old-application-continuity gate.
- **Not yet implemented:** Candidate orchestration, cutover, application rollback,
  public manual restore, crash recovery, remote storage, and retention from
  Milestone 4 onward.
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

- [ ] Milestone 4 overall — complete only after slices 4A through 4C and the
  final real-Docker continuity/cleanup gate pass.
  - [x] 4A — isolated Docker Compose candidate runner and lifecycle evidence
    (`COMPLETE` on 2026-07-18).
  - [ ] 4B — bounded HTTP readiness, optional smoke command, and health evidence.
  - [ ] 4C — durable rehearsal-plus-candidate orchestration and Milestone 4 gate.

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

##### 4C — Durable candidate validation and final Milestone 4 gate

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
