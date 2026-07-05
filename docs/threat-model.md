# Threat Model

This document describes the trust assumptions archwright is built on,
who the tool is for, and which security decisions follow from those
assumptions. Read this before filing security issues that turn out to
be intentional design decisions.

## Operator

archwright is a **single-tenant operator tool**. The person who writes
the YAML configs, runs the CLI, and (optionally) accesses the web UI
is the same person, or a small ops team that already has root or
equivalent on the host. There is no notion of unprivileged users,
tenant isolation, or untrusted callers.

Concretely:

- YAML configs are **admin-controlled artifacts**, not user input.
  Anything an attacker could put in a YAML file (shell hooks, database
  credentials, source paths) they could already do directly on the
  host.
- The web UI is a **control plane for the operator**, not a public
  service. There is no per-user auth, RBAC, or session model.
- SSH inventory mode relies on **pre-provisioned trust**: the operator
  prepares `known_hosts` and a dedicated private key before starting
  the container.

If your deployment model has untrusted users writing configs or
hitting the web UI, archwright is not the right tool.

## Deployment expectations

| Surface       | Expected exposure                                     |
| ------------- | ----------------------------------------------------- |
| CLI           | Local invocation by the operator or root cron/systemd |
| Web UI        | `127.0.0.1` or LAN/VPN behind a reverse proxy + auth  |
| SSH inventory | Operator's LAN, dedicated key, prepared `known_hosts` |

The shipped Docker compose example binds to `127.0.0.1` for this
reason. Putting the web UI on the public internet without a
reverse-proxy auth layer (Authentik, oauth2-proxy, etc.) is not a
supported configuration.

## What archwright does defend against

These are real boundaries the code enforces.

- **Path traversal in archive operations.** Archive names taken from
  HTTP form input flow through `archwright_web.services.safe_paths`,
  which canonicalizes with `Path.resolve()` and rejects anything that
  escapes the configured `target_base_dir`. See `safe_paths.py` and
  the path-traversal tests in `tests/test_web_routes.py`.
- **Path traversal during restore.** `backup.restore` rejects archive
  entries with `..` segments, absolute paths, or symlink targets that
  escape the per-section restore target.
- **Symlink cycles during collection.** `backup.collector` tracks
  inodes during traversal so a circular symlink cannot loop the
  walker.
- **Auto-escaped templates.** Jinja2 autoescape is on; user-provided
  archive names and config strings are escaped in the rendered HTML.
- **ZIP metadata sanitization.** `backup.archive` clamps timestamps
  and strips Unix mode bits when writing entries, so a hostile archive
  layout cannot produce extraction surprises on a different platform.

## Documented risks accepted by the trust model

These are **not bugs**. They are consequences of the single-tenant,
admin-controlled trust model. Changing them would only make sense if
the trust model itself changed.

### Shell execution of configured hooks

`pre_command` and `post_command` from the YAML run via
`subprocess.run(..., shell=True)` (`backup/orchestrator.py`,
`backup/db_dump.py`). The risk is "operator who can edit the YAML can
run arbitrary shell commands as the archwright process," which is the
same privilege level the operator already has when they run
`archwright` at all.

Under a different trust model (untrusted YAML), this would be a
remote-code-execution path. Under this trust model, it is the feature.

### No CSRF tokens on the web UI

POST endpoints in the web UI do not carry CSRF tokens. With the
deployment model above (`127.0.0.1` or LAN behind an auth proxy), CSRF
requires either a malicious page rendered by the same operator's
browser or a cross-origin request that bypasses the auth proxy.
Neither is in scope.

If you front archwright with a reverse proxy that supports CSRF
protection (e.g. Authentik forward auth with a SameSite-cookie session)
that already covers the surface. Adding token-based CSRF inside
archwright on top of that is duplication.

### Database credentials in YAML

PostgreSQL passwords appear as fields in the YAML config. The config
file is expected to be readable only by the archwright operator and
root (mode `600` or `640` with a dedicated group). The same applies
to inventory keys.

archwright passes passwords to `pg_dump` via the `PGPASSWORD`
environment variable, never on the command line, so they do not
appear in `ps` listings. They can still be visible to `root` through
`/proc/<pid>/environ`; `root` is part of the trust boundary.

### Shell command logged at DEBUG level

`backup.db_dump` logs the full `pg_dump` argv at DEBUG level. The argv
intentionally does not contain the password (see above), but it does
contain hostnames, ports, usernames, and database names. Operators who
ship logs to a central system should keep the default INFO log level
or scrub these fields downstream.

### Singleton job runner with no auth

`archwright_web/services/job_runner.py` is a process-wide singleton
holding a `threading.Lock`. Anyone who can reach the web UI can
trigger a backup, validate, or restore. This is the same surface as
"can run the CLI on this host" and follows the same trust model.

The runner refuses to start a second job while one is running. It
does not implement a global timeout for local jobs; long-running local
file collection and archive creation are an operator concern. Configured
hooks and database dumps have their own timeouts. SSH command execution
has subprocess timeouts because a wedged remote process would otherwise
hold the process-wide job lock.

## Out of scope

archwright does not try to defend against:

- Hostile YAML configs
- Hostile users on the host running the daemon
- A reverse proxy or VPN that has been compromised
- Side channels in the host's crypto, kernel, or container runtime
- Supply-chain compromise of `pg_dump`, `sqlite3`, `docker`, or
  archwright's own dependencies
- Resource exhaustion from a misconfigured backup (huge `include`
  patterns, unbounded archive sizes); the operator owns that

## Reporting a finding

If you believe a finding falls **outside** the trust model documented
above, open a regular GitHub issue. If it falls **inside** the trust
model but you can construct a real-world deployment where it does not
hold, open an issue with the deployment topology described. That is
the more interesting conversation than the bug itself.
