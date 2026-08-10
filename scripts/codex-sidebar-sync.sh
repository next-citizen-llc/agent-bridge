#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/codex-sidebar-sync.sh export --out DIR [--codex-home DIR]
  scripts/codex-sidebar-sync.sh import --from DIR --yes [--codex-home DIR] [--refresh-sidebar] [--restart] [--allow-platform-mismatch]

Copies the Codex Desktop state that drives sessions and sidebar workspaces.

Export should run on the source machine. Import should run on the target machine
after the bundle is available locally through a synced folder, rsync, or scp.

Options:
  --codex-home DIR      Codex home directory. Defaults to CODEX_HOME or ~/.codex.
  --out DIR             Destination bundle directory for export.
  --from DIR            Source bundle directory for import.
  --yes                 Required for import because target state is overwritten.
  --refresh-sidebar     Validate imported state and write a refresh marker.
  --restart             Quit Codex.app before import and reopen it afterward.
  --allow-platform-mismatch
                        Explicit disaster-recovery override; pointer-sync is the
                        supported cross-platform mechanism.
  -h, --help            Show this help.
EOF
}

die() {
  printf 'codex-sidebar-sync: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

current_platform() {
  case "$(uname -s)" in
    Darwin) printf 'macos\n' ;;
    Linux) printf 'linux\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

default_codex_home() {
  printf '%s\n' "${CODEX_HOME:-$HOME/.codex}"
}

copy_file_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -p "$src" "$dst"
  fi
}

copy_dir_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -d "$src" ]]; then
    mkdir -p "$dst"
    rsync -a --delete "$src"/ "$dst"/
  fi
}

backup_sqlite_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    sqlite3 "$src" ".backup '$dst'"
  fi
}

write_manifest() {
  local bundle="$1"
  local codex_home="$2"
  local mode="$3"
  local hostname_json codex_home_json
  hostname_json="$(json_escape "$(hostname)")"
  codex_home_json="$(json_escape "$codex_home")"
  cat >"$bundle/manifest.json" <<EOF
{
  "schema_version": "1.0",
  "kind": "codex_sidebar_state_bundle",
  "mode": "$mode",
  "created_at": "$(timestamp)",
  "hostname": "$hostname_json",
  "platform": "$(current_platform)",
  "source_codex_home": "$codex_home_json"
}
EOF
}

export_bundle() {
  local codex_home="$1"
  local out="$2"
  need_cmd rsync
  need_cmd sqlite3
  [[ -d "$codex_home" ]] || die "Codex home not found: $codex_home"
  mkdir -p "$out"

  copy_file_if_exists "$codex_home/.codex-global-state.json" "$out/.codex-global-state.json"
  copy_file_if_exists "$codex_home/session_index.jsonl" "$out/session_index.jsonl"
  copy_file_if_exists "$codex_home/external_agent_session_imports.json" "$out/external_agent_session_imports.json"
  copy_file_if_exists "$codex_home/config.toml" "$out/config.toml"

  backup_sqlite_if_exists "$codex_home/state_5.sqlite" "$out/state_5.sqlite"
  backup_sqlite_if_exists "$codex_home/sqlite/state_5.sqlite" "$out/sqlite/state_5.sqlite"
  backup_sqlite_if_exists "$codex_home/logs_2.sqlite" "$out/logs_2.sqlite"
  backup_sqlite_if_exists "$codex_home/memories_1.sqlite" "$out/memories_1.sqlite"
  backup_sqlite_if_exists "$codex_home/goals_1.sqlite" "$out/goals_1.sqlite"

  copy_dir_if_exists "$codex_home/sessions" "$out/sessions"
  copy_dir_if_exists "$codex_home/archived_sessions" "$out/archived_sessions"
  copy_dir_if_exists "$codex_home/ambient-suggestions" "$out/ambient-suggestions"
  copy_dir_if_exists "$codex_home/attachments" "$out/attachments"
  copy_dir_if_exists "$codex_home/generated_images" "$out/generated_images"

  write_manifest "$out" "$codex_home" "export"
  printf 'Exported Codex sidebar/session bundle: %s\n' "$out"
}

backup_target() {
  local codex_home="$1"
  local backup_dir="$codex_home/backups/sidebar-state-sync-$(timestamp)"
  mkdir -p "$backup_dir"
  export_bundle "$codex_home" "$backup_dir" >/dev/null
  printf '%s\n' "$backup_dir"
}

restore_sqlite_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -f "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    cp -p "$src" "$dst"
  fi
}

refresh_sidebar_state() {
  local codex_home="$1"
  local marker_dir="$codex_home/backups/sidebar-state-sync-refresh"
  mkdir -p "$marker_dir"
  if [[ -f "$codex_home/state_5.sqlite" ]]; then
    local check
    check="$(sqlite3 "$codex_home/state_5.sqlite" 'pragma integrity_check;')"
    [[ "$check" == "ok" ]] || die "state_5.sqlite integrity check failed: $check"
  fi
  printf '{"refreshed_at":"%s","note":"Restart Codex Desktop to reload sidebar state."}\n' "$(timestamp)" \
    >"$marker_dir/last-refresh.json"
  printf 'Refreshed on-disk sidebar state and wrote marker: %s\n' "$marker_dir/last-refresh.json"
}

quit_codex() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    printf 'Codex.app automatic quit is only implemented for macOS.\n' >&2
    return 0
  fi
  /usr/bin/osascript -e 'tell application "Codex" to quit' >/dev/null 2>&1 || true
  sleep 2
}

open_codex() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    printf 'Restart requested, but automatic Codex.app open is only implemented for macOS.\n' >&2
    return 0
  fi
  /usr/bin/open -a Codex
  printf 'Opened Codex.app\n'
}

