#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN_DIR="${AGENT_BRIDGE_BIN_DIR:-$HOME/.local/bin}"
REMOVE_HOOKS=0

usage() {
  printf '%s\n' \
    "usage: scripts/uninstall.sh [--bin-dir PATH] [--remove-hooks]" \
    "" \
    "Removes only the launcher that points to this checkout." \
    "  --remove-hooks  Also remove exact Agent Bridge hook and wrapper entries." \
    "  --bin-dir PATH  Override the default launcher directory (~/.local/bin)."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      [[ $# -ge 2 ]] || {
        printf 'uninstall.sh: --bin-dir requires a path\n' >&2
        exit 2
      }
      BIN_DIR="$2"
      shift 2
      ;;
    --remove-hooks)
      REMOVE_HOOKS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'uninstall.sh: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

AGENT_PATH="$BIN_DIR/agent"
EXPECTED_TARGET="$PROJECT_DIR/bin/agent"

if [[ "$REMOVE_HOOKS" == "1" ]]; then
  AGENT_BRIDGE_HOOK_AGENT="$AGENT_PATH" "$EXPECTED_TARGET" code hooks uninstall --client all
fi

if [[ ! -e "$AGENT_PATH" && ! -L "$AGENT_PATH" ]]; then
  printf 'Agent launcher already absent: %s\n' "$AGENT_PATH"
elif [[ -L "$AGENT_PATH" && "$(readlink "$AGENT_PATH")" == "$EXPECTED_TARGET" ]]; then
  unlink "$AGENT_PATH"
  printf 'Removed Agent Bridge launcher: %s\n' "$AGENT_PATH"
else
  printf 'uninstall.sh: preserved non-matching launcher: %s\n' "$AGENT_PATH" >&2
  exit 2
fi

printf 'Retained runtime state: %s\n' "${AGENT_BRIDGE_STATE_DIR:-$HOME/.local/state/agent-bridge}"
