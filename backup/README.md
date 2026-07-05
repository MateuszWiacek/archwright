# backup

Config-driven backup tool with database dumps, service orchestration, restore, and archive rotation. Reads a YAML file, collects files and database dumps, packages them into a compressed archive, and rotates old backups.

Cross-platform (Linux / Windows) · Python 3.10+ · Core dependency: PyYAML.
Optional web dependencies are installed with `.[web]`.

## Quick start

```bash
pip install .

# Backup
archwright backup --config backup-config.yaml
archwright backup --config backup-config.yaml --dry-run
archwright backup --config backup-config.yaml --dry-run --json

# Validate config and runtime prerequisites
archwright validate --config backup-config.yaml
archwright validate --config backup-config.yaml --json

# List archives
archwright list --config backup-config.yaml
archwright list --config backup-config.yaml --json

# Restore
archwright restore --config backup-config.yaml --archive /srv/backups/latest.zip
archwright restore --config backup-config.yaml --archive latest.zip --dry-run --json
archwright restore --config backup-config.yaml --archive latest.zip --only app/config --overwrite

# Module entry point
python -m backup backup --config backup-config.yaml

# Optional web UI
archwright serve --config backup-config.yaml
# or
archwright serve --config-dir /etc/archwright
```

## Project structure

```text
backup/
├── __init__.py        # Package marker
├── __main__.py        # python -m backup entry point
├── constants.py       # Shared constants (exit codes, formats, ZIP settings)
├── models.py          # Data classes (BackupConfig, DatabaseConfig, SubfolderConfig, CollectedFile)
├── config.py          # YAML loading, validation, glob parsing, database config
├── collector.py       # Filesystem walker, symlink cycle detection, file matching
├── archive.py         # ZIP creation, metadata stripping, streamed writes
├── rotation.py        # Old backup deletion (isolated destructive ops)
├── db_dump.py         # Database dump providers (PostgreSQL, Docker PostgreSQL, SQLite) plus staging
├── restore.py         # Archive planning, conflict detection, atomic extraction
├── logging_setup.py   # Logger factory (stdout plus file)
└── cli.py             # Subcommand dispatch (backup/restore/list/validate), service hooks

archwright_web/        # Optional FastAPI web UI, installed with web extras
```

Dependency graph is flat and acyclic. `restore.py` and `db_dump.py` each depend only on `models.py` plus `constants.py`, with no coupling to each other.

## Design decisions

**Database safety**: SQLite backups use `sqlite3 .backup`, not file copy. PostgreSQL uses `pg_dump --format=custom` either directly or via `docker exec` for containerized databases. These are hot dump paths that produce consistent snapshots without stopping services by default.

**Service hooks**: `pre_command` and `post_command` must always be paired. `post_command` is always attempted in a `finally` block, and restart failures are surfaced as errors instead of being silently ignored.

**Validation**: `archwright validate` checks config shape, target directory preflight, source directories, tool availability, and provider-specific runtime prerequisites without creating files or running dumps.

**Machine-readable output**: `archwright list --json`, `archwright validate --json`, backup dry-runs, and restore dry-runs emit structured JSON for automation and web UI control paths.

**Two-phase restore**: `plan_restore()` reads only the ZIP central directory with no filesystem writes. `execute_restore()` uses atomic temp-file-then-rename writes. Path traversal in archive entries is rejected at plan time.

**Metadata stripping**: archive entries carry fixed `0644` permissions. Source file permissions and ownership never leak.

**Streamed writes**: 1 MiB chunked I/O for both backup and restore. Memory usage is constant regardless of file size.

**Atomic archives**: the ZIP is written to `.zip.tmp` first, then renamed. A crash mid-write never leaves a corrupt archive.

**Optional web UI**: `archwright serve` exposes a local FastAPI dashboard for one config or a directory of local configs. It delegates to the same CLI/core behavior and should stay a thin control surface.

For the full YAML reference see [docs/configuration.md](../docs/configuration.md).
For cron, systemd, and Ansible deployment see [docs/deployment.md](../docs/deployment.md).
For Docker usage see [docs/docker.md](../docs/docker.md).
For the module dependency graph see [docs/architecture.md](../docs/architecture.md).
For web UI behavior see [docs/web-ui.md](../docs/web-ui.md).
