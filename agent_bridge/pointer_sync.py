"""Pointer-only Codex project and recent-thread catalog synchronization.

This module deliberately does not copy native Codex databases, transcripts,
attachments, prompt previews, or first-user-message fields.  Each runtime
publishes a bounded, atomic metadata snapshot into SharedAgentData.  Other
runtimes can list those pointers without mutating their native Codex state.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

from .correlation import iso_now
from .readiness import machine_id as stable_machine_id
from .readiness import resolve_shared_roots


SCHEMA_VERSION = "1.0"
ARCHIVE_DIR = "AgentBridgePointerSync"
ARCHIVE_VERSION = "v1"
WINDOWS_TASK_NAME = "Agent Bridge Pointer Sync"
SYSTEMD_UNIT_BASENAME = "agent-bridge-pointer-sync"
MACOS_LABEL = "com.nextcz.agent-bridge.pointer-sync"
DEFAULT_RECENT_LIMIT = 100
MAX_RECENT_LIMIT = 500
MAX_PINNED_THREADS = 100
MAX_TITLE_CHARS = 200
LOCK_STALE_SECONDS = 600
MAX_GENERATIONS = 8
GENERATION_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# This is the complete set of native thread columns that may be read.  In
# particular, first_user_message and preview are intentionally absent.
THREAD_COLUMN_ALLOWLIST = (
    "id",
    "title",
    "name",
    "updated_at",
    "updated_at_ms",
    "recency_at",
    "recency_at_ms",
    "cwd",
    "source",
    "archived",
    "is_pinned",
    "git_origin_url",
)

# This is also used as a regression-testable publication contract.
PUBLISHED_CONVERSATION_FIELDS = frozenset(
    {
        "schema_version",
        "thread_id",
        "title",
        "updated_at",
        "updated_at_ms",
        "project_id",
        "machine_id",
        "runtime_id",
        "client",
        "source",
        "archived",
        "pinned",
        "cwd",
        "workspace_status",
        "native_uri",
        "resume",
    }
)

PUBLISHED_PROJECT_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "label",
        "registry_slug",
        "machine_id",
        "runtime_id",
        "roots",
        "primary_root",
        "git_remote",
        "is_git_repository",
        "sources",
    }
)


class PointerSyncError(ValueError):
    """Raised for safe, actionable pointer-sync failures."""


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return value or "unknown"


def _safe_fragment(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value or "unknown"


def _platform_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _runtime_id(platform_name: str | None = None) -> str:
    platform_value = platform_name or _platform_name()
    if platform_value == "windows":
        return "windows-native"
    if platform_value == "macos":
        return "macos-native"
    distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if distro:
        return f"wsl-{_slugify(distro)}"
    try:
        version = Path("/proc/version").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        version = ""
    if "microsoft" in version.casefold():
        try:
            release = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            release = ""
        match = re.search(r"(?m)^ID=[\"']?([^\"'\s]+)", release)
        if match:
            return f"wsl-{_slugify(match.group(1))}"
        return "wsl-linux"
    return "linux-native"


def _resolve_codex_home(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def _resolve_shared_data(value: str | Path | None = None, *, create: bool = False) -> Path:
    if value:
        root = Path(value).expanduser()
    else:
        resolved = resolve_shared_roots(create=create)
        selected = resolved.get("roots", {}).get("data", {}).get("selected", "")
        if not selected:
            raise PointerSyncError("no SharedAgentData root could be resolved; pass --shared-root")
        root = Path(str(selected)).expanduser()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise PointerSyncError(f"SharedAgentData root does not exist: {root}")
    return root


def _archive_root(value: str | Path | None = None, *, create: bool = False) -> Path:
    root = _resolve_shared_data(value, create=create) / ARCHIVE_DIR / ARCHIVE_VERSION
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        pass
    return rows


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8") for row in rows
    )


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_bytes(body)
    try:
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def _local_state_root() -> Path:
    configured = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local" / "state" / "agent-bridge"


@contextmanager
def _publisher_lock(machine: str, runtime: str) -> Iterator[bool]:
    lock_root = _local_state_root() / "pointer-sync"
    lock_root.mkdir(parents=True, exist_ok=True)
    path = lock_root / f"{_safe_fragment(machine)}.{_safe_fragment(runtime)}.lock"
    acquired = False
    try:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = max(0.0, time.time() - path.stat().st_mtime)
            except OSError:
                age = 0.0
            if age <= LOCK_STALE_SECONDS:
                yield False
                return
            path.unlink(missing_ok=True)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                yield False
                return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": iso_now()}, handle)
        acquired = True
        yield True
    finally:
        if acquired:
            path.unlink(missing_ok=True)


def _strip_extended_prefix(value: str) -> str:
    return value[4:] if value.startswith("\\\\?\\") else value


def _is_windows_path(value: str) -> bool:
    value = _strip_extended_prefix(value)
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")


def _is_wsl_unc(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith("\\\\wsl$\\") or lowered.startswith("\\\\wsl.localhost\\")


def _path_compatible(value: str, runtime: str) -> bool:
    if not value:
        return False
    if runtime == "windows-native":
        return _is_windows_path(value) and not _is_wsl_unc(value)
    if runtime.startswith("wsl-") or runtime == "linux-native":
        return value.startswith("/")
    if runtime == "macos-native":
        return value.startswith("/")
    return False


def _path_exists(value: str) -> bool:
    try:
        return Path(_strip_extended_prefix(value)).expanduser().exists()
    except OSError:
        return False


def _path_key(value: str, runtime: str) -> str:
    normalized = _strip_extended_prefix(value).replace("\\", "/").rstrip("/")
    return normalized.casefold() if runtime == "windows-native" else normalized


def _path_within(value: str, root: str, runtime: str) -> bool:
    candidate = _path_key(value, runtime)
    parent = _path_key(root, runtime)
    return candidate == parent or candidate.startswith(parent + "/")


def _normalize_git_remote(value: str) -> str:
    remote = value.strip()
    if not remote:
        return ""
    # Local-path remotes do not provide a portable identity and would disclose
    # an unrelated filesystem pointer. Only publish network remote identities.
    if (
        remote.casefold().startswith("file:")
        or remote.startswith(("/", "\\\\"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", remote))
    ):
        return ""
    scp = re.match(r"^(?:[^@/]+@)?([^:/]+):(.+)$", remote)
    if scp and "://" not in remote and not re.match(r"^[A-Za-z]:[\\/]", remote):
        host, path = scp.groups()
        return f"{host}/{path}".rstrip("/").removesuffix(".git").casefold()
    parsed = urlsplit(remote)
    if parsed.scheme and parsed.hostname:
        path = parsed.path.strip("/").removesuffix(".git")
        return f"{parsed.hostname}/{path}".casefold()
    return ""


def _git_info(path: str, cache: dict[str, dict[str, str]], *, timeout: float = 2.0) -> dict[str, str]:
    cache_key = _strip_extended_prefix(path)
    if cache_key in cache:
        return cache[cache_key]
    result = {"root": "", "remote": ""}
    if not _path_exists(cache_key) or not shutil.which("git"):
        cache[cache_key] = result
        return result

    def run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", cache_key, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout.strip() if completed.returncode == 0 else ""

    result["root"] = run("rev-parse", "--show-toplevel")
    if result["root"]:
        result["remote"] = run("remote", "get-url", "origin")
    cache[cache_key] = result
    return result


def _registry_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise PointerSyncError(f"project registry does not exist: {path}")
        return path
    resolved = resolve_shared_roots(create=False)
    row = resolved.get("roots", {}).get("conversations", {})
    candidates = [Path(value) for value in row.get("existing", [])]
    selected = row.get("selected")
    if selected:
        candidates.insert(0, Path(str(selected)))
    seen: set[str] = set()
    matches: list[Path] = []
    for root in candidates:
        path = root / "projects" / "_registry" / "projects.json"
        key = str(path)
        if key not in seen and path.is_file():
            seen.add(key)
            matches.append(path)
    if not matches:
        return None
    matches.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    return matches[0]


def _registry_maps(registry: dict[str, Any], runtime: str) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    exact: dict[str, tuple[str, str]] = {}
    basename_candidates: dict[str, set[tuple[str, str]]] = {}
    projects = registry.get("projects") if isinstance(registry.get("projects"), list) else []
    for project in projects:
        if not isinstance(project, dict):
            continue
        slug = str(project.get("slug") or _slugify(str(project.get("name") or "project")))
        name = str(project.get("name") or slug)
        aliases: list[str] = []
        for field in (
            "workspace_windows",
            "workspace_windows_aliases",
            "workspace_linux",
            "workspace_linux_aliases",
            "workspace_macos",
            "workspace_macos_aliases",
        ):
            raw = project.get(field)
            aliases.extend(str(item) for item in (raw if isinstance(raw, list) else [raw]) if isinstance(item, str))
        workspaces = project.get("workspaces")
        for workspace in workspaces if isinstance(workspaces, list) else [workspaces]:
            if isinstance(workspace, dict) and isinstance(workspace.get("path"), str):
                aliases.append(str(workspace["path"]))
        for alias in aliases:
            exact[_path_key(alias, runtime)] = (slug, name)
            basename = alias.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
            if basename:
                basename_candidates.setdefault(basename, set()).add((slug, name))
    unique_basename = {
        basename: next(iter(values)) for basename, values in basename_candidates.items() if len(values) == 1
    }
    return exact, unique_basename


def _registry_match(
    path: str,
    *,
    runtime: str,
    exact: dict[str, tuple[str, str]],
    basenames: dict[str, tuple[str, str]],
) -> tuple[str, str] | None:
    direct = exact.get(_path_key(path, runtime))
    if direct:
        return direct
    basename = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].casefold()
    return basenames.get(basename)


def _state_db_path(codex_home: Path) -> Path:
    for candidate in (codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"):
        if candidate.is_file():
            return candidate
    raise PointerSyncError(f"Codex state_5.sqlite was not found under {codex_home}")


def _read_threads(db_path: Path) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            available = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
            selected = [column for column in THREAD_COLUMN_ALLOWLIST if column in available]
            if "id" not in selected:
                raise PointerSyncError(f"threads table in {db_path} has no id column")
            query = "SELECT " + ", ".join(f'"{column}"' for column in selected) + " FROM threads"
            return [dict(row) for row in connection.execute(query)]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise PointerSyncError(f"could not read Codex thread index {db_path}: {exc}") from exc


def _thread_timestamp_ms(row: dict[str, Any]) -> int:
    for field, multiplier in (
        ("recency_at_ms", 1),
        ("updated_at_ms", 1),
        ("recency_at", 1000),
        ("updated_at", 1000),
    ):
        value = row.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return int(value * multiplier)
    return 0


def _iso_from_ms(value: int) -> str:
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    title = " ".join(value.split())
    if len(title) <= MAX_TITLE_CHARS:
        return title
    return title[: MAX_TITLE_CHARS - 3].rstrip() + "..."


def _config_project_paths(codex_home: Path) -> list[str]:
    path = codex_home / "config.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    projects = value.get("projects")
    return [str(key) for key in projects] if isinstance(projects, dict) else []


def _candidate_projects(
    codex_home: Path,
    global_state: dict[str, Any],
    threads: list[dict[str, Any]],
    *,
    runtime: str,
    discover_code_roots: bool,
    discover_thread_roots: bool,
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    local_projects = global_state.get("local-projects")
    if isinstance(local_projects, dict):
        for project in local_projects.values():
            if not isinstance(project, dict):
                continue
            name = str(project.get("name") or "project")
            roots = project.get("rootPaths")
            for root in roots if isinstance(roots, list) else []:
                if isinstance(root, str):
                    candidates.append((name, root, "codex-global-state"))
    for root in _config_project_paths(codex_home):
        candidates.append((Path(root).name or "project", root, "codex-config"))
    if discover_code_roots:
        for parent in (Path.home() / "Code", Path.home() / "code"):
            if not parent.is_dir():
                continue
            try:
                children = sorted(path for path in parent.iterdir() if path.is_dir())
            except OSError:
                children = []
            for child in children:
                if (child / ".git").exists():
                    candidates.append((child.name, str(child), "code-root-scan"))
    if discover_thread_roots:
        for thread in threads:
            cwd = str(thread.get("cwd") or "")
            if cwd and _path_compatible(cwd, runtime) and _path_exists(cwd):
                candidates.append((Path(_strip_extended_prefix(cwd)).name or "project", cwd, "recent-thread"))
    return candidates


def _project_records(
    codex_home: Path,
    global_state: dict[str, Any],
    threads: list[dict[str, Any]],
    registry: dict[str, Any],
    *,
    runtime: str,
    machine: str,
    discover_code_roots: bool,
    discover_thread_roots: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    exact, basenames = _registry_maps(registry, runtime)
    git_cache: dict[str, dict[str, str]] = {}
    merged: dict[str, dict[str, Any]] = {}
    root_to_project: dict[str, dict[str, str]] = {}
    seen_candidates: set[str] = set()
    for label, candidate, source in _candidate_projects(
        codex_home,
        global_state,
        threads,
        runtime=runtime,
        discover_code_roots=discover_code_roots,
        discover_thread_roots=discover_thread_roots,
    ):
        if not _path_compatible(candidate, runtime) or not _path_exists(candidate):
            continue
        key = _path_key(candidate, runtime)
        if key in seen_candidates:
            continue
        seen_candidates.add(key)
        git = _git_info(candidate, git_cache)
        root = git.get("root") or _strip_extended_prefix(candidate)
        registry_match = _registry_match(root, runtime=runtime, exact=exact, basenames=basenames)
        registry_slug, registry_name = registry_match or ("", "")
        canonical_remote = _normalize_git_remote(git.get("remote", ""))
        if canonical_remote:
            logical_id = f"git:{canonical_remote}"
        elif registry_slug:
            logical_id = f"registry:{registry_slug}"
        else:
            logical_id = f"name:{_slugify(label or Path(root).name)}"
        record = merged.setdefault(
            logical_id,
            {
                "schema_version": SCHEMA_VERSION,
                "project_id": logical_id,
                "label": registry_name or label or Path(root).name,
                "registry_slug": registry_slug,
                "machine_id": machine,
                "runtime_id": runtime,
                "roots": [],
                "primary_root": root,
                "git_remote": canonical_remote,
                "is_git_repository": bool(git.get("root")),
                "sources": [],
            },
        )
        if root not in record["roots"]:
            record["roots"].append(root)
        if source not in record["sources"]:
            record["sources"].append(source)
        if registry_slug and not record.get("registry_slug"):
            record["registry_slug"] = registry_slug
        root_to_project[_path_key(root, runtime)] = {
            "project_id": logical_id,
            "root": root,
        }
    projects = list(merged.values())
    for project in projects:
        project["roots"].sort(key=lambda value: (_path_key(value, runtime) != _path_key(project["primary_root"], runtime), value))
        project["sources"].sort()
        if set(project) != PUBLISHED_PROJECT_FIELDS:
            raise PointerSyncError("internal project publication field contract drifted")
    projects.sort(key=lambda row: (str(row.get("label") or "").casefold(), str(row["project_id"])))
    return projects, root_to_project


def _conversation_records(
    threads: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    *,
    runtime: str,
    machine: str,
    client: str,
    recent_limit: int,
) -> list[dict[str, Any]]:
    ordered = sorted(threads, key=_thread_timestamp_ms, reverse=True)
    chosen = ordered[:recent_limit]
    chosen_ids = {str(row.get("id") or "") for row in chosen}
    pinned = [row for row in ordered if bool(row.get("is_pinned")) and str(row.get("id") or "") not in chosen_ids]
    chosen.extend(pinned[:MAX_PINNED_THREADS])
    roots: list[tuple[int, str, str]] = []
    for project in projects:
        for root in project.get("roots", []):
            roots.append((len(_path_key(str(root), runtime)), str(root), str(project["project_id"])))
    roots.sort(reverse=True)
    records: list[dict[str, Any]] = []
    for thread in chosen:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        cwd = str(thread.get("cwd") or "")
        compatible = _path_compatible(cwd, runtime)
        exists = compatible and _path_exists(cwd)
        workspace_status = "available" if exists else ("missing" if compatible and cwd else "foreign_or_unknown")
        published_cwd = _strip_extended_prefix(cwd) if compatible else ""
        project_id = ""
        remote = _normalize_git_remote(str(thread.get("git_origin_url") or ""))
        if remote:
            candidate_id = f"git:{remote}"
            if any(project.get("project_id") == candidate_id for project in projects):
                project_id = candidate_id
        if not project_id and published_cwd:
            for _, root, candidate_id in roots:
                if _path_within(published_cwd, root, runtime):
                    project_id = candidate_id
                    break
        updated_ms = _thread_timestamp_ms(thread)
        record = {
            "schema_version": SCHEMA_VERSION,
            "thread_id": thread_id,
            "title": _clean_title(thread.get("name") or thread.get("title")),
            "updated_at": _iso_from_ms(updated_ms),
            "updated_at_ms": updated_ms,
            "project_id": project_id,
            "machine_id": machine,
            "runtime_id": runtime,
            "client": client,
            "source": str(thread.get("source") or ""),
            "archived": bool(thread.get("archived")),
            "pinned": bool(thread.get("is_pinned")),
            "cwd": published_cwd,
            "workspace_status": workspace_status,
            "native_uri": f"codex://threads/{thread_id}",
            "resume": {"client": "codex", "session_id": thread_id},
        }
        if set(record) != PUBLISHED_CONVERSATION_FIELDS:
            raise PointerSyncError("internal pointer publication field contract drifted")
        records.append(record)
    records.sort(key=lambda row: (int(row["updated_at_ms"]), str(row["thread_id"])), reverse=True)
    return records


def _merge_previous_projects(
    current: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    *,
    machine: str,
    runtime: str,
) -> list[dict[str, Any]]:
    """Preserve a prior full scan during a lightweight startup publication."""

    merged = {str(row["project_id"]): row for row in current}
    for old in previous:
        if (
            set(old) != PUBLISHED_PROJECT_FIELDS
            or str(old.get("machine_id") or "") != machine
            or str(old.get("runtime_id") or "") != runtime
        ):
            continue
        project_id = str(old.get("project_id") or "")
        if not project_id:
            continue
        existing = merged.get(project_id)
        if existing is None:
            preserved = dict(old)
            preserved["roots"] = list(old.get("roots") or [])
            preserved["sources"] = sorted(set(old.get("sources") or []) | {"previous-full-snapshot"})
            merged[project_id] = preserved
            continue
        for root in old.get("roots") or []:
            if isinstance(root, str) and root not in existing["roots"]:
                existing["roots"].append(root)
        existing["sources"] = sorted(
            set(existing.get("sources") or []) | set(old.get("sources") or []) | {"previous-full-snapshot"}
        )
        if not existing.get("git_remote") and old.get("git_remote"):
            existing["git_remote"] = str(old["git_remote"])
            existing["is_git_repository"] = bool(old.get("is_git_repository"))
    rows = list(merged.values())
    for row in rows:
        row["roots"] = sorted(
            set(row.get("roots") or []),
            key=lambda value: (_path_key(str(value), runtime) != _path_key(str(row["primary_root"]), runtime), str(value)),
        )
        if set(row) != PUBLISHED_PROJECT_FIELDS:
            raise PointerSyncError("preserved project publication field contract drifted")
    rows.sort(key=lambda row: (str(row.get("label") or "").casefold(), str(row["project_id"])))
    return rows


def publish_pointer_snapshot(
    *,
    codex_home: str | Path | None = None,
    shared_root: str | Path | None = None,
    project_registry: str | Path | None = None,
    machine_id: str | None = None,
    runtime_id: str | None = None,
    client: str = "codex",
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    discover_code_roots: bool = False,
    discover_thread_roots: bool = True,
    preserve_existing_projects: bool = False,
) -> dict[str, Any]:
    limit = max(1, min(MAX_RECENT_LIMIT, int(recent_limit)))
    home = _resolve_codex_home(codex_home)
    if not home.is_dir():
        raise PointerSyncError(f"Codex home does not exist: {home}")
    machine = _safe_fragment(machine_id or stable_machine_id())
    runtime = _safe_fragment(runtime_id or _runtime_id())
    archive = _archive_root(shared_root, create=True)
    target = archive / "machines" / machine / runtime
    registry_path = _registry_path(project_registry)
    registry = _read_json(registry_path, {"projects": []}) if registry_path else {"projects": []}
    if not isinstance(registry, dict):
        registry = {"projects": []}
    with _publisher_lock(machine, runtime) as acquired:
        if not acquired:
            return {
                "ok": True,
                "status": "busy",
                "machine_id": machine,
                "runtime_id": runtime,
                "archive_root": str(archive),
            }
        state = _read_json(home / ".codex-global-state.json", {})
        if not isinstance(state, dict):
            state = {}
        threads = _read_threads(_state_db_path(home))
        projects, _ = _project_records(
            home,
            state,
            threads,
            registry,
            runtime=runtime,
            machine=machine,
            discover_code_roots=discover_code_roots,
            discover_thread_roots=discover_thread_roots,
        )
        if preserve_existing_projects:
            previous = _load_source(target)
            if previous is not None:
                projects = _merge_previous_projects(
                    projects,
                    previous["projects"],
                    machine=machine,
                    runtime=runtime,
                )
        conversations = _conversation_records(
            threads,
            projects,
            runtime=runtime,
            machine=machine,
            client=client,
            recent_limit=limit,
        )
        project_body = _jsonl_bytes(projects)
        conversation_body = _jsonl_bytes(conversations)
        generation_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + f"-{os.getpid()}-{hashlib.sha256(project_body + conversation_body).hexdigest()[:12]}"
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "kind": "agent_bridge_pointer_snapshot",
            "generated_at": iso_now(),
            "machine_id": machine,
            "platform": _platform_name(),
            "runtime_id": runtime,
            "client": client,
            "project_count": len(projects),
            "conversation_count": len(conversations),
            "recent_limit": limit,
            "content_policy": "pointers_only_no_transcripts_no_prompt_bodies",
            "generation_id": generation_id,
            "files": {
                "projects.jsonl": hashlib.sha256(project_body).hexdigest(),
                "conversations.jsonl": hashlib.sha256(conversation_body).hexdigest(),
            },
        }
        manifest_body = _json_bytes(manifest)
        generation = target / "generations" / generation_id
        _atomic_write(generation / "projects.jsonl", project_body)
        _atomic_write(generation / "conversations.jsonl", conversation_body)
        _atomic_write(generation / "manifest.json", manifest_body)
        current = {
            "schema_version": SCHEMA_VERSION,
            "kind": "agent_bridge_pointer_current",
            "generation_id": generation_id,
            "generated_at": manifest["generated_at"],
            "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
        }
        _atomic_write(target / "current.json", _json_bytes(current))
        _prune_generations(target, keep=MAX_GENERATIONS)
    return {"ok": True, "status": "published", "archive_root": str(archive), **manifest}


def _load_snapshot_dir(path: Path, *, expected_manifest_sha256: str = "") -> dict[str, Any] | None:
    try:
        manifest_body = (path / "manifest.json").read_bytes()
    except OSError:
        return None
    if expected_manifest_sha256 and hashlib.sha256(manifest_body).hexdigest() != expected_manifest_sha256:
        return None
    try:
        manifest = json.loads(manifest_body)
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict) or manifest.get("kind") != "agent_bridge_pointer_snapshot":
        return None
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return None
    files = manifest.get("files")
    if not isinstance(files, dict):
        return None
    bodies: dict[str, bytes] = {}
    for name in ("projects.jsonl", "conversations.jsonl"):
        try:
            body = (path / name).read_bytes()
        except OSError:
            return None
        if hashlib.sha256(body).hexdigest() != files.get(name):
            return None
        bodies[name] = body
    return {
        "path": path,
        "manifest": manifest,
        "projects": _read_jsonl(path / "projects.jsonl"),
        "conversations": _read_jsonl(path / "conversations.jsonl"),
    }


def _load_source(path: Path) -> dict[str, Any] | None:
    current = _read_json(path / "current.json", None)
    attempted: set[str] = set()
    if isinstance(current, dict) and current.get("kind") == "agent_bridge_pointer_current":
        generation_id = str(current.get("generation_id") or "")
        if GENERATION_NAME_RE.fullmatch(generation_id):
            attempted.add(generation_id)
            source = _load_snapshot_dir(
                path / "generations" / generation_id,
                expected_manifest_sha256=str(current.get("manifest_sha256") or ""),
            )
            if source is not None:
                return source
    generations = path / "generations"
    if generations.is_dir():
        candidates = sorted(
            (candidate for candidate in generations.iterdir() if candidate.is_dir()),
            key=lambda candidate: candidate.name,
            reverse=True,
        )
        for candidate in candidates:
            if candidate.name in attempted or not GENERATION_NAME_RE.fullmatch(candidate.name):
                continue
            source = _load_snapshot_dir(candidate)
            if source is not None:
                return source
    # Backward-compatible fallback for the original direct-file layout.
    return _load_snapshot_dir(path)


def _prune_generations(target: Path, *, keep: int) -> None:
    generations = target / "generations"
    try:
        candidates = sorted(
            (
                candidate
                for candidate in generations.iterdir()
                if candidate.is_dir() and GENERATION_NAME_RE.fullmatch(candidate.name)
            ),
            key=lambda candidate: candidate.name,
            reverse=True,
        )
    except OSError:
        return
    for candidate in candidates[max(2, keep) :]:
        manifest = _read_json(candidate / "manifest.json", None)
        if not isinstance(manifest, dict) or manifest.get("kind") != "agent_bridge_pointer_snapshot":
            continue
        try:
            shutil.rmtree(candidate)
        except OSError:
            pass


def list_pointer_sources(*, shared_root: str | Path | None = None) -> list[dict[str, Any]]:
    archive = _archive_root(shared_root, create=False)
    rows: list[dict[str, Any]] = []
    machines = archive / "machines"
    if not machines.is_dir():
        return rows
    for runtime_path in sorted(path for path in machines.glob("*/*") if path.is_dir()):
        source = _load_source(runtime_path)
        if source is None:
            continue
        manifest = source["manifest"]
        rows.append(
            {
                "machine_id": str(manifest.get("machine_id") or ""),
                "runtime_id": str(manifest.get("runtime_id") or ""),
                "platform": str(manifest.get("platform") or ""),
                "generated_at": str(manifest.get("generated_at") or ""),
                "project_count": int(manifest.get("project_count") or 0),
                "conversation_count": int(manifest.get("conversation_count") or 0),
                "content_policy": str(manifest.get("content_policy") or ""),
                "path": str(source["path"]),
            }
        )
    rows.sort(key=lambda row: (row["generated_at"], row["machine_id"], row["runtime_id"]), reverse=True)
    return rows


def _all_sources(shared_root: str | Path | None = None) -> list[dict[str, Any]]:
    archive = _archive_root(shared_root, create=False)
    result: list[dict[str, Any]] = []
    machines = archive / "machines"
    if not machines.is_dir():
        return result
    for runtime_path in sorted(path for path in machines.glob("*/*") if path.is_dir()):
        source = _load_source(runtime_path)
        if source is not None:
            result.append(source)
    return result


def recent_pointers(
    *,
    shared_root: str | Path | None = None,
    limit: int = 50,
    project_id: str | None = None,
) -> dict[str, Any]:
    maximum = max(1, min(MAX_RECENT_LIMIT, int(limit)))
    merged: dict[str, dict[str, Any]] = {}
    availability: dict[str, set[str]] = {}
    for source in _all_sources(shared_root):
        for row in source["conversations"]:
            thread_id = str(row.get("thread_id") or "")
            if not thread_id or (project_id and row.get("project_id") != project_id):
                continue
            availability.setdefault(thread_id, set()).add(
                f"{row.get('machine_id', '')}/{row.get('runtime_id', '')}"
            )
            current = merged.get(thread_id)
            if current is None or int(row.get("updated_at_ms") or 0) > int(current.get("updated_at_ms") or 0):
                merged[thread_id] = dict(row)
    rows = sorted(
        merged.values(),
        key=lambda row: (int(row.get("updated_at_ms") or 0), str(row.get("thread_id") or "")),
        reverse=True,
    )[:maximum]
    for row in rows:
        row["available_on"] = sorted(availability.get(str(row["thread_id"]), set()))
    return {"ok": True, "count": len(rows), "limit": maximum, "conversations": rows}


def project_catalog(*, shared_root: str | Path | None = None) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    for source in _all_sources(shared_root):
        for row in source["projects"]:
            project_id = str(row.get("project_id") or "")
            if not project_id:
                continue
            target = merged.setdefault(
                project_id,
                {
                    "project_id": project_id,
                    "label": str(row.get("label") or project_id),
                    "registry_slug": str(row.get("registry_slug") or ""),
                    "aliases": [],
                },
            )
            target["aliases"].append(
                {
                    "machine_id": str(row.get("machine_id") or ""),
                    "runtime_id": str(row.get("runtime_id") or ""),
                    "roots": list(row.get("roots") or []),
                    "git_remote": str(row.get("git_remote") or ""),
                    "is_git_repository": bool(row.get("is_git_repository")),
                }
            )
    rows = list(merged.values())
    for row in rows:
        row["aliases"].sort(key=lambda alias: (alias["machine_id"], alias["runtime_id"]))
    rows.sort(key=lambda row: (row["label"].casefold(), row["project_id"]))
    return {"ok": True, "count": len(rows), "projects": rows}


def _agent_command() -> str:
    configured = os.environ.get("AGENT_BRIDGE_HOOK_AGENT")
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("agent")
    return found or str(Path.home() / ".local" / "bin" / ("agent.cmd" if os.name == "nt" else "agent"))


def _scheduler_command(
    *,
    shared_root: Path,
    codex_home: Path,
    recent_limit: int,
    machine_id: str | None = None,
    include_code_scan: bool = False,
) -> list[str]:
    command = [
        _agent_command(),
        "code",
        "pointer-sync",
        "publish",
        "--shared-root",
        str(shared_root),
        "--codex-home",
        str(codex_home),
        "--recent-limit",
        str(recent_limit),
        "--code-scan" if include_code_scan else "--no-code-scan",
        "--quiet",
    ]
    if machine_id:
        command.extend(["--machine-id", machine_id])
    return command


def _systemd_exec_arg(value: str) -> str:
    """Quote one absolute path for systemd ExecStart syntax."""

    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _windows_batch_arg(value: str) -> str:
    """Quote a batch-file argument with delayed expansion left disabled."""

    return '"' + value.replace("%", "%%") + '"'


def _systemd_status() -> dict[str, Any]:
    timer = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT_BASENAME}.timer"
    result = {"installed": timer.is_file(), "timer": str(timer), "active": False, "enabled": False}
    if not timer.is_file() or not shutil.which("systemctl"):
        return result
    active = subprocess.run(
        ["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT_BASENAME}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    enabled = subprocess.run(
        ["systemctl", "--user", "is-enabled", f"{SYSTEMD_UNIT_BASENAME}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    result["active"] = active.returncode == 0
    result["enabled"] = enabled.returncode == 0
    return result


def pointer_scheduler_status(platform_name: str | None = None) -> dict[str, Any]:
    platform_value = platform_name or _platform_name()
    if platform_value == "windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        return {"installed": result.returncode == 0, "name": WINDOWS_TASK_NAME}
    if platform_value == "linux":
        return _systemd_status()
    if platform_value == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
        return {"installed": plist.is_file(), "path": str(plist)}
    return {"installed": False, "supported": False}


def install_pointer_scheduler(
    *,
    platform_name: str | None = None,
    interval_seconds: int = 3600,
    shared_root: str | Path | None = None,
    codex_home: str | Path | None = None,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
    machine_id: str | None = None,
    include_code_scan: bool = False,
) -> dict[str, Any]:
    platform_value = platform_name or _platform_name()
    interval = max(300, int(interval_seconds))
    shared = _resolve_shared_data(shared_root, create=True)
    home = _resolve_codex_home(codex_home)
    command = _scheduler_command(
        shared_root=shared,
        codex_home=home,
        recent_limit=max(1, min(MAX_RECENT_LIMIT, int(recent_limit))),
        machine_id=machine_id,
        include_code_scan=include_code_scan,
    )
    state = _local_state_root() / "pointer-sync"
    state.mkdir(parents=True, exist_ok=True)
    if platform_value == "windows":
        wrapper = state / "run-pointer-sync.cmd"
        log = state / "scheduler.log"
        body = (
            "@echo off\r\n"
            + " ".join(_windows_batch_arg(value) for value in command)
            + f" >> {_windows_batch_arg(str(log))} 2>&1\r\n"
            + "exit /b %ERRORLEVEL%\r\n"
        )
        _atomic_write(wrapper, body.encode("utf-8"))
        minutes = max(5, int(round(interval / 60)))
        result = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                WINDOWS_TASK_NAME,
                "/TR",
                f'"{wrapper}"',
                "/SC",
                "MINUTE",
                "/MO",
                str(minutes),
                "/F",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PointerSyncError(f"schtasks creation failed: {result.stderr.strip() or result.stdout.strip()}")
        return {
            "ok": True,
            "platform": platform_value,
            "scheduler": WINDOWS_TASK_NAME,
            "wrapper": str(wrapper),
            "interval_seconds": minutes * 60,
            "command": command,
        }
    if platform_value == "linux":
        if not shutil.which("systemctl"):
            raise PointerSyncError("systemctl is required for the Linux pointer-sync scheduler")
        wrapper = state / "run-pointer-sync.sh"
        _atomic_write(wrapper, ("#!/bin/sh\nexec " + shlex.join(command) + "\n").encode("utf-8"))
        wrapper.chmod(0o700)
        unit_root = Path.home() / ".config" / "systemd" / "user"
        service = unit_root / f"{SYSTEMD_UNIT_BASENAME}.service"
        timer = unit_root / f"{SYSTEMD_UNIT_BASENAME}.timer"
        service_body = (
            "# Managed by Agent Bridge pointer-sync\n"
            "[Unit]\nDescription=Publish Agent Bridge Codex pointer catalog\n\n"
            "[Service]\nType=oneshot\n"
            f"ExecStart={_systemd_exec_arg(str(wrapper))}\n"
        ).encode("utf-8")
        timer_body = (
            "# Managed by Agent Bridge pointer-sync\n"
            "[Unit]\nDescription=Publish Agent Bridge Codex pointer catalog hourly\n\n"
            "[Timer]\nOnBootSec=2min\n"
            f"OnUnitActiveSec={interval}s\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ).encode("utf-8")
        _atomic_write(service, service_body)
        _atomic_write(timer, timer_body)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", timer.name],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PointerSyncError(f"systemd timer installation failed: {result.stderr.strip() or result.stdout.strip()}")
        return {
            "ok": True,
            "platform": platform_value,
            "scheduler": str(timer),
            "wrapper": str(wrapper),
            "interval_seconds": interval,
            "command": command,
        }
    if platform_value == "macos":
        if sys.platform != "darwin":
            raise PointerSyncError("macOS scheduler installation must run on macOS")
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
        logs = state / "scheduler.log"
        plist = {
            "Label": MACOS_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "StartInterval": interval,
            "StandardOutPath": str(logs),
            "StandardErrorPath": str(logs),
            "ProcessType": "Background",
        }
        _atomic_write(plist_path, plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True))
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True, check=False)
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PointerSyncError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
        return {
            "ok": True,
            "platform": platform_value,
            "scheduler": str(plist_path),
            "interval_seconds": interval,
            "command": command,
        }
    raise PointerSyncError(f"scheduler installation is not implemented for {platform_value}")


def uninstall_pointer_scheduler(platform_name: str | None = None) -> dict[str, Any]:
    platform_value = platform_name or _platform_name()
    if platform_value == "windows":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout + result.stderr).casefold()
        if result.returncode != 0 and "cannot find" not in output and "does not exist" not in output:
            raise PointerSyncError(result.stderr.strip() or result.stdout.strip())
        return {"ok": True, "status": "removed", "scheduler": WINDOWS_TASK_NAME}
    if platform_value == "linux":
        unit_root = Path.home() / ".config" / "systemd" / "user"
        service = unit_root / f"{SYSTEMD_UNIT_BASENAME}.service"
        timer = unit_root / f"{SYSTEMD_UNIT_BASENAME}.timer"
        for path in (service, timer):
            if path.is_file() and "Managed by Agent Bridge pointer-sync" not in path.read_text(
                encoding="utf-8", errors="ignore"
            ):
                raise PointerSyncError(f"refusing to remove modified scheduler file: {path}")
        subprocess.run(["systemctl", "--user", "disable", "--now", timer.name], capture_output=True, check=False)
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        return {"ok": True, "status": "removed", "scheduler": str(timer)}
    if platform_value == "macos":
        if sys.platform != "darwin":
            raise PointerSyncError("macOS scheduler removal must run on macOS")
        plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
        if plist.is_file():
            value = plistlib.loads(plist.read_bytes())
            if value.get("Label") != MACOS_LABEL:
                raise PointerSyncError(f"refusing to remove modified scheduler: {plist}")
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True, check=False
            )
            plist.unlink()
        return {"ok": True, "status": "removed", "scheduler": str(plist)}
    raise PointerSyncError(f"scheduler removal is not implemented for {platform_value}")


def pointer_sync_status(*, shared_root: str | Path | None = None) -> dict[str, Any]:
    shared = _resolve_shared_data(shared_root, create=False)
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "archive_root": str(shared / ARCHIVE_DIR / ARCHIVE_VERSION),
        "current_machine": stable_machine_id(),
        "current_runtime": _runtime_id(),
        "scheduler": pointer_scheduler_status(),
        "sources": list_pointer_sources(shared_root=shared),
    }


def pointer_sync_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code pointer-sync",
        description="Publish and inspect a bounded Codex project/recent-task pointer catalog.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def shared_options(command: argparse.ArgumentParser) -> None:
        command.add_argument("--shared-root", help="SharedAgentData root")

    publish = sub.add_parser("publish", help="Publish this runtime's pointer-only snapshot")
    shared_options(publish)
    publish.add_argument("--codex-home")
    publish.add_argument("--project-registry")
    publish.add_argument("--machine-id")
    publish.add_argument("--runtime-id")
    publish.add_argument("--client", default="codex")
    publish.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_LIMIT)
    code_scan = publish.add_mutually_exclusive_group()
    code_scan.add_argument("--code-scan", action="store_true", help="Also enumerate every Git repo under ~/Code")
    code_scan.add_argument("--no-code-scan", action="store_true", help=argparse.SUPPRESS)
    publish.add_argument("--no-thread-root-scan", action="store_true")
    publish.add_argument("--preserve-existing-projects", action="store_true")
    publish.add_argument("--quiet", action="store_true")

    status = sub.add_parser("status", help="Show pointer sources and scheduler state")
    shared_options(status)

    recent = sub.add_parser("recent", help="List recent pointers across published runtimes")
    shared_options(recent)
    recent.add_argument("--limit", type=int, default=50)
    recent.add_argument("--project-id")

    projects = sub.add_parser("projects", help="List logical projects and runtime aliases")
    shared_options(projects)

    install = sub.add_parser("install-scheduler", help="Install a publish-only periodic scheduler")
    shared_options(install)
    install.add_argument("--codex-home")
    install.add_argument("--platform", choices=["auto", "windows", "linux", "macos"], default="auto")
    install.add_argument("--interval-seconds", type=int, default=3600)
    install.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_LIMIT)
    install.add_argument("--machine-id")
    install.add_argument(
        "--include-code-scan",
        action="store_true",
        help="Opt in to publishing every Git repo under ~/Code",
    )

    uninstall = sub.add_parser("uninstall-scheduler", help="Remove only the managed pointer scheduler")
    uninstall.add_argument("--platform", choices=["auto", "windows", "linux", "macos"], default="auto")

    args = parser.parse_args(argv)
    if args.command == "publish":
        result = publish_pointer_snapshot(
            codex_home=args.codex_home,
            shared_root=args.shared_root,
            project_registry=args.project_registry,
            machine_id=args.machine_id,
            runtime_id=args.runtime_id,
            client=args.client,
            recent_limit=args.recent_limit,
            discover_code_roots=args.code_scan and not args.no_code_scan,
            discover_thread_roots=not args.no_thread_root_scan,
            preserve_existing_projects=args.preserve_existing_projects,
        )
    elif args.command == "status":
        result = pointer_sync_status(shared_root=args.shared_root)
    elif args.command == "recent":
        result = recent_pointers(shared_root=args.shared_root, limit=args.limit, project_id=args.project_id)
    elif args.command == "projects":
        result = project_catalog(shared_root=args.shared_root)
    elif args.command == "install-scheduler":
        result = install_pointer_scheduler(
            platform_name=None if args.platform == "auto" else args.platform,
            interval_seconds=args.interval_seconds,
            shared_root=args.shared_root,
            codex_home=args.codex_home,
            recent_limit=args.recent_limit,
            machine_id=args.machine_id,
            include_code_scan=args.include_code_scan,
        )
    else:
        result = uninstall_pointer_scheduler(None if args.platform == "auto" else args.platform)
    if not getattr(args, "quiet", False):
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(pointer_sync_cmd(sys.argv[1:]))
