#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
exec "$PROJECT_DIR/.venv/bin/uvicorn" app.main:app \
  --app-dir "$PROJECT_DIR" \
  --host "${SILO_HOST:-0.0.0.0}" \
  --port "${SILO_PORT:-8001}"
