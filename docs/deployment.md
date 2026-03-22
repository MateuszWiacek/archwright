# Deployment

## Prerequisites

- Python 3.8+
- PyYAML (`pip install pyyaml`)
- Optional: `sqlite3` for SQLite database dumps
- Optional: `pg_dump` for PostgreSQL database dumps
- Optional: `docker` for Docker-hosted PostgreSQL dumps

## CLI reference

```bash
# Create a backup
archwright backup --config /etc/archwright/myapp.yaml

# Preview without creating files
archwright backup --config /etc/archwright/myapp.yaml --dry-run

# Validate config and runtime prerequisites (no side effects)
archwright validate --config /etc/archwright/myapp.yaml

# List available archives
archwright list --config /etc/archwright/myapp.yaml

# Restore from an archive
archwright restore --config /etc/archwright/myapp.yaml \
    --archive /srv/backups/myapp/myapp_2026-03-15_03-00-00.zip

# Restore only specific sections
archwright restore --config myapp.yaml --archive backup.zip \
    --only vaultwarden/config logs/application

# Force overwrite existing files
archwright restore --config myapp.yaml --archive backup.zip --overwrite

# Preview a restore
archwright restore --config myapp.yaml --archive backup.zip --dry-run
```

All subcommands accept `--verbose` / `-v` (DEBUG output) and `--quiet` / `-q` (suppress INFO, show only warnings and errors). These flags are mutually exclusive.

Exit codes: `0` means success, `1` means error.

## Cron

```cron
# Tier 1 - daily at 03:00
0 3 * * * /usr/local/bin/archwright backup --config /etc/archwright/tier1.yaml >> /var/log/archwright-tier1.log 2>&1

# Tier 2 - weekly at 04:00 on Sunday
0 4 * * 0 /usr/local/bin/archwright backup --config /etc/archwright/tier2.yaml >> /var/log/archwright-tier2.log 2>&1
```

## systemd

### Service unit

```ini
# /etc/systemd/system/archwright-tier1.service
[Unit]
Description=archwright tier-1 backup
After=network-online.target docker.service
Wants=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/archwright backup --config /etc/archwright/tier1.yaml

NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/srv/backups /opt
ReadOnlyPaths=/var/log
ProtectHome=yes
PrivateTmp=yes
```

### Timer unit

```ini
# /etc/systemd/system/archwright-tier1.timer
[Unit]
Description=Run archwright tier-1 backup daily at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

### Activation

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now archwright-tier1.timer
sudo systemctl start archwright-tier1.service
systemctl list-timers archwright-*
journalctl -u archwright-tier1.service --since today
```

`Persistent=true` replays a missed run after boot. `RandomizedDelaySec=300` helps spread load when multiple timers share the same schedule.

## systemd hardening notes

`ReadWritePaths` must include `target_base_dir` and any paths written during restore or dump staging. If your config uses Docker-oriented hooks, the service user also needs Docker socket access.

```ini
ReadWritePaths=/srv/backups /opt
SupplementaryGroups=docker
```

## Ansible

`archwright` does not ship an Ansible role in this repository. The
examples below show one reasonable deployment pattern for teams that
want to manage configs, systemd timers, and installation declaratively.

### Reference role structure

```text
roles/archwright/
├── defaults/main.yml
├── tasks/main.yml
└── templates/
    ├── backup-config.yml.j2
    ├── archwright.service.j2
    └── archwright.timer.j2
```

### Deployment model

Typical options:

- install from a wheel or `pip install .`
- deploy a checked-out source tree and call `python -m backup`
- wrap the command in a small shell script if you want a stable local path

The important part is that the host ends up with:

- a runnable `archwright` command
- one or more rendered YAML configs
- one `systemd` service/timer pair per backup job

### defaults/main.yml

```yaml
archwright_command: "/usr/local/bin/archwright"
archwright_config_dir: "/etc/archwright"
archwright_backup_dir: "/srv/backups/archwright"
archwright_keep_last: 7
archwright_run_user: "root"
archwright_run_group: "root"
archwright_jobs: []
```

### Job definition

