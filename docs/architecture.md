# Architecture

## Module map

```text
backup/
├── __init__.py          Package marker
├── __main__.py          python -m backup entry point
│
├── constants.py         Shared constants (exit codes, formats, ZIP settings)
├── models.py            Data classes, safe dependency root
│
├── config.py            YAML loading, validation, glob parsing, database config
├── logging_setup.py     Logger factory (stdout + optional file)
│
├── collector.py         Filesystem walker, symlink cycle detection, file matching
├── archive.py           ZIP creation, metadata stripping, streamed writes
├── rotation.py          Old backup deletion
│
├── db_dump.py           Database dump providers (PostgreSQL, Docker PostgreSQL, SQLite) + staging
├── restore.py           Archive planning, conflict detection, atomic extraction
│
├── json_output.py       JSON payload formatters for --json output
├── orchestrator.py      Pipeline workflows (run, run_restore, run_list, run_validate) + service hooks
└── cli.py               Argparse, subcommand dispatch, main entry point

archwright_web/
├── app.py               FastAPI app factory and router registration
├── server.py            Uvicorn launcher used by archwright serve
│
├── routers/             HTTP route handlers
│   ├── dashboard.py     Config/job dashboard
│   ├── jobs.py          Backup, dry-run, validate, status, live logs
│   ├── archives.py      Archive listing, download, ZIP entry inspection
│   ├── logs.py          Log file viewer
│   ├── restore.py       Guarded restore wizard
│   ├── config_editor.py Config preview and YAML export
│   └── preview.py       Glob preview endpoint
│
├── services/            Web-facing service layer
│   ├── archive_service.py
│   ├── collector_service.py
│   ├── config_service.py
│   └── job_runner.py
│
├── templates/           Jinja2 templates
└── static/              Static assets
```

## Dependency graph

```text
Two independent roots (no mutual imports):

  constants.py    Shared by: config, archive, db_dump, restore, orchestrator, logging_setup
  models.py       Shared by: config, archive, collector, db_dump, restore, orchestrator, json_output

Middle layer (import only from the roots above):

  config.py       <- models, constants
  logging_setup   <- constants
  collector.py    <- models, config (parse_glob)
  archive.py      <- models, constants
  db_dump.py      <- models, constants
  restore.py      <- models, constants
  rotation.py     <- (stdlib only)
  json_output.py  <- models (pure formatting, no pipeline)

Pipeline layer:

  orchestrator.py <- all middle-layer modules + json_output

Entry point:

  cli.py          <- orchestrator, constants

Optional web layer:

  archwright_web.app       <- FastAPI, Jinja2, routers
  archwright_web.routers   <- services
  archwright_web.services  <- backup.orchestrator (lazy), config/archive/restore/collector helpers
```

Key property: the core graph is acyclic and flat. `orchestrator.py` is the only module that imports broadly across the pipeline. `cli.py` is a thin shell over the orchestrator. The web layer depends on the core through `backup.orchestrator`, but the core does not depend on the web layer.

## Data flow: backup

```text
Phase 0  - load_config()           YAML file -> BackupConfig
Phase 1  - _ensure_target_dir()    Create target dir if missing
Phase 2  - validate_source_dirs()  Stat every source_dir
Phase 3  - run_dumps()             DatabaseConfig[] -> [CollectedFile] (staged via pg_dump/sqlite3/docker exec)
Phase 3b - pre_commands            Deduplicated service stop (config order)
Phase 4  - collect_files()         Walk filesystems -> [CollectedFile]
Phase 5  - create_archive()        [CollectedFile] -> .zip (atomic)
         - post_commands           Service restart (reverse order, finally)
Phase 6  - rotate_backups()        Delete old .zip + .log pairs
```

If any phase fails, the pipeline returns `EXIT_ERROR`. Database dumps run before file hooks because `pg_dump` and `sqlite3 .backup` are hot dump tools that usually do not need service interruption.

## Data flow: validate

```text
Phase 0  - load_config()                    YAML file -> BackupConfig
Phase 1  - _validate_target_dir_preflight() Check parent dir writable
Phase 2  - validate_source_dirs()           Stat every source_dir
Phase 3  - validate_dump_prerequisites()    Tool on PATH + provider.validate()
```

Read-only -- no files created, no dumps run, no service hooks executed.

## Data flow: restore

```text
plan_restore()     ZIP central directory + config -> [RestoreEntry]
execute_restore()  [RestoreEntry] + ZIP data -> extracted files (atomic)
```

Restore is intentionally two-phase. `plan_restore()` has no filesystem side effects. `execute_restore()` performs the writes. This separation enables dry-run, conflict detection, and selective prefix filtering.

## Data flow: web UI

```text
archwright serve --config myapp.yaml
archwright serve --config-dir /etc/archwright
archwright serve --inventory inventory.yaml
  -> archwright_web.server.run_server()
  -> archwright_web.app.create_app()
  -> config registry selects one config job
  -> dashboard / archives / logs / config / jobs / restore routers
```

