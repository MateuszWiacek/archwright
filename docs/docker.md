# Docker

The Docker image is primarily a deployment option for the web UI. It also keeps the `archwright` CLI available as the container entrypoint.

The CLI remains the core execution engine. Docker only provides a repeatable runtime.

## Build

```bash
docker build -t archwright:local .
```

Smoke check:

```bash
docker run --rm archwright:local --help
docker run --rm archwright:local serve --help
```

## Web UI

Run the web UI for a directory of configs:

```bash
docker run --rm \
  -p 127.0.0.1:8471:8471 \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local
```

The default container command is:

```bash
archwright serve --config-dir /config --host 0.0.0.0 --port 8471
```

Use an explicit command for one config:

```bash
docker run --rm \
  -p 127.0.0.1:8471:8471 \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local serve --config /config/myapp.yaml --host 0.0.0.0
```

## Compose

Copy the example and adjust the host paths:

```bash
cp docker-compose.example.yml docker-compose.yml
ARCHWRIGHT_CONFIG_DIR=/etc/archwright \
ARCHWRIGHT_BACKUP_DIR=/srv/backups \
docker compose up -d --build
```

The example binds to `127.0.0.1` by default. Put it behind a trusted reverse proxy if you expose it on a LAN.

## Inventory Mode In Docker

Inventory mode can control remote nodes over SSH:

```bash
docker run --rm \
  -p 127.0.0.1:8471:8471 \
  -v /etc/archwright/inventory.yaml:/inventory.yaml:ro \
  -v "$HOME/.ssh/known_hosts:/home/archwright/.ssh/known_hosts:ro" \
  -v "$HOME/.ssh/archwright_gui_ed25519:/home/archwright/.ssh/id_ed25519:ro" \
  archwright:local serve --inventory /inventory.yaml --host 0.0.0.0
```

The container includes `openssh-client`, but it does not manage trust or keys.
Prepare SSH outside the container first:

- every SSH node must already be trusted in `known_hosts`
- use a dedicated key for the web UI, not your main personal SSH key
- the mounted key must work without a password prompt
- the inventory `command` should point at the host-local `archwright` wrapper
- if the wrapper needs sudo, allow only that exact command through sudoers and
  use `sudo -n /path/to/archwright` so the UI fails fast instead of prompting

For a homelab deployment, keep the scheduled backup jobs on the nodes through
systemd timers. Use the Docker-hosted web UI as the control plane for archive
visibility, validate, dry-run, live backup, and guarded restore.

## Path Model

Configs are evaluated inside the container. Any path in YAML must exist inside the container.

`target_base_dir` must point at a writable mounted volume inside the container. If the host backup directory is mounted as `/backups`, use paths such as `/backups/myapp` in the Docker-facing config.

Recommended convention:

```yaml
backup_name: "myapp"
target_base_dir: "/backups/myapp"

structure:
  myapp:
    config:
      source_dir: "/sources/myapp/config"
      include: "*"
```

Matching run command:

```bash
docker run --rm \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  -v /opt/myapp/config:/sources/myapp/config:ro \
  archwright:local backup --config /config/myapp.yaml
```

If your native config uses host paths like `/mnt/appdata`, either mount the same host path into the same container path or keep a Docker-specific config that uses `/sources/...` and `/backups/...`.

## CLI In Docker

Native CLI remains the recommended mode for scheduled host backups because it sees host paths, local tools, and service managers directly.

Docker CLI runs are useful for smoke tests, demos, and hosts where Python packaging is awkward:

```bash
docker run --rm \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  -v /opt/myapp:/sources/myapp:ro \
  archwright:local validate --config /config/myapp.yaml
```

## Docker-Hosted PostgreSQL

The image includes the Docker CLI so `docker_postgres` and `docker stop/start` hooks can work when the host Docker socket is mounted:

```bash
docker run --rm \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  -v /var/run/docker.sock:/var/run/docker.sock \
  archwright:local backup --config /config/myapp.yaml
```

Mounting `/var/run/docker.sock` grants powerful host access. Only do this on trusted systems.

If the socket is group-restricted, add the host socket group:

```bash
docker run --rm \
  --group-add "$(stat -c '%g' /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local validate --config /config/myapp.yaml
```

## Permissions

The image runs as UID `10001` by default. The mounted backup directory must be writable by that user, or run the container with a matching user:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v /etc/archwright:/config:ro \
  -v /srv/backups:/backups \
  archwright:local list --config /config/myapp.yaml
```

For restore jobs, the container user also needs write access to the mounted restore targets.

## Runtime Tools

The image includes:

- `sqlite3` for SQLite dumps
- `pg_dump` / `pg_restore` from `postgresql-client`
- Docker CLI for `docker_postgres` and Docker-based hooks
- OpenSSH client for inventory-based remote execution

Host service managers such as `systemctl` are not available inside the container. Prefer Docker hooks through the socket, or run the CLI natively on hosts that need systemd-level hooks.
