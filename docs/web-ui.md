# Web UI

`archwright serve` starts an optional local web UI for one config file or a directory of configs.

The web UI is a control surface over the existing CLI and core modules. It does not replace the CLI, and it should not become a separate backup implementation.

## Install

```bash
pip install ".[web]"
```

## Run

```bash
archwright serve --config /etc/archwright/myapp.yaml
```

Multiple local configs:

```bash
archwright serve --config-dir /etc/archwright
```

Experimental inventory mode:

```bash
archwright serve --inventory inventory.yaml
```

Inventory mode discovers `local` nodes and SSH node config files. SSH nodes
currently support archive listing, validate, backup dry-run, live backup,
restore dry-run planning, and guarded live restore.

Docker is supported for web UI deployment:

```bash
docker run --rm \
  -p 127.0.0.1:8471:8471 \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local
```

See [docker.md](docker.md) for mount and Docker socket details.

Default address:

```text
http://127.0.0.1:8471
```

LAN binding is explicit:

```bash
archwright serve \
  --config-dir /etc/archwright \
  --host 0.0.0.0 \
  --port 8471
```

## Current Scope

The current UI is single-process. It can run one config via `--config` or discover multiple local YAML configs via `--config-dir`.

It supports:

- dashboard for the selected config job
- config switcher for local YAML files
- archive listing and ZIP download scoped to the selected job
- safe ZIP entry inspection
- log viewing
- config preview and YAML export
- glob preview for file collection rules
- backup, dry-run, and validate triggers
- guarded restore wizard with plan, conflicts, overwrite choice, and `RESTORE` confirmation

The current UI does not yet support:

- built-in authentication
- scheduling management
- editing production configs in place
- remote ZIP download, ZIP entry inspection, or log viewing

Inventory mode supports local nodes and SSH-backed control actions. Remote
write actions run through the same process-local job lock as local backup and
restore.

## Source of Truth

The YAML config remains the source of truth.

The UI may render, preview, or export config data, but production config changes should still happen through Git-managed YAML files. This keeps backup behavior reproducible and reviewable.

## Execution Model

`archwright serve --config myapp.yaml` loads one config path at startup.

`archwright serve --config-dir /etc/archwright` discovers `*.yaml` and `*.yml` files, assigns stable job ids from filenames, and scopes dashboard/archive/log/restore routes with `?job=<id>`.

`archwright serve --inventory inventory.yaml` discovers local config directories directly. For SSH nodes it runs a remote `find` against the configured `config_dir`, then uses the same job-id format: `<node_id>:<config_stem>`.

The web routes then call core behavior:

```text
POST /jobs/backup     -> backup.orchestrator.run()
POST /jobs/validate   -> backup.orchestrator.run_validate()
POST /restore/execute -> restore_orchestrator -> backup.restore
```

Archive listing goes through the local JSON executor:

```text
GET /archives/        -> run_list(json_output=True)
GET /                 -> run_list(json_output=True) for dashboard archive stats
```

This is an in-process Python call, not a spawned `archwright` subprocess. It uses the same JSON contract that the SSH executor uses through the CLI.

The local executor also exposes JSON backup and restore dry-runs. SSH inventory
jobs use the SSH executor for archive listing, validate, backup dry-run, live
backup, restore dry-run planning, and live restore. These calls run `archwright`
on the remote node and parse output in the web process.

Job execution happens in a background thread through `JobRunner`. A process-local lock prevents concurrent web-triggered jobs. With `--config-dir`, this is one lock for the whole web process, not one lock per config job.

Remote validate, backup dry-run, and restore dry-run planning are immediate
read-only calls. Remote live backup and remote live restore run as background
jobs through `JobRunner` with streaming logs.

## SSH Inventory Notes

SSH execution intentionally relies on normal OpenSSH behavior:

- each SSH host must already be trusted in `~/.ssh/known_hosts` for the user running `archwright serve`
- run `ssh user@host` once manually before adding a host to inventory
- `BatchMode=yes` is used, so password prompts fail fast instead of hanging the web UI
- key identity comes from `~/.ssh/config`, default SSH keys, or the SSH agent visible to the `archwright serve` process
- if the UI runs under systemd, the service user needs access to the key or agent

## Security Model

Default bind is `127.0.0.1`.

For LAN use, put the UI behind a trusted reverse proxy or access it over a private network. The UI can trigger backup and restore actions, so it should be treated as an administrative interface.

Important rules:

- expose it only to trusted users
- keep configs trusted and Git-managed
- keep credentials in config files protected with filesystem permissions
- use POST for state-changing actions
- validate archive and log filenames before reading from disk
- do not pass arbitrary shell input from the browser

## Restore UI

Restore stays intentionally guarded:

1. choose archive
2. inspect restore plan
3. optionally filter prefixes
4. review conflicts
5. choose overwrite behavior
6. type `RESTORE`
7. execute through the same restore core used by the CLI

For SSH inventory jobs, the wizard first builds a remote dry-run plan, then
executes the restore on the selected node only after the same `RESTORE`
confirmation used for local restores.

The wizard exists to reduce accidents. It should not become a single-click destructive action.

## Testing

Relevant checks:

```bash
pytest tests/test_web_routes.py
pytest tests/test_web_services.py
ruff check archwright_web tests
```

Full project checks:

```bash
pytest -q
ruff check backup archwright_web tests
```