The web UI accepts one config path, a config directory, or an inventory file. Directory mode discovers local `*.yaml` / `*.yml` files. Inventory mode discovers local node config directories and SSH node config files. Stable job ids are built from filenames and scoped with `?job=<id>`.

Local backup and validation actions use `JobRunner`, which calls the existing CLI-level functions in a background thread:

```text
POST /jobs/backup    -> JobRunner.run_backup()    -> backup.orchestrator.run()
POST /jobs/validate  -> JobRunner.run_validate()  -> backup.orchestrator.run_validate()
POST /restore/execute -> restore_orchestrator -> JobRunner.run_restore()
```

SSH inventory jobs use `SSHExecutor` for archive listing, validate, backup
dry-run, remote live backup, restore dry-run planning, and remote live restore.
Remote live backup and remote live restore both run through `JobRunner` with streaming log capture.

`JobRunner` currently uses one process-local lock, so only one web-triggered job runs at a time. This is deliberate for the first GUI version.

Local archive and log views read from the configured `target_base_dir`. Routes validate filenames before joining paths to avoid traversal outside the backup directory. Remote archive lists come from `archwright list --json`; remote ZIP download, entry inspection, and log viewing are not wired yet.

## Module responsibilities

### models.py

Data contracts shared across the package:

- `SubfolderConfig`: one entry from the YAML `structure` block, including optional `pre_command` and `post_command`.
- `DatabaseConfig`: one entry from the `databases` block, with provider-specific fields and optional `stop_command` and `start_command`.
- `BackupConfig`: validated top-level config.
- `CollectedFile`: source path plus target archive path pair.

This module intentionally imports nothing from the rest of the package, which keeps it safe as the dependency root.

### config.py

The only module that imports `yaml`. Responsibilities:

- parse YAML into typed config objects
- validate types and required fields
- enforce semantic rules such as safe `backup_name`, `keep_last >= 0`, and safe archive path segments
- perform provider-specific database validation
- validate hook pairs (`pre_command` with `post_command`, `stop_command` with `start_command`)
- expand brace globs like `*.{log,txt}`

### db_dump.py

Provider-based database dump system:

- `DatabaseProvider`: abstract interface with `detect()`, `validate()`, `pre_backup()`, `dump()`, `plan_dump_paths()`, and `post_backup()`
- `PostgresProvider`: `pg_dump --format=custom` (direct connection)
- `DockerPostgresProvider`: `docker exec <container> pg_dump` (streaming stdout to local file)
- `SqliteProvider`: `sqlite3 .backup`
- `run_dumps()`: staging orchestration plus conversion to `CollectedFile`
- `validate_dump_prerequisites()`: tool availability + provider-specific reachability checks

Staging directories are created under `target_base_dir` with a `.db_staging_` prefix and are cleaned up after archiving or on failure.

### restore.py

Archive extraction with safety checks:

- `_build_prefix_map()`: reverse mapping from `folder/subfolder` to target directory
- `plan_restore()`: read ZIP central directory, resolve entries, reject path traversal, optionally filter by prefix
- `execute_restore()`: extract files with atomic writes, conflict detection, and overwrite control

### cli.py

The orchestrator and command dispatcher:

- `run()`: backup workflow
- `run_restore()`: restore workflow
- `run_list()`: archive listing
- `run_validate()`: config + runtime prerequisite validation (no side effects)
- `serve`: optional web UI launcher

Service hook orchestration also lives here. `_collect_service_hooks()` deduplicates hooks, `_run_hook()` executes them, and the `finally` block guarantees restart attempts.

### archwright_web

Optional web control surface:

- dashboard for one config or multiple local config jobs
- experimental inventory mode with SSH control actions
- archive and log browser
- backup, dry-run, and validate triggers
- guarded restore wizard
- config preview and YAML export

The web layer is not the source of truth. The CLI and YAML config remain the source of truth. The web layer should keep delegating to core modules or CLI-level orchestration instead of duplicating backup behavior.

## Error handling strategy

**Hard stop**:
- config validation failures
- missing or invalid source directories
- archive path collisions
- target directory creation failures
- archive write failures
- database dump failures
- failed `pre_command`
- failed `post_command` restart attempts

**Soft skip**:
- unresolvable symlinks
- permission-denied directories
- symlink cycles

**Always attempted**:
- `post_command` restarts
- staging directory cleanup

## Testability

The flat module graph makes isolated testing straightforward:

- Unit: config validators, glob parsing, ZIP metadata helpers, restore planning helpers
- Integration: file collection, rotation, `PostgresProvider`, `DockerPostgresProvider`, `SqliteProvider`, `run_dumps()`
- E2E: full backup workflow, CLI argument parsing, hook failure behavior, restore pipeline pieces
- Edge: corrupt archives, dry-run semantics, cleanup on error, path traversal, collisions
- Web: FastAPI route behavior, archive/log safety, job runner locking, restore wizard flow

Markers in `pytest`: `unit`, `integration`, `e2e`, `edge`, `restore`, `web`.
