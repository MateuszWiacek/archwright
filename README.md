# archwright

![archwright banner](assets/archwright-banner.svg)

[![CI](https://github.com/MateuszWiacek/archwright/actions/workflows/ci.yml/badge.svg)](https://github.com/MateuszWiacek/archwright/actions/workflows/ci.yml)

Config-driven backup and restore for self-hosted systems.

`archwright` backs up files, SQLite databases, and PostgreSQL databases into a single ZIP archive with rotation, restore, and service orchestration, all from one YAML config.

Built for homelabs and self-hosted stacks where Vaultwarden, Immich, Paperless-ngx, and other services need different backup steps, but you still want one tool to drive them.

## What makes this different

Most backup tools handle files and leave database dumps, service control, and restore to wrapper scripts. `archwright` keeps that flow in one config:

```yaml
backup_name: "vaultwarden"
target_base_dir: "/srv/backups/vaultwarden"
keep_last: 7

structure:
  vaultwarden:
    config:
      source_dir: "/opt/vaultwarden/data"
      include: "*.json"
    attachments:
      source_dir: "/opt/vaultwarden/data/attachments"
      include: "*"
      pre_command: "docker stop vaultwarden"
      post_command: "docker start vaultwarden"

databases:
  vaultwarden_db:
    provider: "sqlite"
    db_path: "/opt/vaultwarden/data/db.sqlite3"
```

This config snapshots the SQLite database with `sqlite3 .backup`, stops the container before copying attachments, always attempts the restart, and packages everything into a rotated ZIP archive. It can run from cron, systemd, the CLI, or the optional web UI.

## Quick start

```bash
pip install .

# Create a backup
archwright backup --config myapp.yaml

# Preview without touching anything
archwright backup --config myapp.yaml --dry-run

# Preview with JSON output for automation
archwright backup --config myapp.yaml --dry-run --json

# Validate config and runtime prerequisites
archwright validate --config myapp.yaml

# Validate with JSON output for automation
archwright validate --config myapp.yaml --json

# List available archives
archwright list --config myapp.yaml

# List archives with JSON output for automation
archwright list --config myapp.yaml --json

# Restore from an archive
archwright restore --config myapp.yaml --archive /srv/backups/myapp_archive.zip

# Restore only specific sections
archwright restore --config myapp.yaml --archive backup.zip --only vaultwarden/config

# Preview a restore with JSON output for automation
archwright restore --config myapp.yaml --archive backup.zip --dry-run --json

# Force overwrite existing files
archwright restore --config myapp.yaml --archive backup.zip --overwrite

# Module entry point
python -m backup backup --config myapp.yaml
```

## Optional Web UI

`archwright` includes an optional FastAPI web UI. It is a thin layer over the existing CLI and core modules, not a replacement for them.

```bash
pip install ".[web]"
archwright serve --config myapp.yaml
# or
archwright serve --config-dir /etc/archwright
# or experimental local-node inventory mode
archwright serve --inventory inventory.yaml
```

See [inventory.example.yaml](inventory.example.yaml) for the local/SSH node inventory format.

Docker is also supported for the web UI:

```bash
docker run --rm \
  -p 127.0.0.1:8471:8471 \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local
```

Then open:

```text
http://127.0.0.1:8471
```

The current UI supports one config, a local config directory, or an
experimental inventory with SSH node control. It can show a dashboard,
switch between config jobs, browse archives and logs locally, trigger
backup and validate runs, run remote validate/dry-run/live backup/live
restore over SSH, and guide restore through a guarded wizard.

## Features

**File collection**: recursive scanning with glob include/exclude patterns, brace expansion (`*.{log,txt}`), symlink-safe walking with cycle detection, and atomic archive writes via temp file plus rename.

**Database dumps**: PostgreSQL via `pg_dump` (custom format) or `docker exec pg_dump` (for containerized databases without exposed ports), SQLite via `sqlite3 .backup` (safe hot copy). Dumps run before file collection so they capture a consistent snapshot.

**Service hooks**: `pre_command` and `post_command` on file sections for stopping and restarting containers. `stop_command` and `start_command` on database sections for cold dumps. Restarts are always attempted in reverse order, and restart failures make the run fail explicitly.

**Restore**: two-phase pipeline with planning, selective restore by prefix, conflict detection, overwrite control, dry-run support, and path traversal rejection.

**Rotation**: keep the N most recent archives, delete oldest pairs (`.zip` plus `.log`). Lexicographic sort on timestamps equals chronological order.

**Web UI**: optional local web interface for archive browsing, log viewing, backup/validate triggers, and guarded restore. The CLI remains the source of truth.

**Automation output**: `list`, `validate`, backup dry-runs, and restore dry-runs can emit JSON with `--json`, which is useful for the web UI, scripts, and SSH inventory execution.

## Safety guarantees

- `pre_command` always has a matching `post_command`; config validation rejects orphaned hooks.
- `post_command` is always attempted in a `finally` block, and a failed restart returns an error instead of reporting a false success.
- SQLite backups use `.backup`, not file copy, so live databases are copied safely.
- Archive writes use a `.zip.tmp` intermediate, so a crash never leaves a partial `.zip`.
- Restore validates path traversal; a malicious archive cannot write outside target directories.
- Config validation rejects `backup_name` with path separators or glob metacharacters.
- Memory usage is constant, with 1 MiB streamed chunks regardless of file sizes.
- Source permissions never leak into the archive; every entry is written with fixed `0644` metadata.

## Documentation

| Document | Contents |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Full YAML reference: structure, databases, hooks, validation rules |
| [docs/architecture.md](docs/architecture.md) | Module map, dependency graph, data flow, error strategy |
| [docs/deployment.md](docs/deployment.md) | CLI, web UI, cron, systemd, Ansible, monitoring, restore runbook |
| [docs/docker.md](docs/docker.md) | Docker image usage, mount model, Docker socket notes |
| [docs/web-ui.md](docs/web-ui.md) | Current web UI scope, security model, and local usage |
| [docs/threat-model.md](docs/threat-model.md) | Trust assumptions, defended boundaries, accepted risks |
| [docs/control-plane-roadmap.md](docs/control-plane-roadmap.md) | Planned multi-config and multi-node control model |
| [CHANGELOG.md](CHANGELOG.md) | Version history and decision log |
| [backup/README.md](backup/README.md) | Package-level technical reference |

## License

Copyright (c) 2026 Mateusz Wiacek.

archwright is licensed under the GNU Affero General Public License,
version 3 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the
full text.

In short: you can use, modify, and redistribute archwright freely, but
if you run a modified version as a network service, you must make your
modified source available to its users.

## Running tests

```bash
pip install pytest pyyaml ruff
pip install ".[web]"
pytest -q
python -m ruff check backup/ archwright_web/ tests/
pytest tests/ -m unit
pytest tests/ -m integration
pytest tests/ -m e2e
pytest tests/ -m edge
pytest tests/ -m restore
pytest tests/ -m web
```

## Requirements

Python 3.10+ and [PyYAML](https://pypi.org/project/PyYAML/).

Optional web dependencies are installed with:

```bash
pip install ".[web]"
```

Optional runtime tools:
- `sqlite3` for SQLite dumps
- `pg_dump` for PostgreSQL dumps (direct connection)
- `docker` for Docker exec PostgreSQL dumps (containerized databases)
