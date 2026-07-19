# Changelog

All notable changes to DployDB are documented here. The project follows
[Semantic Versioning](https://semver.org/) and keeps this file in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-07-19

### Added

- Verified SQLite online backups with checksums and integrity checks.
- Disposable migration rehearsal that never targets the production database.
- Isolated Docker Compose candidate validation with bounded health and smoke checks.
- Controlled production cutover with automatic application and database rollback
  before traffic activation.
- Durable operation/release state, release history, backup-first manual restore,
  and interrupted-operation recovery.
- Optional verified S3-compatible off-server backup and retention protection.
- Stable human and JSON CLI output, secret redaction, timeouts, and a durable
  operating-system-backed deployment lock.
- Clean-Linux installation and deterministic real deployment demo.

### Known limitations

- Alpha support is limited to one Linux VPS, one Docker Compose application
  service, one SQLite database, command-based traffic hooks, and one HTTP health
  endpoint.
- Automatic database rollback ends when new production traffic is activated.
- DployDB does not sandbox developer-supplied migrations or hook commands.
- During `0.x`, breaking public-contract changes may occur only in minor releases
  and will include explicit migration guidance.

[Unreleased]: https://github.com/recursiveway/dployDB/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/recursiveway/dployDB/releases/tag/v0.1.0
