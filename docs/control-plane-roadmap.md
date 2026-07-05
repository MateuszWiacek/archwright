# Control Plane Roadmap

The current web UI can handle one config file or multiple local config files. The next direction is central control without moving backup execution away from the node that owns the data.

## Principle

`archwright` should stay CLI-first.

The CLI is the execution engine. The web UI is a control plane that can call the CLI locally or remotely.

Configs should stay in Git. The UI should not become the hidden source of production backup state.

## Why Local Execution Still Matters

Backup jobs often need local context:

- filesystem paths
- Docker socket access
- local container names
- local `sqlite3`, `pg_dump`, or `docker` binaries
- NFS mount paths as seen by that host
- service hook commands

A central UI can coordinate these jobs, but the job should run on the node where those paths and tools are valid.

## Target Model

```text
Git-managed configs
  -> deployed to nodes by Ansible, rsync, or another config process

Central web UI
  -> local executor for jobs on the UI host
  -> SSH executor for jobs on remote nodes

Node
  -> runs archwright CLI locally
  -> writes archives to local or mounted backup storage
```

This keeps storage flexible. A node can write to local disk, NFS, or another mounted target, as long as its YAML config uses paths valid on that node.

## Proposed Inventory

See [../inventory.example.yaml](../inventory.example.yaml) for a copyable example.

```yaml
nodes:
  local-node:
    executor: local
    config_dir: /etc/archwright

  remote-node:
    executor: ssh
    host: app-node.example
    user: archwright
    port: 22
    config_dir: /opt/archwright/configs
    command: /opt/archwright/archwright
```

The inventory describes where configs live and how commands are executed. The YAML backup configs still describe what each backup does.

Current parser rules:

- top-level `nodes` mapping is required
- node ids may contain only letters, digits, `-`, and `_`
- `executor` must be `local` or `ssh`
- all nodes require `config_dir`
- `ssh` nodes require `host` and `user`
- `port` defaults to `22`
- `command` defaults to `archwright`

## Milestones

### Phase 1: Single-Config Web UI

Status: implemented in the current web UI.

Scope:

- one config passed by `--config`
- dashboard
- archive and log browser
- backup, dry-run, validate
- guarded restore wizard

### Phase 2: Multi-Config Single Node

Status: implemented for local config directories.

Current shape:

- `archwright serve --config-dir /etc/archwright`
- config discovery
- stable `job_id`
- dashboard/config/archive/log/restore views scoped by `?job=<job_id>`
- one process-local execution lock

Possible next polish:

- lock per job or per host

Longer-term route shape:

```text
GET /jobs
GET /jobs/{job_id}
POST /jobs/{job_id}/backup
POST /jobs/{job_id}/validate
GET /jobs/{job_id}/archives
GET /jobs/{job_id}/restore
```

### Phase 3: Multi-Node Control

Status: inventory mode is wired for local nodes and SSH-backed control actions, including remote restore execution.

Add:

- `archwright serve --inventory inventory.yaml`
- `LocalExecutor`
- `SSHExecutor`
- node/job dashboard
- remote archive listing (done)
- remote validate and backup dry-run (done)
- remote restore dry-run plan (done)
- remote live backup (done)
- remote guarded restore execution (done)

The SSH executor should run `archwright` on the remote host. It should not reimplement backup behavior in the web process.

### Phase 4: Machine-Readable CLI

Status: mostly implemented. `list`, `validate`, backup dry-run, and restore dry-run support JSON output. The web UI archive listing uses the local and SSH JSON executors. Live backup and live restore JSON are intentionally not implemented yet.

JSON output for UI and automation:

| Command | Status |
|---|---|
| `archwright list --config job.yaml --json` | done |
| `archwright validate --config job.yaml --json` | done |
| `archwright backup --config job.yaml --dry-run --json` | done |
| `archwright restore --config job.yaml --archive backup.zip --dry-run --json` | done |

```bash
archwright list --config job.yaml --json
archwright validate --config job.yaml --json
archwright backup --config job.yaml --dry-run --json
archwright restore --config job.yaml --archive backup.zip --dry-run --json
```

This reduces scraping and makes local and SSH execution behave the same way.

### Phase 5: Docker Image

Status: local image implemented. Registry publishing is still future work.

The image supports both CLI and web UI:

```bash
docker run ... archwright backup --config /config/job.yaml
docker run ... archwright serve --config /config/job.yaml --host 0.0.0.0
```

The image documents mounts for configs, source paths, backup targets, permissions, and Docker socket access.

## What Not To Build Yet

Avoid these until the simpler model proves itself:

- always-on remote agents
- multi-user RBAC
- distributed scheduler
- web-based production config editing
- built-in secrets management
- replacing systemd timers

Those can come later if the project needs them. The near-term value is central visibility and controlled execution over Git-managed configs.

## Non-Negotiables

- CLI keeps working without the web UI
- configs remain readable YAML
- production config changes stay Git-friendly
- remote execution happens on the node that owns the data
- restore remains guarded
- the UI never accepts arbitrary shell commands from the browser
