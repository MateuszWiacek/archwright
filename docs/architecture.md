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
└── cli.py               Subcommand dispatch, pipeline orchestration, service hooks
```

## Dependency graph

```text
Two independent roots (no mutual imports):

  constants.py    Shared by: config, archive, db_dump, restore, cli, logging_setup
  models.py       Shared by: config, archive, collector, db_dump, restore, cli

Middle layer (import only from the roots above):

  config.py       <- models, constants
  logging_setup   <- constants
  collector.py    <- models, config (parse_glob)
  archive.py      <- models, constants
  db_dump.py      <- models, constants
  restore.py      <- models, constants
  rotation.py     <- (stdlib only)

Top layer (orchestrator):

  cli.py          <- all of the above
```

Key property: the graph is acyclic and flat. `cli.py` is the only module that imports broadly. `restore.py` and `db_dump.py` do not depend on each other.

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

Service hook orchestration also lives here. `_collect_service_hooks()` deduplicates hooks, `_run_hook()` executes them, and the `finally` block guarantees restart attempts.

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

Markers in `pytest`: `unit`, `integration`, `e2e`, `edge`, `restore`.
