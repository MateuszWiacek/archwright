# Configuration reference

archwright reads a single YAML file that defines what to back up, where to store it, how many copies to keep, and what databases to dump.

## Minimal example

```yaml
backup_name: "myapp"
target_base_dir: "/srv/backups/myapp"
keep_last: 5

structure:
  data:
    config:
      source_dir: "/opt/myapp/config"
      include: "*"
```

## Full example

```yaml
backup_name: "vaultwarden_full"
target_base_dir: "/srv/backups/vaultwarden"
keep_last: 7

structure:
  vaultwarden:
    config:
      source_dir: "/opt/vaultwarden/data"
      include: "*.json"
      exclude: "*-tmp.json"
    attachments:
      source_dir: "/opt/vaultwarden/data/attachments"
      include: "*"
      pre_command: "docker stop vaultwarden"
      post_command: "docker start vaultwarden"
  logs:
    application:
      source_dir: "/var/log/vaultwarden"
      include: "*.{log,txt}"

databases:
  vaultwarden_sqlite:
    provider: "sqlite"
    db_path: "/opt/vaultwarden/data/db.sqlite3"
  authentik_pg:
    provider: "postgres"
    dbname: "authentik"
    host: "localhost"
    port: 5432
    user: "authentik"
    password: "changeme"
    extra_args: ["--no-owner", "--clean"]
  immich_db:
    provider: "docker_postgres"
    container: "immich_postgres"
    dbname: "immich"
    user: "immich"
```

## Top-level fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `backup_name` | string | yes | - | Prefix for output filenames. Must be a plain name: no `/`, `\`, `.`, `..`, or glob metacharacters. |
| `target_base_dir` | string | yes | - | Directory for `.zip` and `.log` output. Created automatically if missing. |
| `keep_last` | integer | yes | - | Number of backup pairs to retain after rotation. `0` disables rotation. |
| `structure` | mapping | yes | - | Two-level folder to subfolder to source mapping. At least one subfolder required. |
| `databases` | mapping | no | none | Named database dump configurations. |
| `log_level` | string | no | `"INFO"` | Console log level. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |
| `hook_timeout` | integer | no | `300` | Timeout in seconds for service hook commands (`pre_command`, `post_command`, `stop_command`, `start_command`). Must be > 0. |
| `dump_timeout` | integer | no | `3600` | Timeout in seconds for database dump commands (`pg_dump`, `sqlite3`, `docker exec`). Must be > 0. |

## Structure: file collection

The structure is always exactly three levels deep:

```yaml
structure:
  <folder>:
    <subfolder>:
      source_dir: "..."
      include: "..."
      exclude: "..."
      pre_command: "..."
      post_command: "..."
```

### Subfolder fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `source_dir` | string | yes | - | Absolute path to scan recursively. Must exist and be a directory. |
| `include` | string | yes | - | Glob pattern matched against filenames only. Supports brace expansion. |
| `exclude` | string | no | none | Glob pattern for files to skip. Evaluated after `include`. |
| `pre_command` | string | no | none | Shell command to run before collecting this subfolder's files. |
| `post_command` | string | no | none | Shell command to run after collection completes or fails. |

### Service hooks

`pre_command` and `post_command` must always appear as a pair. Config validation rejects one without the other, which prevents accidentally stopping a service without a matching restart.

Commands are deduplicated: if multiple subfolders share the same `pre_command`, the command runs once. Restart order is reversed, so if service A was stopped before service B, service B is restarted first.

`post_command` is always attempted in a `finally` block. If the restart itself fails, the backup run is marked as failed rather than logging a misleading success.

```yaml
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
```

## Databases

The `databases` section is optional. Each key is a logical name used in the archive path (`databases/<name>/...`). Dumps run before file collection and before any file-level service hooks.

### Provider: sqlite

