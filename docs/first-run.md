# First production setup

This guide is for the supported DployDB deployment shape: one Linux host, one
Docker Compose application service, one SQLite database, one migration command,
and one loopback HTTP health endpoint.

## 1. Establish ownership and prerequisites

Use a dedicated unprivileged account for DployDB. That account must be able to
run the configured Docker Compose service and traffic hooks and must own the
application database, DployDB state directory, and local backup directory.
Docker daemon access is effectively root-equivalent on a conventional Linux
host, so do not grant it to unrelated users.

Install Python 3.12 or newer, `pipx`, Docker Engine, and the Docker Compose
plugin. Verify them before installing DployDB:

```bash
python3 --version  # must report 3.12 or newer
pipx --version
docker version
docker compose version
```

Install the exact published Alpha version into an isolated environment:

```bash
pipx install dploydb==0.1.0
dploydb --no-color version
```

For an offline host, download the wheel and its `SHA256SUMS` attachment from the
matching signed GitHub release, verify the checksum, and pass that reviewed
wheel path to `pipx install`.

## 2. Create and edit the configuration

`init` creates a mode-`0600` starter without overwriting an existing path:

```bash
dploydb init --config /srv/example-app/dploydb.yaml
```

Edit every `/srv/example` placeholder. In particular:

- `database.path` is the one production SQLite file.
- The migration command must take its database path from `database.path_env`.
  It must not embed the production path or perform unrelated side effects.
- The Compose service must use the configured container-side database directory
  and expose its internal health port only through the DployDB-selected host
  port.
- The candidate and production ports must differ and both must be loopback-only.
- All hook and smoke commands are argument arrays, never shell command strings.
- The state and backup directories must be separate from the application
  checkout so application upgrades cannot remove recovery evidence.

The production-shaped [Nginx example](../examples/nginx/README.md) includes a
complete configuration, maintenance marker, and fixed-port target hooks.

## 3. Configure credentials without storing them in YAML

When S3-compatible backup is enabled, place only environment variable names in
the configuration. Supply the values through the service manager or another
runtime secret mechanism. The DployDB account should receive bucket access only
for the configured prefix. Do not put credentials in command arguments,
Compose labels, the configuration, or the repository.

## 4. Prove the host before the first deployment

Start the known-good production application exactly as configured, then run:

```bash
dploydb doctor --config /srv/example-app/dploydb.yaml
dploydb doctor --deep --config /srv/example-app/dploydb.yaml
dploydb backup --upload --config /srv/example-app/dploydb.yaml --json
```

Do not continue until `doctor --deep` passes its implemented checks and the
baseline backup is verified. If remote backup is required, confirm the JSON
result says the remote copy was committed.

## 5. Deploy and retain the result

Use non-interactive JSON in automation and parse the process exit code as well
as the result:

```bash
dploydb deploy --version v2 \
  --config /srv/example-app/dploydb.yaml \
  --json --non-interactive
```

Preserve the result and the referenced event log. `outcome: active` means the
new release passed checks and traffic activation was recorded. `rolled_back`
means the pre-traffic failure restored and verified the previous application
and database. `recovery_required` means automation could not prove a safe
state; run read-only `status` and `recover` diagnosis before changing anything.

Review [security](security.md), [limitations](limitations.md), and
[backup-preserving uninstall](uninstall.md) before production use.
