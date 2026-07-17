#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN_DIR="${AGENT_BRIDGE_BIN_DIR:-$HOME/.local/bin}"

mkdir -p "$BIN_DIR" "$HOME/.local/state/agent-bridge"
chmod +x "$PROJECT_DIR/bin/agent"
ln -sfn "$PROJECT_DIR/bin/agent" "$BIN_DIR/agent"

if [[ "${AGENT_BRIDGE_SKIP_HOOKS:-0}" != "1" ]]; then
  AGENT_BRIDGE_HOOK_AGENT="$BIN_DIR/agent" "$BIN_DIR/agent" code hooks install --client all
fi

printf 'Installed agent command: %s/agent -> %s/bin/agent\n' "$BIN_DIR" "$PROJECT_DIR"
printf 'State directory: %s\n' "${AGENT_BRIDGE_STATE_DIR:-$HOME/.local/state/agent-bridge}"
printf 'Run: agent code hooks status --client all\n'
