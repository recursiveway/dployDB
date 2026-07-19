# Backup-preserving uninstall

Removing the Python package must not remove application data, release evidence,
or backups. DployDB has no uninstall hook and `pipx uninstall dploydb` removes
only the isolated Python environment and console entry point.

Before uninstalling:

1. Run `dploydb status --config /absolute/path/dploydb.yaml` and do not uninstall
   while an operation holds the lock or recovery is required.
2. Record the absolute configuration, `state_directory`, database path, local
   backup directory, remote bucket/prefix, active release ID, and previous
   release ID.
3. Copy the configuration and release evidence into the operator's protected
   archive. Verify at least one protected backup locally and, when configured,
   off-server.
4. Stop any scheduler or deployment job that invokes DployDB.

Then remove only the installed CLI:

```bash
pipx uninstall dploydb
```

Do not delete the configured state directory, local backup directory, production
database, remote prefix, or reverse-proxy target state. They are intentionally
outside the package environment and remain usable for audit or a later
reinstallation. Reinstalling the same or a reviewed newer version does not
require moving those paths; run `doctor --deep` before the next operation.

If the application is permanently retired, backup retention and deletion are a
separate operator decision. Confirm legal/operational retention requirements
and perform a restore test before removing any last known-good copy. Package
uninstallation is never authorization to erase data.