Safe hot backup via `sqlite3 .backup`. Does not require stopping the application, and output paths are quoted safely even when the staging directory contains spaces.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `provider` | string | yes | - | Must be `"sqlite"`. |
| `db_path` | string | yes | - | Absolute path to the SQLite database file. |
| `sqlite3_path` | string | no | `"sqlite3"` | Override if `sqlite3` is not on PATH. |
| `stop_command` | string | no | none | Shell command to stop a service before dumping. |
| `start_command` | string | no | none | Shell command to restart after dumping. |

```yaml
databases:
  vaultwarden_db:
    provider: "sqlite"
    db_path: "/opt/vaultwarden/data/db.sqlite3"
```

### Provider: postgres

Hot dump via `pg_dump --format=custom`. Produces a `.dump` file compatible with `pg_restore`. Requires `pg_dump` on the host and a network-reachable database.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `provider` | string | yes | - | Must be `"postgres"`. |
| `dbname` | string | yes | - | Database name to dump. |
| `host` | string | no | `"localhost"` | Database host. |
| `port` | integer | no | `5432` | Database port. |
| `user` | string | no | `"postgres"` | Database user. |
| `password` | string | no | none | Database password (set via `PGPASSWORD` environment variable). |
| `pg_dump_path` | string | no | `"pg_dump"` | Override if `pg_dump` is not on PATH. |
| `extra_args` | list | no | `[]` | Additional arguments passed to `pg_dump`. |
| `stop_command` | string | no | none | Shell command to stop a service before dumping. |
| `start_command` | string | no | none | Shell command to restart after dumping. |

```yaml
databases:
  authentik_pg:
    provider: "postgres"
    dbname: "authentik"
    host: "localhost"
    port: 5432
    user: "authentik"
    password: "changeme"
    extra_args: ["--no-owner"]
```

### Provider: docker_postgres

Hot dump via `docker exec <container> pg_dump --format=custom`. For databases running inside Docker containers without exposed ports. Runs `pg_dump` inside the container and captures stdout to a local file. No host-level `pg_dump` binary needed.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `provider` | string | yes | - | Must be `"docker_postgres"`. |
| `container` | string | yes | - | Docker container name (e.g. `immich_postgres`). |
| `dbname` | string | yes | - | Database name to dump. |
| `user` | string | no | `"postgres"` | Database user inside the container. |
| `docker_path` | string | no | `"docker"` | Override if `docker` is not on PATH. |
| `extra_args` | list | no | `[]` | Additional arguments passed to `pg_dump` inside the container. |
| `stop_command` | string | no | none | Shell command to stop a service before dumping. |
| `start_command` | string | no | none | Shell command to restart after dumping. |

```yaml
databases:
  immich_db:
    provider: "docker_postgres"
    container: "immich_postgres"
    dbname: "immich"
    user: "immich"

  paperless_db:
    provider: "docker_postgres"
    container: "paperless_db"
    dbname: "paperless"
    user: "paperless"
    extra_args: ["--no-owner"]
```

When to use `docker_postgres` vs `postgres`:
- Use `docker_postgres` when the database runs in a Docker container without an exposed port (most homelab setups)
- Use `postgres` when `pg_dump` is installed on the host and the database port is reachable (direct connection, managed databases)

### Database service hooks

Like file-level hooks, `stop_command` and `start_command` must appear as a pair. They are rarely needed because `pg_dump` and `sqlite3 .backup` are already hot dump tools. Use them only when you explicitly need a cold backup.

## Glob patterns

Patterns use Python `fnmatch` syntax and are evaluated against filenames only, not full paths.

| Pattern | Matches | Does not match |
|---|---|---|
| `*` | Everything | - |
| `*.json` | `data.json`, `config.json` | `data.yaml` |
| `*-tmp.json` | `cache-tmp.json` | `data.json` |
| `*.{log,txt}` | `app.log`, `debug.txt` | `app.csv` |

The `exclude` pattern is always evaluated after `include`. A file must match `include` and not match `exclude` to be collected.

## Validation rules

The config loader enforces strict typing. The following are rejected:

