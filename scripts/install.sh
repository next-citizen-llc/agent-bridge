#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BIN_DIR="${AGENT_BRIDGE_BIN_DIR:-$HOME/.local/bin}"
INSTALL_HOOKS="${AGENT_BRIDGE_INSTALL_HOOKS:-0}"
FORCE_INSTALL="${AGENT_BRIDGE_FORCE_INSTALL:-0}"

usage() {
  printf '%s\n' \
    "usage: scripts/install.sh [--bin-dir PATH] [--install-hooks] [--force]" \
    "" \
    "Installs only the Agent Bridge launcher by default." \
    "  --install-hooks  Also install supported harness startup hooks and wrappers." \
    "  --force          Replace an existing launcher at the exact target path." \
    "  --bin-dir PATH   Override the default launcher directory (~/.local/bin)."
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bin-dir)
      [[ $# -ge 2 ]] || {
        printf 'install.sh: --bin-dir requires a path\n' >&2
        exit 2
      }
      BIN_DIR="$2"
      shift 2
      ;;
    --install-hooks)
      INSTALL_HOOKS=1
      shift
      ;;
    --force)
      FORCE_INSTALL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'install.sh: unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

mkdir -p "$BIN_DIR" "$HOME/.local/state/agent-bridge"
chmod +x "$PROJECT_DIR/bin/agent"

AGENT_PATH="$BIN_DIR/agent"
EXPECTED_TARGET="$PROJECT_DIR/bin/agent"
if [[ -e "$AGENT_PATH" || -L "$AGENT_PATH" ]]; then
  if [[ -d "$AGENT_PATH" && ! -L "$AGENT_PATH" ]]; then
    printf 'install.sh: refusing to replace directory: %s\n' "$AGENT_PATH" >&2
    exit 2
  elif [[ -L "$AGENT_PATH" && "$(readlink "$AGENT_PATH")" == "$EXPECTED_TARGET" ]]; then
    LAUNCHER_STATUS="already installed"
  elif [[ "$FORCE_INSTALL" == "1" ]]; then
    ln -sfn "$EXPECTED_TARGET" "$AGENT_PATH"
    LAUNCHER_STATUS="replaced by explicit --force"
  else
    printf 'install.sh: refusing to replace existing launcher: %s\n' "$AGENT_PATH" >&2
    printf 'Inspect it, choose another --bin-dir, or rerun with --force.\n' >&2
    exit 2
  fi
else
  ln -s "$EXPECTED_TARGET" "$AGENT_PATH"
  LAUNCHER_STATUS="installed"
fi

if [[ "$INSTALL_HOOKS" == "1" ]]; then
  AGENT_BRIDGE_HOOK_AGENT="$AGENT_PATH" "$AGENT_PATH" code hooks install --client all
  HOOK_STATUS="installed by explicit request"
else
  HOOK_STATUS="not installed; opt in with: agent code hooks install --client all"
fi

printf 'Agent launcher: %s -> %s (%s)\n' "$AGENT_PATH" "$EXPECTED_TARGET" "$LAUNCHER_STATUS"
printf 'State directory: %s\n' "${AGENT_BRIDGE_STATE_DIR:-$HOME/.local/state/agent-bridge}"
printf 'Startup hooks: %s\n' "$HOOK_STATUS"
printf 'If needed for this shell: export PATH="%s:$PATH"\n' "$BIN_DIR"
printf 'Uninstall: %s/scripts/uninstall.sh\n' "$PROJECT_DIR"
