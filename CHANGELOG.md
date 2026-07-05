# Changelog

All notable changes to archwright are documented here.

From `1.0.0` onward, archwright follows Semantic Versioning.

> **About this repository.** This public repository is a curated mirror.
> Active development, issue tracking, and the full commit history live in
> a private repository; releases here are published as reviewed
> snapshots. This changelog is the canonical record of what changed
> between those snapshots, grouped by theme rather than mapped
> one-to-one onto individual commits.

## [Unreleased]

This entry covers the web UI and multi-node control work, plus a round
of supply-chain and CVE hardening, layered on top of the 1.0 CLI.

### Added

- **Web UI** (`archwright serve`):
  - FastAPI dashboard for a single config or a local config directory
  - config switcher for `--config-dir`
  - archive and log browser with in-place ZIP entry inspection
  - backup, dry-run, and validate triggers
  - guarded restore wizard (plan, prefix filter, conflict review, typed
    confirmation, streamed result)
  - dark "blueprint" theme

- **Multi-node inventory and remote control**:
  - inventory parser and validation for local and SSH nodes
  - `archwright serve --inventory`
  - SSH executor adapter for remote JSON commands, with an example
    inventory file
  - SSH-backed config discovery, archive listing, validate, backup
    dry-run, live backup, restore dry-run planning, and live restore
    execution surfaced in the web UI

- **Machine-readable CLI output**:
  - `--json` for `list`, `validate`, `backup --dry-run`, and
    `restore --dry-run`
  - a local JSON executor so the web UI drives local and SSH jobs
    through the same `list --json` contract

- **Docker packaging**:
  - Dockerfile for the CLI/web UI runtime, base image pinned by digest
  - compose example for local web UI deployment
  - Docker docs covering mounts, permissions, and Docker socket usage

- **Security and supply chain**:
  - `docs/threat-model.md`: trust model, defended boundaries, and the
    risks the single-tenant design intentionally accepts
  - GitHub Actions pinned to commit SHAs
  - hash-pinned lockfile installed with `--require-hashes` in CI and
    Docker
  - `bandit` and `pip-audit` CI jobs, plus a `lock-fresh` job that
    fails if the lockfile drifts from `pyproject.toml`

- **License**: AGPL-3.0-or-later.

- **Web test coverage**: route tests (dashboard, config preview,
  archives, logs, jobs, restore) and service tests (config discovery,
  serialization, archive streaming, glob preview, job locking, SSH
  executor).

### Changed

- **CLI internals refactored** with no change in behavior:
  - the 1100-line `cli.py` split into `orchestrator.py` (pipelines),
    `json_output.py` (formatters), and a thin argparse dispatcher
  - restore route logic extracted into a `RestoreOrchestrator` service;
    local-vs-remote job dispatch deduplicated behind one helper
- **Async responsiveness**: blocking executor calls (SSH, in-process
  CLI, archive listing) offloaded to worker threads so web routes no
  longer stall the event loop.
- **Minimum Python raised** from 3.8 to 3.10 (3.8/3.9 end of life; the
  `python-multipart` CVE fix requires 3.10).
- **Documentation**: web UI usage, the multi-config/multi-node
  control-plane roadmap, and `archwright serve` deployment are now
  documented explicitly.

### Fixed

- **Dependency CVEs**: bumped `starlette`, `python-multipart`, and
  `python-dotenv` past disclosed advisories; the pinned set is clean
  under `pip-audit`.
- **Error handling**: a global handler maps input `ValueError`s to HTTP
  400 instead of 500, replacing repeated per-route try/except blocks.
- **Temp-file race**: config serialization no longer uses the
  deprecated `tempfile.mktemp()` (TOCTOU, CWE-377).
- **Remote JSON cap**: the SSH executor refuses to parse responses over
  16 MiB, so a misbehaving node cannot exhaust host memory.
- **Web restore filtering**: selected prefixes in the plan now match the
  prefixes sent to confirm and execute.
- **Web path safety**: log viewing rejects traversal outside the
  configured backup directory.
- **Web archive handling**: bad ZIP files return a controlled error
  instead of crashing the server.
- **Web archive listing errors**: listing failures surface to the UI
  instead of looking like an empty backup set.
- **Restore defense in depth**: background restore validates archive
  filenames before joining paths.
- **Download headers**: ZIP entry downloads use quoted
  `Content-Disposition` headers.
- **SSH executor defaults**: remote commands use `BatchMode=yes` and a
  bounded connect timeout.

## [1.0.0]

### Added

- **Validation and runtime controls**:
  - `archwright validate`
  - `--verbose` / `--quiet`
  - `log_level`, `hook_timeout`, `dump_timeout`

