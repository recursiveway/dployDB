# Security model

DployDB reduces deployment and recovery risk; it is not a sandbox for code or a
host-hardening product. Its trust boundary includes the dedicated DployDB
account, Docker daemon, configured migration/smoke/traffic commands, Compose
file, application image, database directory, state directory, and backup
storage credentials.

## Host and process permissions

- Run DployDB as a dedicated unprivileged account. Do not run the CLI from a web
  request handler or expose it as a network service.
- Treat Docker daemon membership as root-equivalent. Protect the account,
  socket, Compose file, images, and release inputs accordingly.
- Make the configuration mode `0600`; state and local backup roots must be mode
  `0700`; DployDB-managed manifests, logs, and backup files are mode `0600`.
- Keep migration, smoke, and traffic-hook programs owned by an administrator
  and non-writable by the application or untrusted release contents.
- Use absolute paths. Do not place state or backups inside a checkout that a
  deployment replaces.

## Secrets

DployDB resolves named environment variables at runtime, registers sensitive
values only in memory, and redacts them before terminal, JSON, manifest, event,
and command-output boundaries. That protection cannot remove a secret that an
external program writes directly to an unrelated file or external logging
system.

Use short-lived or narrowly scoped S3-compatible credentials where possible.
Restrict them to the selected bucket and prefix. Do not put credentials in
YAML, command arguments, URLs, Compose labels, release metadata, or shell
history. Protect any service-manager environment file as a secret.

## Network and application checks

- Candidate and production health endpoints are loopback HTTP endpoints. TLS
  termination belongs at the reverse proxy.
- Bind both application ports to `127.0.0.1`; never expose the candidate port to
  normal users.
- A health endpoint must prove the application can use the selected SQLite
  database, not merely that a process is listening.
- The optional smoke test is trusted code and receives a bounded runtime and
  output budget; it is not isolated from the host by DployDB.

## Evidence and backups

Release manifests and event logs are recovery evidence. Do not edit them to
repair a failed deployment. Unknown, contradictory, over-limit, truncated, or
unsafe evidence becomes `recovery_required`.

Local backups protect against deployment failures but not total host loss.
Enable required off-server backup for important data, encrypt the storage and
transport according to the provider's controls, test credential rotation, and
periodically perform a verified restore exercise.
