#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

HOST="${ARCHWRIGHT_HOST:-127.0.0.1}"
PORT="${ARCHWRIGHT_PORT:-8471}"
ARCHWRIGHT_BIN="${ARCHWRIGHT_BIN:-$ROOT_DIR/.venv/bin/archwright}"

if [[ ! -x "$ARCHWRIGHT_BIN" ]]; then
  ARCHWRIGHT_BIN="archwright"
fi

if [[ "$#" -eq 0 ]]; then
  DEV_DIR="$ROOT_DIR/.dev/archwright"
  CONFIG_DIR="$DEV_DIR/configs"
  SOURCE_DIR="$DEV_DIR/sample-source"
  TARGET_DIR="$DEV_DIR/backups"
  CONFIG_PATH="$CONFIG_DIR/local-dev.yaml"

  mkdir -p "$CONFIG_DIR" "$SOURCE_DIR" "$TARGET_DIR"

  if [[ ! -f "$SOURCE_DIR/hello.txt" ]]; then
    printf "hello from archwright dev backend\n" > "$SOURCE_DIR/hello.txt"
  fi

  cat > "$CONFIG_PATH" <<EOF
backup_name: local_dev
target_base_dir: "$TARGET_DIR"
keep_last: 3
structure:
  app:
    files:
      source_dir: "$SOURCE_DIR"
      include: "*"
EOF

  set -- --config-dir "$CONFIG_DIR"
fi

echo "Starting Archwright backend on http://$HOST:$PORT"
echo "Command: $ARCHWRIGHT_BIN serve --host $HOST --port $PORT $*"
exec "$ARCHWRIGHT_BIN" serve --host "$HOST" --port "$PORT" "$@"