codex_writer_pids() {
  pgrep -f 'Codex\.app|codex-code-mode-host|/codex([[:space:]]|$)' || true
}

import_bundle() {
  local codex_home="$1"
  local from="$2"
  local refresh="$3"
  local restart="$4"
  local allow_platform_mismatch="$5"
  [[ -d "$from" ]] || die "bundle directory not found: $from"
  [[ -f "$from/manifest.json" ]] || die "bundle manifest not found: $from/manifest.json"
  local source_kind source_platform source_home target_platform
  source_kind="$(sed -n 's/.*"kind"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$from/manifest.json" | head -n 1)"
  [[ "$source_kind" == "codex_sidebar_state_bundle" ]] || die "unrecognized bundle manifest kind: $source_kind"
  source_platform="$(sed -n 's/.*"platform"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$from/manifest.json" | head -n 1)"
  source_home="$(sed -n 's/.*"source_codex_home"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$from/manifest.json" | head -n 1)"
  if [[ -z "$source_platform" ]]; then
    case "$source_home" in
      /Users/*) source_platform="macos" ;;
      /home/*|/root/*) source_platform="linux" ;;
      [A-Za-z]:*|\\\\*) source_platform="windows" ;;
    esac
  fi
  target_platform="$(current_platform)"
  if [[ -n "$source_platform" && "$source_platform" != "$target_platform" && "$allow_platform_mismatch" != "1" ]]; then
    die "refusing $source_platform bundle import into $target_platform; use Agent Bridge pointer-sync instead (or pass --allow-platform-mismatch for explicit disaster recovery)"
  fi
  need_cmd rsync
  need_cmd sqlite3
  need_cmd pgrep
  local writers
  writers="$(codex_writer_pids)"
  if [[ -n "$writers" && "$restart" != "1" ]]; then
    die "close Codex before import, or pass --restart to stop detected writer processes"
  fi
  mkdir -p "$codex_home"
  if [[ "$restart" == "1" ]]; then
    quit_codex
    writers="$(codex_writer_pids)"
    [[ -z "$writers" ]] || die "Codex writer processes are still active: ${writers//$'\n'/,}"
  fi

  local backup_dir
  backup_dir="$(backup_target "$codex_home")"

  copy_file_if_exists "$from/.codex-global-state.json" "$codex_home/.codex-global-state.json"
  copy_file_if_exists "$from/session_index.jsonl" "$codex_home/session_index.jsonl"
  copy_file_if_exists "$from/external_agent_session_imports.json" "$codex_home/external_agent_session_imports.json"
  copy_file_if_exists "$from/config.toml" "$codex_home/config.toml"

  restore_sqlite_if_exists "$from/state_5.sqlite" "$codex_home/state_5.sqlite"
  restore_sqlite_if_exists "$from/sqlite/state_5.sqlite" "$codex_home/sqlite/state_5.sqlite"
  restore_sqlite_if_exists "$from/logs_2.sqlite" "$codex_home/logs_2.sqlite"
  restore_sqlite_if_exists "$from/memories_1.sqlite" "$codex_home/memories_1.sqlite"
  restore_sqlite_if_exists "$from/goals_1.sqlite" "$codex_home/goals_1.sqlite"

  copy_dir_if_exists "$from/sessions" "$codex_home/sessions"
  copy_dir_if_exists "$from/archived_sessions" "$codex_home/archived_sessions"
  copy_dir_if_exists "$from/ambient-suggestions" "$codex_home/ambient-suggestions"
  copy_dir_if_exists "$from/attachments" "$codex_home/attachments"
  copy_dir_if_exists "$from/generated_images" "$codex_home/generated_images"

  printf 'Imported Codex sidebar/session bundle from: %s\n' "$from"
  printf 'Target backup saved at: %s\n' "$backup_dir"

  if [[ "$refresh" == "1" ]]; then
    refresh_sidebar_state "$codex_home"
  fi
  if [[ "$restart" == "1" ]]; then
    open_codex
  fi
}

mode="${1:-}"
[[ -n "$mode" ]] || { usage; exit 2; }
shift || true

codex_home="$(default_codex_home)"
out=""
from=""
yes="0"
refresh="0"
restart="0"
allow_platform_mismatch="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --codex-home)
      codex_home="${2:-}"
      [[ -n "$codex_home" ]] || die "--codex-home requires a value"
      shift 2
      ;;
    --out)
      out="${2:-}"
      [[ -n "$out" ]] || die "--out requires a value"
      shift 2
      ;;
    --from)
      from="${2:-}"
      [[ -n "$from" ]] || die "--from requires a value"
      shift 2
      ;;
    --yes)
      yes="1"
      shift
      ;;
    --refresh-sidebar)
      refresh="1"
      shift
      ;;
    --restart)
      restart="1"
      shift
      ;;
    --allow-platform-mismatch)
      allow_platform_mismatch="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

codex_home="${codex_home/#\~/$HOME}"
if [[ "$codex_home" != /* ]]; then
  codex_home="$(pwd)/$codex_home"
fi
codex_home="${codex_home%/}"

case "$mode" in
  export)
    [[ -n "$out" ]] || die "export requires --out DIR"
    export_bundle "$codex_home" "$out"
    ;;
  import)
    [[ -n "$from" ]] || die "import requires --from DIR"
    [[ "$yes" == "1" ]] || die "import overwrites target state; pass --yes after reviewing the bundle"
    import_bundle "$codex_home" "$from" "$refresh" "$restart" "$allow_platform_mismatch"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    die "unknown mode: $mode"
    ;;
esac
