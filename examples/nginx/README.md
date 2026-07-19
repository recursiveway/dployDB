# Nginx fixed-port traffic hooks

This example matches DployDB's supported topology: the verified old or new
application occupies the same loopback production port (`4510` here), and Nginx
serves a maintenance response while DployDB stops writers, migrates, verifies,
and chooses the release. The candidate remains isolated on `4511` and is never
an Nginx upstream.

The Nginx `if` in [`site.conf`](site.conf) performs only `return 503`, which is
the safe, deliberately narrow use here. Nginx checks for a maintenance marker
on each request, so enabling or disabling maintenance does not depend on a
reload. The activation hooks atomically record whether the application now
occupying the fixed production port is the old or new release. They refuse to
change that target unless maintenance is already enabled.

## Install the example

Perform these host-administration steps before running DployDB:

1. Copy `site.conf` into the Nginx `http` configuration and replace the domain
   and port. Run `nginx -t`, then reload Nginx through the host's service
   manager.
2. Install `dploydb-hook.py` at `/opt/dploydb/nginx/dploydb-hook.py`, owned by
   root and not writable by the DployDB service account.
3. Create `/var/lib/dploydb/example-app` as mode `0700`, owned by the dedicated
   DployDB account. Create `/run/dploydb` as mode `0755`, with that account able
   to create and remove only the `example-app.maintenance` marker. Use a
   systemd-tmpfiles rule when `/run` is cleared at boot.
4. Copy `dploydb.yaml` to a mode-`0600` path owned by the DployDB account, then
   adapt every example path and command.

Bootstrap the logical target only while maintenance is active:

```bash
python3 /opt/dploydb/nginx/dploydb-hook.py \
  --state-file /var/lib/dploydb/example-app/proxy-target.json \
  --maintenance-file /run/dploydb/example-app.maintenance maintenance-on
python3 /opt/dploydb/nginx/dploydb-hook.py \
  --state-file /var/lib/dploydb/example-app/proxy-target.json \
  --maintenance-file /run/dploydb/example-app.maintenance activate-old
python3 /opt/dploydb/nginx/dploydb-hook.py \
  --state-file /var/lib/dploydb/example-app/proxy-target.json \
  --maintenance-file /run/dploydb/example-app.maintenance maintenance-off
```

Run those commands as the same dedicated account that will execute DployDB.
Each command prints one bounded JSON record. Repeating any action is safe.

## Failure behavior

- If maintenance enablement fails, DployDB does not stop the application.
- Target activation is refused while the marker is absent.
- A symlink, malformed marker, unsafe mode, malformed target record, short
  write, or failed filesystem sync produces a nonzero exit.
- The hook never deletes DployDB state, release evidence, application data, or
  backups.
- `maintenance-off` removes only the exact configured marker and is idempotent.

The example controls traffic for one fixed-port application. It is not a
general blue/green router and must not be changed to send normal traffic to the
candidate validation port.