- **Stronger database validation**:
  - provider-level runtime prerequisite checks during `validate`
  - password-aware PostgreSQL reachability checks via `pg_isready`

- **Streaming Docker PostgreSQL dumps**: `docker_postgres` now streams
  dump output directly to disk instead of buffering the full dump in
  memory.

### Changed

- **Release versioning**: the project is now marked as `1.0.0` to
  reflect a stable public CLI and config surface covering backup,
  restore, archive listing, validation, dump providers, hooks, and
  rotation.

- **CLI/runtime polish**:
  - console verbosity can now be controlled explicitly
  - validation preflight now covers target directory checks and dump
    prerequisites instead of only source directories

- **Documentation updates**:
  - architecture, configuration, deployment, and package-level docs were
    synchronized with the current CLI and provider set
  - package README now reflects `backup`, `restore`, `list`, and
    `validate`

### Fixed

- **PostgreSQL validation with passwords**: `validate` now passes
  `PGPASSWORD` to `pg_isready`, avoiding false failures for
  password-protected servers.

- **SQLite dry-run logging**: provider-specific dry-run output now uses
  `db_path=...` for SQLite instead of misleading network-style host/port
  messaging.

- **Partial dump cleanup**: failed or timed-out dump runs now clean up
  incomplete output files more reliably.

- **Lint and compatibility cleanup**:
  - repository is `ruff` clean
  - Python 3.12 metadata is advertised consistently with CI coverage
  - stale comments and docstrings were updated to match actual behavior

## [0.4.0]

### Added

- **Docker exec PostgreSQL provider** (`docker_postgres`): dump databases
  running inside Docker containers via `docker exec <container> pg_dump`.
  No exposed ports or host-level pg_dump needed. Output uses `--format=custom`
  for `pg_restore` compatibility.

- **GitHub Actions CI**: lint (ruff) + tests on Python 3.8-3.12.

- **Restore pipeline** in `backup/restore.py`: archive planning,
  prefix filtering, conflict detection, path-traversal rejection,
  dry-run support, and atomic extraction.

- **Database dump subsystem** in `backup/db_dump.py`: provider-based
  staging for PostgreSQL (`pg_dump`) and SQLite (`sqlite3 .backup`)
  with archive integration.

- **Optional service hooks**:
  - `pre_command` / `post_command` for filesystem subfolders
  - `stop_command` / `start_command` for database dump configs

- **Packaging and CLI metadata**:
  - setuptools-based `pyproject.toml`
  - installable `archwright` console script
  - packaged example config

- **CLI subcommands**:
  - `archwright backup`
  - `archwright restore`
  - `archwright list`

- **Documentation and repo presentation**:
  - `docs/architecture.md`
  - `docs/configuration.md`
  - `docs/deployment.md`
  - repository landing README and banner assets
  - refreshed package README and example config around dumps, hooks, and restore

- **New test coverage**:
  - `tests/test_restore.py`
  - `tests/test_db_dump.py`
  - new end-to-end cases for hook failures and dump cleanup behavior

### Changed

- **Config schema hardening**:
  - strict type validation for core fields
  - provider-specific database validation (`postgres` vs `sqlite`)
  - safe segment validation for backup/database archive names

- **Backup pipeline orchestration**:
  - restore and db-dump paths now live inside the active package layout
  - database dumps are merged into the normal archive flow
  - service hooks run in config order and restart in reverse order
  - CLI dispatch now exposes backup, restore, and archive listing as first-class commands

- **Archive internals cleanup**:
  - `ZIP_MIN_TIMESTAMP` / `ZIP_MAX_TIMESTAMP` moved to `constants.py`
  - restore-related tests refreshed to match the current package flow

### Fixed

- **Dry-run side effects removed for database dumps**: `--dry-run` no
  longer creates dump staging directories or backup targets just because
  `databases:` is configured.

- **Staging cleanup on error paths**: temporary `.db_staging_*`
  directories are removed on dump failures, collection failures, and
  archive failures.

- **Service restart failures now fail the run**: a broken
  `post_command` no longer results in `EXIT_SUCCESS` with a misleading
  "Backup completed successfully" log line.

- **SQLite backup paths with spaces**: SQLite `.backup` output paths are
  now quoted correctly, so dump targets under directories like
  `"with space/"` work.

- **Validation updates**:
  - removed a dead import in `cli.py`
  - reduced `validate_source_dirs()` from a double-check path to a
    single `stat()`-based validation
  - kept standard `pytest` invocation working from the repo root
  - preserved side-effect-free dry-run semantics across newer features

