#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${SILO_DATA_DIR:-$HOME/SiloData}"
PYTHON_BIN="${SILO_PYTHON:-/opt/homebrew/bin/python3.12}"
BREW_BIN="${SILO_BREW:-/opt/homebrew/bin/brew}"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

if [[ ! -x "$PYTHON_BIN" ]]; then
  "$BREW_BIN" install python@3.12
fi
mkdir -p "$DATA_DIR/logs" "$DATA_DIR/private"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$DATA_DIR/browsers}"
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"

"$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
"$PROJECT_DIR/.venv/bin/python" -m playwright install chromium

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
fi
if ! grep -q '^PLAYWRIGHT_BROWSERS_PATH=' "$PROJECT_DIR/.env"; then
  printf '\nPLAYWRIGHT_BROWSERS_PATH=%s\n' "$PLAYWRIGHT_BROWSERS_PATH" >> "$PROJECT_DIR/.env"
fi

chmod +x "$PROJECT_DIR/run.sh"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/com.silo.creator-corpus.plist"
mkdir -p "$LAUNCH_AGENTS_DIR"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    "$PROJECT_DIR/deploy/macos/com.silo.creator-corpus.plist" > "$PLIST_PATH"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$PLIST_PATH"
launchctl enable "$DOMAIN/com.silo.creator-corpus"
launchctl kickstart -k "$DOMAIN/com.silo.creator-corpus"

echo "Silo installed. Open http://$(hostname):8001 or http://127.0.0.1:8001."

