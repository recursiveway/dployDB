# Supported scope and limitations

DployDB 0.1 supports exactly one Linux host, one Docker Compose application
service, one SQLite database file, one developer-supplied migration command,
one loopback HTTP health endpoint, local verified backups, optional
S3-compatible backup, and command-based maintenance/traffic hooks.

It does not provide:

- Kubernetes, multi-host orchestration, database replication, or point-in-time
  recovery;
- Windows or macOS production support;
- multiple SQLite databases or coordinated external datastore migrations;
- a migration language, migration generation, or sandboxing of developer code;
- universal zero-downtime migrations, background data backfills, or schema
  compatibility analysis;
- authentication, billing, teams, multi-tenancy, or a state-mutating dashboard;
- automatic recovery when routing, container identity, backup lineage, or
  durable evidence is contradictory.

## Rollback boundary

Automatic database rollback is allowed only before DployDB records that the new
release may have received production traffic. Before that point, a failed
cutover restores the final verified backup, restarts the exact previous
application, restores the old traffic target, and proves health.

After traffic activation, automatically restoring the old database could erase
writes accepted by the new release. DployDB therefore reports uncertain or
post-activation failures as `recovery_required`. A human must diagnose routing
and data ownership. Manual restore is limited to the protected previous release,
warns about possible data loss, and backs up the current database first, but it
can still discard writes made after the selected backup.

## Operational limitations

The application must tolerate a controlled maintenance period. All background
writers must stop with the configured application; DployDB cannot discover an
unrelated process writing directly to the SQLite file. SQLite files on unusual
network filesystems, Docker socket proxies, rootless Docker variations, custom
Compose behavior, and S3-compatible providers outside the tested API subset
require their own acceptance testing.

Retention protects backups referenced by the active and immediately previous
release. It is not a legal hold or archival policy. Off-server storage protects
against host loss only when upload is enabled, verified, and monitored.