| Input | Error |
|---|---|
| `backup_name: 123` | Must be a string |
| `backup_name: "../escape"` | Must not contain path separators |
| `backup_name: "test[*]"` | Must not contain glob metacharacters |
| `keep_last: true` | Must be an integer (booleans rejected) |
| `keep_last: -1` | Must be >= 0 |
| `include: ["*.json"]` | Must be a string |
| `pre_command` without `post_command` | Requires matching pair |
| `post_command` without `pre_command` | Requires matching pair |
| Postgres without `dbname` | Required field |
| Docker postgres without `container` | Required field |
| Docker postgres without `dbname` | Required field |
| SQLite without `db_path` | Required field |
| `provider: "oracle"` | Unsupported provider |
| `stop_command` without `start_command` (database) | Requires matching pair |
| `start_command` without `stop_command` (database) | Requires matching pair |
| `log_level: "TRACE"` | Must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `hook_timeout: 0` | Must be > 0 |
| `dump_timeout: -1` | Must be > 0 |

### Runtime validation (`archwright validate`)

Beyond config syntax, `archwright validate` performs deeper runtime checks
for each configured database. Validation runs in two stages:

1. **Tool availability** -- is the dump binary (`pg_dump`, `sqlite3`, `docker`) on `PATH`?
2. **Provider-specific reachability** -- can the tool actually reach its target?

| Provider | Check | How |
|---|---|---|
| `postgres` | Database server reachable | Runs `pg_isready --host H --port P --username U`. Skipped gracefully if `pg_isready` is not installed (it is not always bundled with `pg_dump`). |
| `docker_postgres` | Container exists and is running | Runs `docker inspect --format '{{.State.Running}}' <container>`. Fails if the container does not exist or is stopped. |
| `sqlite` | Database file exists | Checks that `db_path` points to an existing regular file. |

This means `archwright validate` will now catch problems like a missing Docker
container or a stopped database service _before_ the backup runs, instead of
reporting success at validation time and then failing during the actual dump.

## Output naming

A run at `2026-03-15 03:00:00` with `backup_name: "vaultwarden_full"` produces:

```text
/srv/backups/vaultwarden/
├── vaultwarden_full_2026-03-15_03-00-00.zip
└── vaultwarden_full_2026-03-15_03-00-00.log
```

## Archive internal structure

```text
vaultwarden/config/config.json
vaultwarden/config/settings.json
vaultwarden/attachments/doc.pdf
logs/application/app.log
databases/vaultwarden_sqlite/vaultwarden_sqlite_db_2026-03-15_03-00-00.sqlite3
databases/authentik_pg/authentik_pg_authentik_2026-03-15_03-00-00.dump
```

Files keep their relative path under `source_dir`. Database dumps are placed under `databases/<name>/`.

## Pipeline execution order

### `archwright backup`

```text
Phase 0  - load_config()           YAML -> BackupConfig
Phase 1  - _ensure_target_dir()    Create target if missing
Phase 2  - validate_source_dirs()  Stat every source_dir
Phase 3  - run_dumps()             Database dumps (sqlite3, pg_dump, docker exec)
Phase 3b - pre_commands            Stop services (deduplicated)
Phase 4  - collect_files()         Walk filesystems -> [CollectedFile]
Phase 5  - create_archive()        [CollectedFile] -> .zip (atomic)
         - post_commands           Restart services (finally block)
Phase 6  - rotate_backups()        Delete old .zip + .log pairs
```

Database dumps always run before file hooks. `pg_dump` and `sqlite3 .backup` capture data while services are still running, and service stopping only applies where you need filesystem-level consistency.

### `archwright validate`

```text
Phase 0  - load_config()                    YAML -> BackupConfig
Phase 1  - _validate_target_dir_preflight() Check parent dir is writable
Phase 2  - validate_source_dirs()           Stat every source_dir
Phase 3  - validate_dump_prerequisites()    Tool on PATH + provider.validate()
```

The validate pipeline never creates files, never runs dumps, and never
executes service hooks. It only reads state and reports problems.