Each host can define one or more jobs. A job maps a rendered config file
to a `systemd` timer schedule:

```yaml
archwright_jobs:
  - name: "immich-db"
    description: "Immich PostgreSQL backup"
    config_template: "backup-config.yml.j2"
    config_path: "{{ archwright_config_dir }}/immich-db.yml"
    on_calendar: "*-*-* 03:00:00"
    backup_name: "immich"
    target_base_dir: "{{ archwright_backup_dir }}/immich-db"
    structure:
      immich:
        compose:
          source_dir: "/opt/immich"
          include: "docker-compose.yml"
    databases:
      immich_db:
        provider: "docker_postgres"
        container: "immich_postgres"
        dbname: "immich"
        user: "immich"
```

The role can loop over `archwright_jobs` to render configs, create
directories, and manage matching service/timer units.

### Generic config template

One generic template is often enough if your jobs are already described
as data:

```jinja2
backup_name: "{{ item.backup_name }}"
target_base_dir: "{{ item.target_base_dir }}"
keep_last: {{ archwright_keep_last }}
{% if item.log_level is defined %}
log_level: "{{ item.log_level }}"
{% endif %}
{% if item.structure is defined %}
structure:
{{ item.structure | to_nice_yaml(indent=2) | indent(0, true) }}
{% endif %}
{% if item.databases is defined %}
databases:
{{ item.databases | to_nice_yaml(indent=2) | indent(0, true) }}
{% endif %}
```

### What the role does

1. Install or deploy `archwright` on the target host
2. Create config and backup directories
3. Render one YAML config per job
4. Create one `systemd` service and timer per job
5. Enable and start the timers

## Monitoring

### Exit code checks

```bash
#!/bin/bash
archwright backup --config "$1"
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "archwright backup failed (exit $EXIT_CODE)" | \
        mail -s "Backup failure on $(hostname)" ops@example.com
fi
exit $EXIT_CODE
```

### Freshness and size checks

```bash
# Alert if newest archive is older than 25 hours
find /srv/backups/myapp -name "myapp_*.zip" -mmin -1500 | \
    grep -q . || echo "STALE BACKUP"

# Last two archive sizes (large drop can indicate missing source data)
ls -lS /srv/backups/myapp/myapp_*.zip | head -2
```

## Restore runbook

### Quick restore

```bash
# 1. List available archives
archwright list --config /etc/archwright/myapp.yaml

# 2. Preview the restore
archwright restore --config /etc/archwright/myapp.yaml \
    --archive /srv/backups/myapp/myapp_2026-03-15_03-00-00.zip \
    --dry-run

# 3. Restore, optionally stopping services first
docker stop myapp
archwright restore --config /etc/archwright/myapp.yaml \
    --archive /srv/backups/myapp/myapp_2026-03-15_03-00-00.zip \
    --overwrite
docker start myapp
```

### Partial restore

```bash
archwright restore --config myapp.yaml --archive backup.zip --only vaultwarden/config
archwright restore --config myapp.yaml --archive backup.zip --only logs/application
```

### Database restore

Database dumps are stored inside the archive, but restoration is manual with the provider-specific tool.

```bash
# Extract dump files from archive
unzip backup.zip "databases/*" -d /tmp/restore/
```

**postgres** (host-level pg_dump, direct connection):

```bash
pg_restore --host localhost --dbname authentik --clean \
    /tmp/restore/databases/authentik_pg/*.dump
```

**docker_postgres** (dump was created via docker exec, database has no exposed port):

```bash
docker exec -i immich_postgres pg_restore \
    --username immich --dbname immich --clean \
    < /tmp/restore/databases/immich_db/*.dump
```

**sqlite** (plain file copy):

```bash
# Stop the application first if it holds a write lock
cp /tmp/restore/databases/vaultwarden_db/*.sqlite3 \
    /opt/vaultwarden/data/db.sqlite3
```

## Security considerations

- Config files should be readable only by the backup user (`chmod 0640`).
- Credentials in YAML should be protected like any other secret.
- Service hook commands are shell-executed, so configs must be trusted input.
- Restore should run under the least-privileged account that still has access to target paths.