---

## [0.3.0] - Config hardening and timestamp safety

### Added

- **`_require_string()`**, **`_optional_string()`**, **`_require_int()`**
  in `config.py`: Strict type validators that reject silent coercion
  (e.g. `include: 42` no longer becomes `"42"`, it raises `ValueError`).

- **`_validate_backup_name()`** in `config.py`: Rejects path separators
  (`/`, `\`) and dot-only names (`.`, `..`) to prevent path traversal
  in output filenames.

- **`_require_int()` rejects booleans**: In Python, `isinstance(True, int)`
  is `True`. The validator now explicitly checks `isinstance(value, bool)`
  and rejects it, so `keep_last: true` fails instead of being treated as 1.

- **ZIP timestamp clamping** in `archive.py`: Modification timestamps
  before 1980-01-01 or after 2107-12-31 are clamped to the ZIP format
  bounds instead of crashing `ZipInfo`.

- **`_ensure_target_dir` file-vs-directory check** in `cli.py`: If
  `target_base_dir` points at an existing regular file (not a directory),
  the tool now raises `ValueError` instead of silently failing later.

- **`setup_logging` OSError guard** in `cli.py`: `FileHandler` creation
  is wrapped in `try/except OSError` to handle race conditions where the
  target directory becomes unwritable between `mkdir` and log file open.

- **`pythonpath = ["."]`** in `pyproject.toml` so `pytest` discovers the
  `backup` package without `pip install -e .`.

### Added (tests)

- `test_keep_last_bool_rejected` - verifies `keep_last: true` fails.
- `test_include_must_be_string` - verifies `include: [...]` fails.
- `test_backup_name_rejects_path_separators` - verifies `../escape` fails.
- `test_mod_time_before_1980_is_clamped` - verifies pre-1980 files get
  clamped timestamps in ZipInfo.
- `test_dry_run_rejects_target_path_that_is_file` - verifies file-as-dir
  detection works in dry-run mode.
- `test_invalid_backup_name_returns_error` - E2E test for path traversal
  rejection through the full pipeline.

---

## [0.2.0] - Modular package with test suite

### Changed

- **Monolith → package**: Split single-file `backup.py` into 10 modules
  with a flat, acyclic dependency graph.

### Added

- **`backup/__main__.py`**: Enables `python -m backup` invocation.
- **`pyproject.toml`**: pytest configuration with markers.
- **`tests/conftest.py`**: Shared fixtures (logger, filesystem builders,
  config factories, YAML writer).
- **`tests/test_unit.py`**: 23 test functions covering `parse_glob`,
  `_matches_any`, `_require_field`, config validation, and `_make_clean_zipinfo`.
- **`tests/test_integration.py`**: 15 test functions covering file
  collection, symlink resolution, destination collision, rotation, and
  symlink cycle detection.
- **`tests/test_e2e.py`**: 14 test functions covering full pipeline run,
  dry-run, archive structure, content integrity, metadata neutrality,
  and CLI argument parsing.
- **`tests/test_edge_cases.py`**: 15 test functions covering missing
  source dirs, target dir creation, permission denial, atomic writes,
  dangling symlinks, and empty result sets.

---

## [0.1.2] - OOM fix

### Fixed

- **Memory regression in `create_archive`**: Replaced `read_bytes()`
  (loads entire file into RAM) with `ZipFile.open()` +
  `shutil.copyfileobj()` streaming in 1 MiB chunks. Memory usage is
  now constant regardless of file size.

---

## [0.1.1] - Nitpick fixes

### Fixed

- **Symlink loop protection**: Replaced `os.walk(followlinks=True)` with
  a custom `_walk_safe()` using `(st_dev, st_ino)` cycle detection.
  Handles circular directory symlinks without infinite loops.

- **Pure pathlib**: Removed all `os.walk` / `os.path` usage. File
  iteration now uses `Path.iterdir()` exclusively.

- **Metadata stripping**: Switched from `zf.write()` (which copies
  source permissions via `os.stat`) to `ZipInfo` with fixed
  `external_attr = (S_IFREG | 0o644) << 16`. Original file permissions
  no longer leak into the archive.

---

## [0.1.0] - Initial implementation

### Added

- Single-file `backup.py` with all features:
  - YAML config loading and validation.
  - Recursive file collection with glob include/exclude.
  - Compressed ZIP archive creation.
  - Backup rotation with `keep_last`.
  - Dry-run mode.
  - Dual logging (stdout + file).
  - Atomic writes via `.zip.tmp` → rename.
  - Cross-platform (Linux / Windows), Python 3.8+.
