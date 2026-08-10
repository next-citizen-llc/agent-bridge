"""Retention-safe, incremental Codex session and project synchronization.

The legacy sidebar sync scripts copy an entire native Codex home into a new
timestamped bundle and later replace the target state.  This module provides a
mergeable alternative:

* immutable content-addressed gzip objects for sessions and small assets;
* per-machine manifests and normalized project/thread indexes;
* additive imports that preserve target-only state and remap project paths;
* optional periodic publication through LaunchAgent or Task Scheduler.

No native log database, credentials, config file, or cache is published.  No
session object is deleted automatically. Included session artifacts are copied
as-is: this is a trusted-private-root archive, not encrypted storage, content
redaction, or cryptographically authenticated replication.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import gzip
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from .correlation import iso_now
from .readiness import machine_id as stable_machine_id
from .readiness import resolve_shared_roots


SCHEMA_VERSION = "1.0"
ARCHIVE_DIR = "AgentBridgeStateSync"
ARCHIVE_VERSION = "v1"
SCHEDULER_LABEL = "com.nextcz.agent-bridge.state-sync"
WINDOWS_TASK_NAME = "Agent Bridge State Sync"
CHUNK_SIZE = 4 * 1024 * 1024
MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
ARTIFACT_TREES = {
    "sessions": "session",
    "archived_sessions": "archived_session",
    "attachments": "attachment",
    "generated_images": "generated_image",
}


class StateSyncError(ValueError):
    """Raised for safe, user-actionable state-sync failures."""


def _platform_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "unknown-project"


def _validate_machine_id(value: str) -> str:
    machine = str(value or "")
    if not MACHINE_ID_RE.fullmatch(machine):
        raise StateSyncError(f"invalid machine id {machine!r}")
    return machine


def _path_key(value: str, *, platform_name: str | None = None) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    if (platform_name or "").lower() == "windows" or re.match(r"^[A-Za-z]:/", normalized):
        return normalized.casefold()
    return normalized


def _path_is_within(candidate: str, root: str) -> bool:
    child = _path_key(candidate)
    parent = _path_key(root)
    return bool(parent) and (child == parent or child.startswith(parent + "/"))


def _path_basename(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_native_json_object(path: Path) -> dict[str, Any]:
    """Read mutable native state without treating read failures as empty state."""
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StateSyncError(f"could not read native Codex state {path}: {exc}") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise StateSyncError(f"native Codex state is invalid JSON at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateSyncError(f"native Codex state must be a JSON object: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_bytes(body)
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EBUSY, errno.EPERM} or attempt == 19:
                    raise
                time.sleep(min(1.0, 0.05 * (2**attempt)))
    finally:
        temporary.unlink(missing_ok=True)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode("utf-8")


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, _json_bytes(value))


def _atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    _atomic_write_bytes(path, _jsonl_bytes(rows))


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _resolve_codex_home(value: str | Path | None = None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _resolve_archive_root(value: str | Path | None = None, *, create: bool = False) -> Path:
    if value:
        shared_data = Path(value).expanduser()
    else:
        resolved = resolve_shared_roots(create=create)
        selected = resolved.get("roots", {}).get("data", {}).get("selected", "")
        if not selected:
            raise StateSyncError("no SharedAgentData root could be resolved; pass --shared-root")
        shared_data = Path(str(selected)).expanduser()
    if create:
        shared_data.mkdir(parents=True, exist_ok=True)
    if not shared_data.exists():
        raise StateSyncError(f"SharedAgentData root does not exist: {shared_data}")
    archive = shared_data / ARCHIVE_DIR / ARCHIVE_VERSION
    if create:
        archive.mkdir(parents=True, exist_ok=True)
    return archive


def _default_project_registry() -> Path | None:
    resolved = resolve_shared_roots(create=False)
    selected = resolved.get("roots", {}).get("conversations", {}).get("selected", "")
    if not selected:
        return None
    path = Path(str(selected)) / "projects" / "_registry" / "projects.json"
    return path if path.is_file() else None


def _load_project_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path).expanduser() if path else _default_project_registry()
    if registry_path is None:
        return {"version": 0, "projects": []}
    value = _read_json(registry_path, {"version": 0, "projects": []})
    return value if isinstance(value, dict) else {"version": 0, "projects": []}


def _registry_maps(registry: dict[str, Any]) -> tuple[dict[str, str], dict[str, dict[str, list[str]]], dict[str, str]]:
    path_to_slug: dict[str, str] = {}
    paths_by_slug: dict[str, dict[str, list[str]]] = {}
    names: dict[str, str] = {}
    projects = registry.get("projects") if isinstance(registry.get("projects"), list) else []
    for project in projects:
        if not isinstance(project, dict):
            continue
        slug = str(project.get("slug") or _slugify(str(project.get("name") or "project")))
        names[slug] = str(project.get("name") or slug)
        by_os = paths_by_slug.setdefault(slug, {"macos": [], "windows": [], "linux": [], "unknown": []})

        def add(value: Any, os_name: str) -> None:
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    continue
                if item not in by_os.setdefault(os_name, []):
                    by_os[os_name].append(item)
                path_to_slug[_path_key(item)] = slug

        add(project.get("workspace_macos"), "macos")
        add(project.get("workspace_macos_aliases"), "macos")
        add(project.get("workspace_windows"), "windows")
        add(project.get("workspace_windows_aliases"), "windows")
        add(project.get("workspace_linux"), "linux")
        add(project.get("workspace_linux_aliases"), "linux")
        raw_workspaces = project.get("workspaces")
        workspaces = raw_workspaces if isinstance(raw_workspaces, list) else [raw_workspaces]
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            os_name = str(workspace.get("os") or "unknown").lower()
            add(workspace.get("path"), os_name if os_name in by_os else "unknown")
    return path_to_slug, paths_by_slug, names


def _registry_slug_for_path(path: str, path_to_slug: dict[str, str]) -> str | None:
    key = _path_key(path)
    if key in path_to_slug:
        return path_to_slug[key]
    matches = [(known, slug) for known, slug in path_to_slug.items() if _path_is_within(key, known)]
    if not matches:
        return None
    matches.sort(key=lambda item: len(item[0]), reverse=True)
    return matches[0][1]


def _normalize_git_remote(value: str) -> str:
    remote = str(value or "").strip()
    if not remote:
        return ""
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in remote):
        return ""
    if (
        remote.casefold().startswith("file:")
        or remote.startswith(("/", "\\\\"))
        or remote.startswith(("./", "../", "~/"))
        or bool(re.match(r"^[A-Za-z]:", remote))
    ):
        return ""

    scp = re.fullmatch(r"(?:[^@/:\s]+@)?([^@/:\s]+):([^\s]+)", remote)
    if scp and "://" not in remote:
        host, path = scp.groups()
        path = re.split(r"[?#]", path, maxsplit=1)[0]
    else:
        try:
            parsed = urlsplit(remote)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            return ""
        if parsed.scheme.casefold() not in {"http", "https", "ssh", "git", "git+ssh", "ssh+git"} or not host:
            return ""
        path = parsed.path

    canonical_host = host.casefold()
    if canonical_host.endswith("."):
        canonical_host = canonical_host[:-1]
    if (
        not canonical_host
        or canonical_host.startswith(".")
        or canonical_host.endswith(".")
        or ".." in canonical_host
    ):
        return ""

    path = path.strip("/").removesuffix(".git").rstrip("/")
    if not path:
        return ""
    if canonical_host in {"github.com", "www.github.com", "ssh.github.com"}:
        return path.casefold()
    return f"{canonical_host}/{path}".casefold()


def _git_remote(path: str) -> str:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return _normalize_git_remote(result.stdout.strip()) if result.returncode == 0 else ""


def _ordered_project_ids(state: dict[str, Any]) -> list[str]:
    order = state.get("project-order")
    return [str(value) for value in order] if isinstance(order, list) else []


def _project_records(state: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    path_to_slug, _, registry_names = _registry_maps(registry)
    local_projects = state.get("local-projects") if isinstance(state.get("local-projects"), dict) else {}
    labels = state.get("electron-workspace-root-labels") if isinstance(state.get("electron-workspace-root-labels"), dict) else {}
    order = _ordered_project_ids(state)
    records: list[dict[str, Any]] = []

    if local_projects:
        ids = [project_id for project_id in order if project_id in local_projects]
        ids.extend(project_id for project_id in local_projects if project_id not in ids)
        for position, project_id in enumerate(ids):
            raw = local_projects.get(project_id)
            if not isinstance(raw, dict):
                continue
            roots = [str(path) for path in raw.get("rootPaths", []) if isinstance(path, str) and path]
            primary = roots[0] if roots else ""
            name = str(raw.get("name") or labels.get(primary) or _path_basename(primary) or project_id)
            slug = next((_registry_slug_for_path(root, path_to_slug) for root in roots if _registry_slug_for_path(root, path_to_slug)), None)
            remote = ""
            for root in roots:
                remote = _git_remote(root)
                if remote:
                    break
            logical_id = slug or remote or _slugify(name)
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "source_project_id": str(project_id),
                    "name": name,
                    "source_roots": roots,
                    "primary_root": primary,
                    "project_slug": slug or _slugify(name),
                    "logical_id": logical_id,
                    "git_remote": remote,
                    "position": position,
                    "registry_name": registry_names.get(slug or "", ""),
                }
            )
        return records

    # Legacy Codex state represented project-order directly as paths.
    for position, path in enumerate(order):
        name = str(labels.get(path) or _path_basename(path) or path)
        slug = _registry_slug_for_path(path, path_to_slug)
        remote = _git_remote(path)
        records.append(
            {
                "schema_version": SCHEMA_VERSION,
                "source_project_id": f"legacy-{hashlib.sha256(path.encode('utf-8')).hexdigest()[:16]}",
                "name": name,
                "source_roots": [path],
                "primary_root": path,
                "project_slug": slug or _slugify(name),
                "logical_id": slug or remote or _slugify(name),
                "git_remote": remote,
                "position": position,
                "registry_name": registry_names.get(slug or "", ""),
            }
        )
    return records


def _state_db_path(codex_home: Path) -> Path:
    for candidate in (codex_home / "state_5.sqlite", codex_home / "sqlite" / "state_5.sqlite"):
        if candidate.is_file():
            return candidate
    raise StateSyncError(f"Codex state_5.sqlite not found under {codex_home}")


def _read_threads(db_path: Path) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM threads")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StateSyncError(f"could not read Codex thread index {db_path}: {exc}") from exc


def _thread_timestamp(row: dict[str, Any]) -> int:
    value = row.get("updated_at_ms")
    if isinstance(value, (int, float)) and int(value) > 0:
        return int(value)
    value = row.get("updated_at")
    return int(value or 0) * 1000


def _thread_project(
    thread_id: str,
    cwd: str,
    projects: list[dict[str, Any]],
    ui_state: dict[str, Any],
    registry: dict[str, Any],
) -> tuple[str, str]:
    assignments = ui_state.get("thread-project-assignments") if isinstance(ui_state.get("thread-project-assignments"), dict) else {}
    assignment = assignments.get(thread_id) if isinstance(assignments.get(thread_id), dict) else {}
    source_project_id = str(assignment.get("projectId") or "")
    for project in projects:
        if source_project_id and project.get("source_project_id") == source_project_id:
            return str(project.get("project_slug") or ""), source_project_id
    for project in projects:
        roots = project.get("source_roots") if isinstance(project.get("source_roots"), list) else []
        if any(_path_is_within(cwd, str(root)) for root in roots):
            return str(project.get("project_slug") or ""), str(project.get("source_project_id") or "")
    path_to_slug, _, _ = _registry_maps(registry)
    return str(_registry_slug_for_path(cwd, path_to_slug) or ""), source_project_id


def _thread_id_from_path(path: Path) -> str:
    matches = SESSION_ID_RE.findall(path.name)
    return matches[-1].lower() if matches else ""


def _discover_artifacts(codex_home: Path) -> list[tuple[Path, str, str]]:
    artifacts: list[tuple[Path, str, str]] = []
    for tree, kind in ARTIFACT_TREES.items():
        root = codex_home / tree
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            try:
                if not path.is_file() or path.is_symlink():
                    continue
            except OSError:
                continue
            if kind in {"session", "archived_session"} and path.suffix.lower() != ".jsonl":
                continue
            relative = path.relative_to(codex_home).as_posix()
            artifacts.append((path, relative, kind))
    artifacts.sort(key=lambda item: item[1])
    return artifacts


def _object_path(archive_root: Path, sha256: str) -> Path:
    return archive_root / "objects" / "sha256" / sha256[:2] / f"{sha256}.gz"


def _store_compressed_chunk(
    body: bytes,
    archive_root: Path,
    *,
    compression_level: int,
) -> tuple[dict[str, Any], bool]:
    sha256 = hashlib.sha256(body).hexdigest()
    destination = _object_path(archive_root, sha256)
    if destination.is_file():
        return {
            "sha256": sha256,
            "size": len(body),
            "stored_bytes": destination.stat().st_size,
            "object_path": destination.relative_to(archive_root).as_posix(),
        }, False

    staging = archive_root / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"{os.getpid()}-{time.time_ns()}.gz"
    try:
        with temporary.open("wb") as raw_writer:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_writer,
                compresslevel=compression_level,
                mtime=0,
            ) as writer:
                writer.write(body)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if destination.exists():
            temporary.unlink(missing_ok=True)
        else:
            try:
                os.replace(temporary, destination)
                created = True
            except OSError:
                if destination.exists():
                    temporary.unlink(missing_ok=True)
                else:
                    raise
        return {
            "sha256": sha256,
            "size": len(body),
            "stored_bytes": destination.stat().st_size,
            "object_path": destination.relative_to(archive_root).as_posix(),
        }, created
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_id(chunks: Iterable[dict[str, Any]], size: int) -> str:
    digest = hashlib.sha256(b"agent-bridge-chunked-artifact-v1\0")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    for chunk in chunks:
        sha256 = str(chunk.get("sha256") or "")
        chunk_size = int(chunk.get("size") or 0)
        digest.update(bytes.fromhex(sha256))
        digest.update(chunk_size.to_bytes(8, "big", signed=False))
    return digest.hexdigest()


def _row_objects_exist(archive_root: Path, row: dict[str, Any]) -> bool:
    chunks = row.get("chunks") if isinstance(row.get("chunks"), list) else []
    if not chunks:
        return int(row.get("size") or 0) == 0 and bool(re.fullmatch(r"[0-9a-f]{64}", str(row.get("artifact_id") or "")))
    for chunk in chunks:
        if not isinstance(chunk, dict):
            return False
        sha256 = str(chunk.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256) or not _object_path(archive_root, sha256).is_file():
            return False
    return True


def _file_identity(stat: os.stat_result) -> str:
    return f"{int(stat.st_dev)}:{int(stat.st_ino)}"


def _stable_prefix_chunks(source: Path, stat: os.stat_result, previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not previous or previous.get("file_identity") != _file_identity(stat):
        return []
    previous_size = int(previous.get("size") or 0)
    previous_chunks = previous.get("chunks") if isinstance(previous.get("chunks"), list) else []
    if previous_size <= 0 or stat.st_size <= previous_size or not previous_chunks:
        return []
    stable_count = previous_size // CHUNK_SIZE
    if stable_count <= 0 or stable_count > len(previous_chunks):
        return []
    # Codex rollouts are append-only.  Re-check the last supposedly stable
    # fixed-size block before reusing the prefix so truncation/rewrite cannot be
    # mistaken for an append.
    last = previous_chunks[stable_count - 1]
    if int(last.get("size") or 0) != CHUNK_SIZE:
        return []
    with source.open("rb") as handle:
        handle.seek((stable_count - 1) * CHUNK_SIZE)
        probe = handle.read(CHUNK_SIZE)
    if hashlib.sha256(probe).hexdigest() != str(last.get("sha256") or ""):
        return []
    return [dict(chunk) for chunk in previous_chunks[:stable_count] if isinstance(chunk, dict)]


def _store_chunked_artifact(
    source: Path,
    archive_root: Path,
    *,
    stat_before: os.stat_result,
    previous: dict[str, Any] | None,
    compression_level: int,
) -> tuple[dict[str, Any] | None, int, int, int]:
    chunks = _stable_prefix_chunks(source, stat_before, previous)
    offset = sum(int(chunk.get("size") or 0) for chunk in chunks)
    created_objects = 0
    reused_objects = len(chunks)
    new_stored_bytes = 0
    with source.open("rb") as handle:
        handle.seek(offset)
        while True:
            body = handle.read(CHUNK_SIZE)
            if not body:
                break
            descriptor, created = _store_compressed_chunk(
                body,
                archive_root,
                compression_level=compression_level,
            )
            chunks.append(descriptor)
            created_objects += int(created)
            reused_objects += int(not created)
            if created:
                new_stored_bytes += int(descriptor["stored_bytes"])

    stat_after = source.stat()
    total_size = sum(int(chunk.get("size") or 0) for chunk in chunks)
    if (
        stat_after.st_size != stat_before.st_size
        or stat_after.st_mtime_ns != stat_before.st_mtime_ns
        or total_size != stat_after.st_size
    ):
        return None, created_objects, reused_objects, new_stored_bytes
    return {
        "artifact_id": _artifact_id(chunks, total_size),
        "chunk_size": CHUNK_SIZE,
        "chunks": chunks,
        "size": total_size,
        "stored_bytes": sum(int(chunk.get("stored_bytes") or 0) for chunk in chunks),
        "file_identity": _file_identity(stat_after),
        "mtime_ns": stat_after.st_mtime_ns,
    }, created_objects, reused_objects, new_stored_bytes


def _ui_state_fragment(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "thread-project-assignments",
        "thread-workspace-root-hints",
        "sidebar-project-thread-orders",
        "pinned-thread-ids",
        "projectless-thread-ids",
    )
    return {key: state.get(key, {} if key not in {"pinned-thread-ids", "projectless-thread-ids"} else []) for key in keys}


def _pid_is_running(pid: int) -> bool:
    """Check a lock owner without sending a terminating signal on Windows."""
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    # On Windows, os.kill(pid, 0) calls TerminateProcess with exit code 0. Use
    # the process query API so a lock check cannot kill its own publisher.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means the process exists but cannot be inspected. Keep
        # its lock instead of reclaiming a potentially live publisher.
        return ctypes.get_last_error() == 5
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def _state_sync_lock(machine_id: str, operation: str) -> Iterator[None]:
    state_root = Path(
        os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")
    ).expanduser()
    locks = state_root / "state-sync" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    lock_name = machine_id if operation == "publish" else f"{operation}-{machine_id}"
    lock = locks / f"{lock_name}.lock"
    owner = lock / "owner.json"
    token = f"{os.getpid()}:{time.time_ns()}"
    try:
        lock.mkdir()
    except FileExistsError:
        current = _read_json(owner, {})
        pid = int(current.get("pid") or 0) if isinstance(current, dict) else 0
        try:
            lock_age = max(0.0, time.time() - lock.stat().st_mtime)
        except OSError:
            lock_age = 0.0
        if pid <= 0 and lock_age < 60:
            raise StateSyncError(f"state-sync {operation} lock is initializing for {machine_id}")
        if _pid_is_running(pid):
            raise StateSyncError(f"state-sync {operation} is already running for {machine_id} (pid {pid})")
        history = state_root / "state-sync" / "lock-history"
        history.mkdir(parents=True, exist_ok=True)
        preserved = history / (
            f"{_utc_stamp()}-{time.time_ns() % 1_000_000_000:09d}-{operation}-{machine_id}.lock"
        )
        try:
            os.replace(lock, preserved)
            lock.mkdir()
        except OSError as exc:
            raise StateSyncError(f"could not reclaim stale {operation} lock {lock}: {exc}") from exc
    _atomic_write_json(
        owner,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": f"state_sync_{operation}_lock",
            "operation": operation,
            "machine_id": machine_id,
            "pid": os.getpid(),
            "created_at": iso_now(),
            "token": token,
        },
    )
    try:
        yield
    finally:
        current = _read_json(owner, {})
        if isinstance(current, dict) and current.get("token") == token:
            owner.unlink(missing_ok=True)
            try:
                lock.rmdir()
            except OSError:
                pass


@contextmanager
def _publisher_lock(machine_id: str) -> Iterator[None]:
    with _state_sync_lock(machine_id, "publish"):
        yield


@contextmanager
def _apply_lock(machine_id: str) -> Iterator[None]:
    with _state_sync_lock(machine_id, "apply"):
        yield


def publish_codex_state(
    *,
    codex_home: str | Path | None = None,
    shared_root: str | Path | None = None,
    project_registry: str | Path | None = None,
    machine_id: str | None = None,
    settle_seconds: int = 60,
    compression_level: int = 1,
    metadata_only: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    source_machine = _validate_machine_id(machine_id or stable_machine_id())
    with _publisher_lock(source_machine):
        return _publish_codex_state_unlocked(
            codex_home=codex_home,
            shared_root=shared_root,
            project_registry=project_registry,
            machine_id=source_machine,
            settle_seconds=settle_seconds,
            compression_level=compression_level,
            metadata_only=metadata_only,
            progress=progress,
        )


def _publish_codex_state_unlocked(
    *,
    codex_home: str | Path | None = None,
    shared_root: str | Path | None = None,
    project_registry: str | Path | None = None,
    machine_id: str,
    settle_seconds: int = 60,
    compression_level: int = 1,
    metadata_only: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    home = _resolve_codex_home(codex_home)
    if not home.is_dir():
        raise StateSyncError(f"Codex home does not exist: {home}")
    global_state = _read_native_json_object(home / ".codex-global-state.json")
    archive = _resolve_archive_root(shared_root, create=True)
    source_machine = _validate_machine_id(machine_id)
    machine_root = archive / "machines" / source_machine / "codex"
    machine_root.mkdir(parents=True, exist_ok=True)
    registry = _load_project_registry(project_registry)
    projects = _project_records(global_state, registry)
    ui_state = _ui_state_fragment(global_state)
    threads = _read_threads(_state_db_path(home))
    previous_rows = _read_jsonl(machine_root / "artifact-index.jsonl")
    previous_thread_rows = _read_jsonl(machine_root / "thread-index.jsonl")
    previous_threads_by_id = {
        str(row.get("thread_id") or ""): row for row in previous_thread_rows if row.get("thread_id")
    }
    previous_by_path = {str(row.get("relative_path") or ""): row for row in previous_rows if row.get("relative_path")}
    artifact_rows: list[dict[str, Any]] = []
    uploaded_objects = 0
    reused_objects = 0
    deferred_files = 0
    new_stored_bytes = 0
    warnings: list[str] = []
    discovered = _discover_artifacts(home)
    discovered_paths = {relative for _, relative, _ in discovered}
    now_ns = time.time_ns()

    if not metadata_only:
        for index, (path, relative, kind) in enumerate(discovered, start=1):
            try:
                stat = path.stat()
            except OSError as exc:
                warnings.append(f"unreadable artifact {relative}: {exc}")
                continue
            previous = previous_by_path.get(relative)
            unchanged = bool(
                previous
                and previous.get("size") == stat.st_size
                and previous.get("mtime_ns") == stat.st_mtime_ns
                and _row_objects_exist(archive, previous)
            )
            if unchanged:
                row = dict(previous)
                row["source_present"] = True
                row.pop("deferred_update", None)
                artifact_rows.append(row)
                reused_objects += len(row.get("chunks") or [])
                continue

            age_seconds = max(0.0, (now_ns - stat.st_mtime_ns) / 1_000_000_000)
            if age_seconds < max(0, settle_seconds):
                if previous and _row_objects_exist(archive, previous):
                    row = dict(previous)
                    row["source_present"] = True
                    row["deferred_update"] = True
                    artifact_rows.append(row)
                else:
                    warnings.append(f"deferred active artifact {relative}")
                deferred_files += 1
                continue

            stored, created, reused, created_bytes = _store_chunked_artifact(
                path,
                archive,
                stat_before=stat,
                previous=(
                    previous
                    if kind in {"session", "archived_session"}
                    and previous
                    and _row_objects_exist(archive, previous)
                    else None
                ),
                compression_level=max(1, min(9, compression_level)),
            )
            uploaded_objects += created
            reused_objects += reused
            new_stored_bytes += created_bytes
            if stored is None:
                if previous and _row_objects_exist(archive, previous):
                    row = dict(previous)
                    row["source_present"] = True
                    row["deferred_update"] = True
                    artifact_rows.append(row)
                else:
                    warnings.append(f"artifact changed while publishing and was deferred: {relative}")
                deferred_files += 1
                continue
            thread_id = _thread_id_from_path(path) if kind in {"session", "archived_session"} else ""
            row = {
                "schema_version": SCHEMA_VERSION,
                "kind": kind,
                "relative_path": relative,
                "source_machine": source_machine,
                "source_present": True,
                **stored,
            }
            if thread_id:
                row["thread_id"] = thread_id
            artifact_rows.append(row)
            if progress and (created > 0 or index % 100 == 0):
                progress(
                    f"state-sync publish: {index}/{len(discovered)} artifacts, "
                    f"new chunks={uploaded_objects}, deferred={deferred_files}"
                )

        # Keep prior catalog entries as non-active retention records when a
        # native file disappears or moves.  They remain recoverable but are not
        # re-imported automatically.
        for relative, previous in previous_by_path.items():
            if relative in discovered_paths or not _row_objects_exist(archive, previous):
                continue
            row = dict(previous)
            row["source_present"] = False
            row.pop("deferred_update", None)
            artifact_rows.append(row)
    else:
        artifact_rows = previous_rows

    artifacts_by_thread: dict[str, list[dict[str, Any]]] = {}
    for artifact in artifact_rows:
        thread_id = str(artifact.get("thread_id") or "")
        if thread_id and artifact.get("source_present", True) and not artifact.get("deferred_update"):
            artifacts_by_thread.setdefault(thread_id, []).append(artifact)

    thread_rows: list[dict[str, Any]] = []
    for thread in threads:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            continue
        rollout_path = str(thread.get("rollout_path") or "")
        artifact_candidates = artifacts_by_thread.get(thread_id, [])
        artifact = next(
            (
                row
                for row in artifact_candidates
                if _path_key(str(home / str(row.get("relative_path") or ""))) == _path_key(rollout_path)
            ),
            artifact_candidates[0] if artifact_candidates else None,
        )
        if artifact is None:
            previous_thread = previous_threads_by_id.get(thread_id)
            if previous_thread and any(
                str(row.get("thread_id") or "") == thread_id and row.get("deferred_update")
                for row in artifact_rows
            ):
                thread_rows.append(dict(previous_thread))
            continue
        project_slug, source_project_id = _thread_project(
            thread_id,
            str(thread.get("cwd") or ""),
            projects,
            ui_state,
            registry,
        )
        thread_rows.append(
            {
                "schema_version": SCHEMA_VERSION,
                "thread_id": thread_id,
                "thread": thread,
                "project_slug": project_slug,
                "source_project_id": source_project_id,
                "artifact_relative_path": str(artifact.get("relative_path") or ""),
                "artifact_id": str(artifact.get("artifact_id") or ""),
                "source_machine": source_machine,
            }
        )

    # Include unindexed session files in the artifact index but flag them in the
    # manifest so operators can distinguish archive coverage from sidebar rows.
    indexed_session_ids = {str(row.get("thread_id") or "") for row in thread_rows}
    artifact_session_ids = {
        str(row.get("thread_id") or "")
        for row in artifact_rows
        if row.get("source_present", True)
        and row.get("kind") in {"session", "archived_session"}
        and row.get("thread_id")
    }
    unindexed_sessions = sorted(artifact_session_ids - indexed_session_ids)
    if unindexed_sessions:
        warnings.append(f"{len(unindexed_sessions)} session artifacts are not present in state_5.sqlite")

    artifact_rows.sort(key=lambda row: str(row.get("relative_path") or ""))
    projects.sort(key=lambda row: (int(row.get("position") or 0), str(row.get("logical_id") or "")))
    thread_rows.sort(key=lambda row: str(row.get("thread_id") or ""))

    active_rows = [row for row in artifact_rows if row.get("source_present", True)]
    raw_bytes = sum(int(row.get("size") or 0) for row in active_rows)
    unique_chunks: dict[str, dict[str, Any]] = {}
    active_chunks: dict[str, dict[str, Any]] = {}
    for row in artifact_rows:
        for chunk in row.get("chunks") if isinstance(row.get("chunks"), list) else []:
            if not isinstance(chunk, dict) or not chunk.get("sha256"):
                continue
            unique_chunks.setdefault(str(chunk["sha256"]), chunk)
            if row.get("source_present", True):
                active_chunks.setdefault(str(chunk["sha256"]), chunk)
    stored_bytes = sum(int(chunk.get("stored_bytes") or 0) for chunk in unique_chunks.values())
    active_stored_bytes = sum(int(chunk.get("stored_bytes") or 0) for chunk in active_chunks.values())

    metadata_bodies = {
        "artifact-index.jsonl": _jsonl_bytes(artifact_rows),
        "project-index.jsonl": _jsonl_bytes(projects),
        "thread-index.jsonl": _jsonl_bytes(thread_rows),
        "ui-state.json": _json_bytes(ui_state),
    }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "codex_incremental_state_manifest",
        "machine_id": source_machine,
        "hostname": socket.gethostname(),
        "platform": _platform_name(),
        "generated_at": iso_now(),
        "artifact_count": len(artifact_rows),
        "active_artifact_count": len(active_rows),
        "retained_artifact_count": len(artifact_rows) - len(active_rows),
        "session_artifact_count": sum(
            1 for row in active_rows if row.get("kind") in {"session", "archived_session"}
        ),
        "thread_count": len(thread_rows),
        "project_count": len(projects),
        "raw_bytes": raw_bytes,
        "stored_bytes": stored_bytes,
        "active_stored_bytes": active_stored_bytes,
        "new_stored_bytes": new_stored_bytes,
        "chunk_size": CHUNK_SIZE,
        "referenced_object_count": len(unique_chunks),
        "new_objects": uploaded_objects,
        "reused_objects": reused_objects,
        "deferred_files": deferred_files,
        "metadata_only": metadata_only,
        "unindexed_session_count": len(unindexed_sessions),
        "warnings": warnings,
        "retention": "append_only_no_automatic_deletion",
        "metadata_sha256": {
            name: hashlib.sha256(body).hexdigest() for name, body in metadata_bodies.items()
        },
    }
    for name, body in metadata_bodies.items():
        _atomic_write_bytes(machine_root / name, body)
    _atomic_write_json(machine_root / "manifest.json", manifest)
    _append_jsonl(
        machine_root / "events.jsonl",
        {
            "schema_version": SCHEMA_VERSION,
            "event": "publish",
            "generated_at": manifest["generated_at"],
            "machine_id": source_machine,
            "new_objects": uploaded_objects,
            "new_stored_bytes": new_stored_bytes,
            "artifact_count": len(artifact_rows),
            "raw_bytes": raw_bytes,
            "stored_bytes": stored_bytes,
        },
    )
    return {
        "ok": True,
        "archive_root": str(archive),
        "machine_root": str(machine_root),
        **manifest,
    }


def _parse_path_maps(values: Iterable[str] | None) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values or []:
        if "=" not in value:
            raise StateSyncError(f"invalid --path-map {value!r}; expected SOURCE=TARGET")
        source, target = value.split("=", 1)
        if not source or not target:
            raise StateSyncError(f"invalid --path-map {value!r}; expected SOURCE=TARGET")
        result.append((source, target))
    result.sort(key=lambda item: len(_path_key(item[0])), reverse=True)
    return result


def _apply_explicit_path_map(value: str, mappings: list[tuple[str, str]]) -> str | None:
    normalized = _path_key(value)
    for source, target in mappings:
        source_key = _path_key(source)
        if normalized == source_key:
            return target
        if normalized.startswith(source_key + "/"):
            suffix = normalized[len(source_key) :].lstrip("/")
            return _join_target_path(target, suffix)
    return None


def _join_target_path(root: str, suffix: str) -> str:
    if not suffix:
        return root
    windows_style = bool(re.match(r"^[A-Za-z]:[\\/]", root)) or ("\\" in root and "/" not in root)
    separator = "\\" if windows_style else "/"
    clean_suffix = suffix.replace("\\", "/").strip("/").replace("/", separator)
    return root.rstrip("\\/") + separator + clean_suffix


def _preferred_registry_path(
    slug: str,
    paths_by_slug: dict[str, dict[str, list[str]]],
    *,
    platform_name: str,
) -> str | None:
    by_os = paths_by_slug.get(slug, {})
    candidates = list(by_os.get(platform_name, [])) + list(by_os.get("unknown", []))
    if not candidates:
        return None
    existing = [candidate for candidate in candidates if Path(candidate).expanduser().exists()]
    return existing[0] if existing else candidates[0]


def _fallback_target_path(source: str, project: dict[str, Any], *, platform_name: str) -> str:
    basename = _path_basename(source) or _slugify(str(project.get("name") or "project"))
    # Git-backed project roots belong under the local Code directory.  Registry
    # mappings and explicit --path-map rules take precedence over this fallback.
    return str(Path.home() / "Code" / basename)


def _target_projects(state: dict[str, Any], registry: dict[str, Any]) -> list[dict[str, Any]]:
    return _project_records(state, registry)


def _map_projects_for_target(
    source_projects: list[dict[str, Any]],
    target_state: dict[str, Any],
    registry: dict[str, Any],
    *,
    platform_name: str,
    path_maps: list[tuple[str, str]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    _, paths_by_slug, _ = _registry_maps(registry)
    targets = _target_projects(target_state, registry)
    target_by_logical = {str(row.get("logical_id") or "").casefold(): row for row in targets if row.get("logical_id")}
    target_by_name = {str(row.get("name") or "").casefold(): row for row in targets if row.get("name")}
    mapping: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    target_local_projects = target_state.get("local-projects")
    used_ids = set(str(project_id) for project_id in target_local_projects) if isinstance(target_local_projects, dict) else set()

    for source in sorted(source_projects, key=lambda row: int(row.get("position") or 0)):
        source_id = str(source.get("source_project_id") or "")
        logical = str(source.get("logical_id") or source.get("project_slug") or source.get("name") or source_id)
        name = str(source.get("name") or source.get("project_slug") or "project")
        existing = target_by_logical.get(logical.casefold()) or target_by_name.get(name.casefold())
        if existing:
            roots = existing.get("source_roots") if isinstance(existing.get("source_roots"), list) else []
            target_path = str(roots[0]) if roots else str(existing.get("primary_root") or "")
            mapping[source_id] = {
                "project_id": str(existing.get("source_project_id") or ""),
                "path": target_path,
                "name": str(existing.get("name") or name),
                "logical_id": logical,
                "project_slug": str(source.get("project_slug") or ""),
                "source_roots": [str(root) for root in source.get("source_roots", []) if isinstance(root, str)],
            }
            continue

        roots = source.get("source_roots") if isinstance(source.get("source_roots"), list) else []
        primary = str(source.get("primary_root") or (roots[0] if roots else ""))
        target_path = _apply_explicit_path_map(primary, path_maps)
        if not target_path:
            target_path = _preferred_registry_path(
                str(source.get("project_slug") or ""),
                paths_by_slug,
                platform_name=platform_name,
            )
        if not target_path:
            target_path = _fallback_target_path(primary, source, platform_name=platform_name)
            warnings.append(
                f"project {name!r} has no {platform_name} registry mapping; using expected path {target_path}"
            )
        digest = hashlib.sha256(f"{logical}|{_path_key(target_path, platform_name=platform_name)}".encode("utf-8")).hexdigest()[:32]
        target_id = f"local-{digest}"
        if target_id in used_ids:
            target_id = f"local-{hashlib.sha256((logical + target_path + source_id).encode('utf-8')).hexdigest()[:32]}"
        used_ids.add(target_id)
        mapping[source_id] = {
            "project_id": target_id,
            "path": target_path,
            "name": name,
            "logical_id": logical,
            "project_slug": str(source.get("project_slug") or ""),
            "source_roots": [str(root) for root in roots],
        }
    return mapping, warnings


def _map_source_path(
    value: str,
    *,
    source_project_id: str,
    project_slug: str,
    project_mapping: dict[str, dict[str, Any]],
    path_maps: list[tuple[str, str]],
) -> str:
    explicit = _apply_explicit_path_map(value, path_maps)
    if explicit:
        return explicit
    project = project_mapping.get(source_project_id)
    if project is None and project_slug:
        project = next(
            (
                row
                for row in project_mapping.values()
                if str(row.get("project_slug") or "").casefold() == project_slug.casefold()
            ),
            None,
        )
    if project and project.get("path"):
        target_root = str(project["path"])
        normalized_value = str(value or "").replace("\\", "/").rstrip("/")
        roots = project.get("source_roots") if isinstance(project.get("source_roots"), list) else []
        roots = sorted((str(root) for root in roots if root), key=lambda root: len(_path_key(root)), reverse=True)
        for root in roots:
            normalized_root = root.replace("\\", "/").rstrip("/")
            if _path_key(normalized_value) == _path_key(normalized_root):
                return target_root
            if _path_key(normalized_value).startswith(_path_key(normalized_root) + "/"):
                return _join_target_path(target_root, normalized_value[len(normalized_root) :].lstrip("/"))
        return target_root
    return value


def _load_source_machine(archive: Path, source_machine: str) -> dict[str, Any]:
    source_machine = _validate_machine_id(source_machine)
    root = archive / "machines" / source_machine / "codex"
    last_error = "metadata is incomplete"
    for _ in range(3):
        manifest = _read_json(root / "manifest.json", {})
        if not isinstance(manifest, dict) or manifest.get("kind") != "codex_incremental_state_manifest":
            raise StateSyncError(f"no valid Codex state-sync manifest for machine {source_machine!r}")
        if str(manifest.get("machine_id") or "") != source_machine:
            raise StateSyncError(f"manifest machine mismatch for {source_machine!r}")
        if str(manifest.get("schema_version") or "").split(".", 1)[0] != SCHEMA_VERSION.split(".", 1)[0]:
            raise StateSyncError(f"unsupported state-sync schema for machine {source_machine!r}")
        hashes = manifest.get("metadata_sha256") if isinstance(manifest.get("metadata_sha256"), dict) else {}
        bodies: dict[str, bytes] = {}
        try:
            for name in ("artifact-index.jsonl", "project-index.jsonl", "thread-index.jsonl", "ui-state.json"):
                bodies[name] = (root / name).read_bytes()
        except OSError as exc:
            last_error = str(exc)
            time.sleep(0.05)
            continue
        mismatch = [
            name
            for name, expected in hashes.items()
            if name in bodies and hashlib.sha256(bodies[name]).hexdigest() != str(expected)
        ]
        if mismatch:
            last_error = f"metadata changed during read: {', '.join(sorted(mismatch))}"
            time.sleep(0.05)
            continue
        try:
            def parse_jsonl(name: str) -> list[dict[str, Any]]:
                rows = []
                for line in bodies[name].decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                return rows

            ui_state = json.loads(bodies["ui-state.json"].decode("utf-8"))
            if not isinstance(ui_state, dict):
                raise ValueError("ui-state.json is not an object")
            return {
                "root": root,
                "manifest": manifest,
                "artifacts": parse_jsonl("artifact-index.jsonl"),
                "projects": parse_jsonl("project-index.jsonl"),
                "threads": parse_jsonl("thread-index.jsonl"),
                "ui_state": ui_state,
            }
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            time.sleep(0.05)
    raise StateSyncError(f"could not read consistent metadata for {source_machine!r}: {last_error}")


def list_state_sync_sources(
    *,
    shared_root: str | Path | None = None,
    current_machine: str | None = None,
) -> list[dict[str, Any]]:
    archive = _resolve_archive_root(shared_root, create=False)
    machines_root = archive / "machines"
    if not machines_root.is_dir():
        return []
    current = _validate_machine_id(current_machine or stable_machine_id())
    rows: list[dict[str, Any]] = []
    for machine_dir in sorted(path for path in machines_root.iterdir() if path.is_dir()):
        manifest = _read_json(machine_dir / "codex" / "manifest.json", {})
        if not isinstance(manifest, dict) or not manifest:
            continue
        rows.append(
            {
                "machine_id": machine_dir.name,
                "current_machine": machine_dir.name == current,
                "generated_at": manifest.get("generated_at"),
                "platform": manifest.get("platform"),
                "thread_count": manifest.get("thread_count", 0),
                "project_count": manifest.get("project_count", 0),
                "session_artifact_count": manifest.get("session_artifact_count", 0),
                "active_artifact_count": manifest.get("active_artifact_count", manifest.get("artifact_count", 0)),
                "raw_bytes": manifest.get("raw_bytes", 0),
                "stored_bytes": manifest.get("stored_bytes", 0),
                "new_objects": manifest.get("new_objects", 0),
                "new_stored_bytes": manifest.get("new_stored_bytes", 0),
                "deferred_files": manifest.get("deferred_files", 0),
            }
        )
    return rows


def _codex_desktop_running(platform_name: str | None = None) -> bool:
    platform_value = platform_name or _platform_name()
    try:
        if platform_value == "macos":
            result = subprocess.run(
                ["osascript", "-e", 'application id "com.openai.codex" is running'],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            answer = result.stdout.strip().casefold()
            if result.returncode == 0 and answer in {"true", "false"}:
                return answer == "true"
            result = subprocess.run(
                ["ps", "-axo", "command="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode != 0:
                return True
            return any(
                line.strip().split(" ", 1)[0]
                in {
                    "/Applications/Codex.app/Contents/MacOS/Codex",
                    "/Applications/Codex.app/Contents/MacOS/ChatGPT",
                }
                for line in result.stdout.splitlines()
                if line.strip()
            )
        if platform_value == "windows":
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return True
            process_names = {
                row[0].strip().casefold()
                for row in csv.reader(result.stdout.splitlines())
                if row and row[0].strip()
            }
            return bool({"codex.exe", "chatgpt.exe"} & process_names)
    except (OSError, subprocess.SubprocessError):
        # A failed safety probe is not evidence that native state is idle.
        return True
    return False


def _backup_native_metadata(codex_home: Path, db_path: Path) -> Path:
    backup = codex_home / "backups" / f"state-sync-{_utc_stamp()}-{time.time_ns() % 1_000_000_000:09d}"
    backup.mkdir(parents=True, exist_ok=False)
    destination_db = backup / db_path.relative_to(codex_home)
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=3)
    destination_connection = sqlite3.connect(destination_db)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    for relative in (".codex-global-state.json", "session_index.jsonl"):
        source = codex_home / relative
        if source.is_file():
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    _atomic_write_json(
        backup / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "codex_state_sync_backup",
            "created_at": iso_now(),
            "source_codex_home": str(codex_home),
        },
    )
    return backup


def _artifact_descriptor_file(path: Path) -> tuple[str, int]:
    chunks: list[dict[str, Any]] = []
    size = 0
    with path.open("rb") as handle:
        while True:
            body = handle.read(CHUNK_SIZE)
            if not body:
                break
            chunks.append({"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)})
            size += len(body)
    return _artifact_id(chunks, size), size


def _reconstruct_artifact(archive: Path, row: dict[str, Any], destination: Path) -> None:
    chunks = row.get("chunks") if isinstance(row.get("chunks"), list) else []
    expected_id = str(row.get("artifact_id") or "")
    expected_size = int(row.get("size") or 0)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_id) or (not chunks and expected_size != 0):
        raise StateSyncError(f"invalid chunk manifest for {row.get('relative_path')}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    verified_chunks: list[dict[str, Any]] = []
    total_size = 0
    try:
        with temporary.open("wb") as writer:
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    raise StateSyncError("invalid non-object chunk descriptor")
                sha256 = str(chunk.get("sha256") or "")
                size = int(chunk.get("size") or 0)
                if not re.fullmatch(r"[0-9a-f]{64}", sha256) or size <= 0 or size > CHUNK_SIZE:
                    raise StateSyncError(f"invalid chunk descriptor for {row.get('relative_path')}")
                object_path = _object_path(archive, sha256)
                if not object_path.is_file():
                    raise StateSyncError(f"missing chunk {sha256} for {row.get('relative_path')}")
                with gzip.open(object_path, "rb") as reader:
                    body = reader.read(size + 1)
                if len(body) != size or hashlib.sha256(body).hexdigest() != sha256:
                    raise StateSyncError(f"corrupt chunk {sha256} for {row.get('relative_path')}")
                writer.write(body)
                verified_chunks.append({"sha256": sha256, "size": size})
                total_size += size
        if total_size != expected_size or _artifact_id(verified_chunks, total_size) != expected_id:
            raise StateSyncError(f"artifact manifest mismatch for {row.get('relative_path')}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _is_prefix(shorter: Path, longer: Path) -> bool:
    if shorter.stat().st_size > longer.stat().st_size:
        return False
    with shorter.open("rb") as left, longer.open("rb") as right:
        while True:
            chunk = left.read(1024 * 1024)
            if not chunk:
                return True
            if right.read(len(chunk)) != chunk:
                return False


def _safe_join_under(root: Path, parts: list[str]) -> Path | None:
    """Resolve an untrusted relative path and prove it remains below root."""
    try:
        resolved_root = root.resolve(strict=False)
        destination = root.joinpath(*parts).resolve(strict=False)
        destination.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return destination


def _install_artifact(
    *,
    archive: Path,
    codex_home: Path,
    source_machine: str,
    row: dict[str, Any],
    backup_root: Path,
) -> tuple[str, str]:
    relative = str(row.get("relative_path") or "")
    normalized_relative = relative.replace("\\", "/")
    relative_parts = normalized_relative.split("/")
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", relative)
        or any(part in {"", ".", ".."} or ":" in part or "\x00" in part for part in relative_parts)
    ):
        return "invalid", ""
    if row.get("source_present", True) is False:
        return "retained", ""
    expected_id = str(row.get("artifact_id") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_id) or not _row_objects_exist(archive, row):
        return "missing_object", ""
    destination = _safe_join_under(codex_home, relative_parts)
    if destination is None:
        return "invalid", ""
    if not destination.exists():
        _reconstruct_artifact(archive, row, destination)
        return "created", str(destination)
    local_id, _ = _artifact_descriptor_file(destination)
    if local_id == expected_id:
        return "unchanged", str(destination)

    staging_root = codex_home / "session-sync-staging" / source_machine / expected_id[:12]
    staging = _safe_join_under(staging_root, relative_parts)
    if staging is None:
        return "invalid", ""
    _reconstruct_artifact(archive, row, staging)
    kind = str(row.get("kind") or "")
    if kind in {"session", "archived_session"}:
        if _is_prefix(destination, staging):
            preserved = _safe_join_under(backup_root / "replaced-artifacts", relative_parts)
            if preserved is None:
                staging.unlink(missing_ok=True)
                return "invalid", ""
            preserved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, preserved)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, destination)
            return "extended", str(destination)
        if _is_prefix(staging, destination):
            staging.unlink(missing_ok=True)
            return "local_newer", str(destination)

    conflict_root = codex_home / "session-sync-conflicts" / source_machine / expected_id[:12]
    conflict = _safe_join_under(conflict_root, relative_parts)
    if conflict is None:
        staging.unlink(missing_ok=True)
        return "invalid", ""
    conflict.parent.mkdir(parents=True, exist_ok=True)
    if conflict.exists() and _artifact_descriptor_file(conflict)[0] == expected_id:
        staging.unlink(missing_ok=True)
    else:
        os.replace(staging, conflict)
    return "conflict", str(destination)


def _table_columns(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {
        str(row[1]): {"type": row[2], "notnull": bool(row[3]), "default": row[4], "pk": bool(row[5])}
        for row in rows
    }


def _merge_thread_rows(
    db_path: Path,
    remote_threads: list[dict[str, Any]],
    *,
    project_mapping_by_machine: dict[str, dict[str, dict[str, Any]]],
    path_maps: list[tuple[str, str]],
) -> dict[str, Any]:
    connection = sqlite3.connect(db_path, timeout=5)
    connection.row_factory = sqlite3.Row
    inserted = 0
    updated = 0
    preserved = 0
    skipped = 0
    merged_thread_ids: list[str] = []
    ui_thread_ids: list[str] = []
    try:
        columns = _table_columns(connection, "threads")
        if not columns:
            raise StateSyncError(f"threads table missing from {db_path}")
        for envelope in remote_threads:
            thread = envelope.get("thread") if isinstance(envelope.get("thread"), dict) else {}
            thread_id = str(envelope.get("thread_id") or thread.get("id") or "")
            source_machine = str(envelope.get("source_machine") or "")
            if not thread_id:
                skipped += 1
                continue
            artifact_path = str(envelope.get("_artifact_path") or "")
            existing_row = connection.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not artifact_path:
                skipped += 1
                continue
            values = {key: value for key, value in thread.items() if key in columns}
            values["id"] = thread_id
            if artifact_path:
                values["rollout_path"] = artifact_path
            source_project_id = str(envelope.get("source_project_id") or "")
            project_mapping = project_mapping_by_machine.get(source_machine, {})
            values["cwd"] = _map_source_path(
                str(values.get("cwd") or ""),
                source_project_id=source_project_id,
                project_slug=str(envelope.get("project_slug") or ""),
                project_mapping=project_mapping,
                path_maps=path_maps,
            )
            if existing_row is None:
                required_missing = [
                    name
                    for name, info in columns.items()
                    if info["notnull"] and info["default"] is None and not info["pk"] and name not in values
                ]
                if required_missing:
                    skipped += 1
                    continue
                names = list(values)
                placeholders = ", ".join("?" for _ in names)
                quoted = ", ".join(f'"{name}"' for name in names)
                connection.execute(
                    f"INSERT INTO threads ({quoted}) VALUES ({placeholders})",
                    [values[name] for name in names],
                )
                inserted += 1
                merged_thread_ids.append(thread_id)
                ui_thread_ids.append(thread_id)
                continue

            existing = dict(existing_row)
            remote_timestamp = _thread_timestamp(thread)
            local_timestamp = _thread_timestamp(existing)
            if remote_timestamp <= local_timestamp:
                # Still preserve a newly imported rollout path when the local row
                # points to a missing file, but never downgrade fresher metadata.
                if artifact_path and not Path(str(existing.get("rollout_path") or "")).exists():
                    connection.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (artifact_path, thread_id))
                    updated += 1
                else:
                    preserved += 1
                merged_thread_ids.append(thread_id)
                if remote_timestamp == local_timestamp:
                    ui_thread_ids.append(thread_id)
                continue
            update_values = {
                key: value
                for key, value in values.items()
                if key not in {"id", "created_at", "created_at_ms"}
            }
            if "is_pinned" in columns:
                update_values["is_pinned"] = max(int(existing.get("is_pinned") or 0), int(values.get("is_pinned") or 0))
            assignments = ", ".join(f'"{name}" = ?' for name in update_values)
            connection.execute(
                f"UPDATE threads SET {assignments} WHERE id = ?",
                [*update_values.values(), thread_id],
            )
            updated += 1
            merged_thread_ids.append(thread_id)
            ui_thread_ids.append(thread_id)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "inserted": inserted,
        "updated": updated,
        "preserved": preserved,
        "skipped": skipped,
        "merged_thread_ids": sorted(set(merged_thread_ids)),
        "ui_thread_ids": sorted(set(ui_thread_ids)),
    }


def _merge_session_index(codex_home: Path, remote_threads: list[dict[str, Any]]) -> int:
    path = codex_home / "session_index.jsonl"
    try:
        raw_lines = path.read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        raw_lines = []
    except OSError as exc:
        raise StateSyncError(f"could not read native Codex session index {path}: {exc}") from exc

    # session_index.jsonl is native state.  Keep every original physical line
    # unless this merge genuinely updates its matching object.  In particular,
    # a tolerant reader followed by a normalized rewrite would silently erase
    # malformed JSON, scalar JSON values, duplicates, and future fields.
    parsed_rows: list[tuple[int, dict[str, Any]]] = []
    for position, raw_line in enumerate(raw_lines):
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("id"):
            parsed_rows.append((position, value))

    by_id: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for position, row in parsed_rows:
        by_id.setdefault(str(row["id"]), []).append((position, row))

    replacements: dict[int, bytes] = {}
    appended: list[dict[str, Any]] = []
    added = 0
    seen_remote_ids: set[str] = set()
    for envelope in remote_threads:
        thread = envelope.get("thread") if isinstance(envelope.get("thread"), dict) else {}
        thread_id = str(envelope.get("thread_id") or thread.get("id") or "")
        if not thread_id or thread_id in seen_remote_ids:
            continue
        seen_remote_ids.add(thread_id)
        updated_ms = _thread_timestamp(thread)
        updated_at = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z") if updated_ms else ""
        row = {
            "id": thread_id,
            "thread_name": str(thread.get("title") or thread.get("name") or thread.get("preview") or "Imported session"),
            "updated_at": updated_at,
        }
        matches = by_id.get(thread_id, [])
        if not matches:
            appended.append(row)
            added += 1
            continue

        # Preserve duplicate rows.  When a remote index is newer, update only
        # the freshest matching object and retain all of its unknown fields.
        position, current = max(matches, key=lambda item: (_session_index_timestamp(item[1]), item[0]))
        if updated_ms > _session_index_timestamp(current):
            merged = dict(current)
            merged.update(row)
            newline = b"\r\n" if raw_lines[position].endswith(b"\r\n") else b"\n"
            replacements[position] = json.dumps(merged, sort_keys=True).encode("utf-8") + newline

    if replacements or appended:
        body = b"".join(replacements.get(position, raw_line) for position, raw_line in enumerate(raw_lines))
        if appended:
            if body and not body.endswith((b"\n", b"\r")):
                body += b"\n"
            body += b"".join(json.dumps(row, sort_keys=True).encode("utf-8") + b"\n" for row in appended)
        _atomic_write_bytes(path, body)
    return added


def _session_index_timestamp(row: dict[str, Any]) -> int:
    """Return a comparable timestamp for native session-index objects."""
    raw = row.get("updated_at_ms")
    if isinstance(raw, (int, float)):
        return int(raw)
    raw = row.get("updated_at")
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value if value > 10_000_000_000 else value * 1000
    if not isinstance(raw, str) or not raw.strip():
        return 0
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _merge_ui_state(
    target_state: dict[str, Any],
    sources: list[dict[str, Any]],
    project_mapping_by_machine: dict[str, dict[str, dict[str, Any]]],
    imported_thread_ids: set[str],
    thread_source_machines: dict[str, str],
) -> dict[str, Any]:
    state = dict(target_state)
    local_projects = dict(state.get("local-projects") or {}) if isinstance(state.get("local-projects"), dict) else {}
    project_order = list(state.get("project-order") or []) if isinstance(state.get("project-order"), list) else []
    saved_roots = list(state.get("electron-saved-workspace-roots") or []) if isinstance(state.get("electron-saved-workspace-roots"), list) else []
    labels = dict(state.get("electron-workspace-root-labels") or {}) if isinstance(state.get("electron-workspace-root-labels"), dict) else {}
    assignments = dict(state.get("thread-project-assignments") or {}) if isinstance(state.get("thread-project-assignments"), dict) else {}
    hints = dict(state.get("thread-workspace-root-hints") or {}) if isinstance(state.get("thread-workspace-root-hints"), dict) else {}
    sidebar_orders = dict(state.get("sidebar-project-thread-orders") or {}) if isinstance(state.get("sidebar-project-thread-orders"), dict) else {}
    pinned = list(state.get("pinned-thread-ids") or []) if isinstance(state.get("pinned-thread-ids"), list) else []
    projectless = list(state.get("projectless-thread-ids") or []) if isinstance(state.get("projectless-thread-ids"), list) else []
    now_ms = _now_ms()

    for source in sources:
        machine = str(source["manifest"].get("machine_id") or "")
        mapping = project_mapping_by_machine.get(machine, {})
        for project in sorted(source["projects"], key=lambda row: int(row.get("position") or 0)):
            source_id = str(project.get("source_project_id") or "")
            target = mapping.get(source_id)
            if not target:
                continue
            target_id = target["project_id"]
            target_path = target["path"]
            target_name = target["name"]
            current = local_projects.get(target_id)
            if not isinstance(current, dict):
                current = {
                    "id": target_id,
                    "name": target_name,
                    "rootPaths": [target_path] if target_path else [],
                    "createdAt": now_ms,
                    "updatedAt": now_ms,
                }
            else:
                current = dict(current)
                roots = list(current.get("rootPaths") or [])
                if target_path and target_path not in roots:
                    roots.append(target_path)
                current["rootPaths"] = roots
                current["updatedAt"] = max(int(current.get("updatedAt") or 0), now_ms)
            local_projects[target_id] = current
            if target_id not in project_order:
                project_order.append(target_id)
            if target_path and target_path not in saved_roots:
                saved_roots.append(target_path)
            if target_path:
                labels.setdefault(target_path, target_name)

        source_ui = source.get("ui_state") if isinstance(source.get("ui_state"), dict) else {}
        source_assignments = source_ui.get("thread-project-assignments") if isinstance(source_ui.get("thread-project-assignments"), dict) else {}
        for thread_id, assignment in source_assignments.items():
            if (
                thread_id not in imported_thread_ids
                or thread_source_machines.get(str(thread_id)) != machine
                or not isinstance(assignment, dict)
            ):
                continue
            source_project_id = str(assignment.get("projectId") or "")
            target = mapping.get(source_project_id)
            if not target:
                continue
            mapped_cwd = _map_source_path(
                str(assignment.get("cwd") or assignment.get("path") or ""),
                source_project_id=source_project_id,
                project_slug=str(target.get("project_slug") or ""),
                project_mapping=mapping,
                path_maps=[],
            )
            assignments[thread_id] = {
                **assignment,
                "projectKind": "local",
                "projectId": target["project_id"],
                "path": mapped_cwd,
                "cwd": mapped_cwd,
                "pendingCoreUpdate": False,
            }
            hints[thread_id] = mapped_cwd

        source_orders = source_ui.get("sidebar-project-thread-orders") if isinstance(source_ui.get("sidebar-project-thread-orders"), dict) else {}
        for source_project_id, thread_order in source_orders.items():
            target = mapping.get(str(source_project_id))
            if not target or not isinstance(thread_order, list):
                continue
            current_order = list(sidebar_orders.get(target["project_id"], []))
            for thread_id in thread_order:
                if (
                    thread_id in imported_thread_ids
                    and thread_source_machines.get(str(thread_id)) == machine
                    and thread_id not in current_order
                ):
                    current_order.append(thread_id)
            sidebar_orders[target["project_id"]] = current_order

        for thread_id in source_ui.get("pinned-thread-ids", []) if isinstance(source_ui.get("pinned-thread-ids"), list) else []:
            if thread_id in imported_thread_ids and thread_id not in pinned:
                pinned.append(thread_id)
        for thread_id in source_ui.get("projectless-thread-ids", []) if isinstance(source_ui.get("projectless-thread-ids"), list) else []:
            if (
                thread_id in imported_thread_ids
                and thread_source_machines.get(str(thread_id)) == machine
                and thread_id not in assignments
                and thread_id not in projectless
            ):
                projectless.append(thread_id)

    projectless = [
        thread_id
        for thread_id in projectless
        if thread_id not in imported_thread_ids or thread_id not in assignments
    ]

    state["local-projects"] = local_projects
    state["project-order"] = project_order
    state["electron-saved-workspace-roots"] = saved_roots
    state["electron-workspace-root-labels"] = labels
    state["thread-project-assignments"] = assignments
    state["thread-workspace-root-hints"] = hints
    state["sidebar-project-thread-orders"] = sidebar_orders
    state["pinned-thread-ids"] = pinned
    state["projectless-thread-ids"] = projectless
    return state


def _archive_pending_marker(codex_home: Path, status: str) -> str:
    pending = codex_home / ".local-state-sync-pending.json"
    if not pending.is_file():
        return ""
    history = codex_home / "state-sync-pending-history"
    history.mkdir(parents=True, exist_ok=True)
    destination = history / f"{_utc_stamp()}-{time.time_ns() % 1_000_000_000:09d}.json"
    os.replace(pending, destination)
    value = _read_json(destination, {})
    if isinstance(value, dict):
        value["resolved_at"] = iso_now()
        value["resolution"] = status
        _atomic_write_json(destination, value)
    return str(destination)


def _defer_pending_apply(codex_home: Path, archive: Path, selected: list[str]) -> dict[str, Any]:
    pending = codex_home / ".local-state-sync-pending.json"
    previous_pending = _archive_pending_marker(codex_home, "superseded")
    _atomic_write_json(
        pending,
        {
            "schema_version": SCHEMA_VERSION,
            "kind": "codex_state_sync_pending_apply",
            "created_at": iso_now(),
            "sources": selected,
            "archive_root": str(archive),
        },
    )
    return {
        "ok": True,
        "status": "deferred_codex_running",
        "pending": str(pending),
        "previous_pending": previous_pending,
        "sources": selected,
    }


def _apply_codex_state_unlocked(
    *,
    codex_home: str | Path | None = None,
    shared_root: str | Path | None = None,
    project_registry: str | Path | None = None,
    source_machines: Iterable[str] | None = None,
    path_map: Iterable[str] | None = None,
    yes: bool = False,
    dry_run: bool = False,
    defer_if_running: bool = False,
    current_machine: str | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    if not yes and not dry_run:
        raise StateSyncError("state-sync apply mutates native Codex indexes; pass --yes after reviewing status")
    home = _resolve_codex_home(codex_home)
    archive = _resolve_archive_root(shared_root, create=False)
    current = _validate_machine_id(current_machine or stable_machine_id())
    platform_value = platform_name or _platform_name()
    selected = [_validate_machine_id(str(value)) for value in source_machines or []]
    if not selected:
        selected = [row["machine_id"] for row in list_state_sync_sources(shared_root=shared_root, current_machine=current) if not row["current_machine"]]
    selected = list(dict.fromkeys(value for value in selected if value != current))
    if not selected:
        return {
            "ok": True,
            "status": "no_remote_sources",
            "sources": [],
            "resolved_pending": "",
        }
    if _codex_desktop_running(platform_value) and not dry_run:
        if defer_if_running:
            return _defer_pending_apply(home, archive, selected)
        raise StateSyncError("Codex Desktop is running; quit it before apply or pass --defer-if-running")

    sources = [_load_source_machine(archive, machine) for machine in selected]
    registry = _load_project_registry(project_registry)
    state_path = home / ".codex-global-state.json"
    target_state = _read_native_json_object(state_path)
    mappings = _parse_path_maps(path_map)
    project_mapping_by_machine: dict[str, dict[str, dict[str, Any]]] = {}
    resolved_targets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for source in sources:
        machine = str(source["manifest"].get("machine_id") or "")
        mapping, mapping_warnings = _map_projects_for_target(
            source["projects"],
            target_state,
            registry,
            platform_name=platform_value,
            path_maps=mappings,
        )
        for project in source["projects"]:
            source_id = str(project.get("source_project_id") or "")
            logical = str(project.get("logical_id") or project.get("project_slug") or project.get("name") or "").casefold()
            if not source_id or not logical or source_id not in mapping:
                continue
            if logical in resolved_targets:
                mapping[source_id] = dict(resolved_targets[logical])
            else:
                resolved_targets[logical] = dict(mapping[source_id])
        project_mapping_by_machine[machine] = mapping
        warnings.extend(mapping_warnings)

    missing_project_paths = sorted(
        {
            str(target.get("path") or "")
            for mapping in project_mapping_by_machine.values()
            for target in mapping.values()
            if not str(target.get("path") or "")
            or not Path(str(target.get("path") or "")).expanduser().is_dir()
        },
        key=str.casefold,
    )
    for path in missing_project_paths:
        warnings.append(f"resolved target project path does not exist: {path or '<empty>'}")

    summary = {
        "ok": True,
        "status": "dry_run" if dry_run else "applied",
        "sources": selected,
        "source_threads": sum(len(source["threads"]) for source in sources),
        "source_projects": sum(len(source["projects"]) for source in sources),
        "source_artifacts": sum(len(source["artifacts"]) for source in sources),
        "warnings": warnings,
        "missing_project_paths": missing_project_paths,
    }
    if dry_run:
        return summary
    if missing_project_paths:
        raise StateSyncError(
            "refusing to add invalid sidebar project roots; create or map these directories first: "
            + ", ".join(path or "<empty>" for path in missing_project_paths)
        )
    # Close the load/map race: Codex may have launched after the first probe.
    if _codex_desktop_running(platform_value):
        if defer_if_running:
            return _defer_pending_apply(home, archive, selected)
        raise StateSyncError("Codex Desktop started during apply preparation; quit it and retry")

    db_path = _state_db_path(home)
    backup = _backup_native_metadata(home, db_path)
    artifact_counts: dict[str, int] = {}
    artifact_paths: dict[tuple[str, str], str] = {}
    artifact_ids: dict[tuple[str, str], str] = {}
    remote_threads: list[dict[str, Any]] = []
    thread_by_id: dict[str, dict[str, Any]] = {}

    for source in sources:
        machine = str(source["manifest"].get("machine_id") or "")
        for row in source["artifacts"]:
            status, destination = _install_artifact(
                archive=archive,
                codex_home=home,
                source_machine=machine,
                row=row,
                backup_root=backup,
            )
            artifact_counts[status] = artifact_counts.get(status, 0) + 1
            relative = str(row.get("relative_path") or "")
            if destination and status not in {"conflict", "invalid", "missing_object", "retained"}:
                artifact_paths[(machine, relative)] = destination
                artifact_ids[(machine, relative)] = str(row.get("artifact_id") or "")
        for envelope in source["threads"]:
            row = dict(envelope)
            row["source_machine"] = machine
            thread_id = str(row.get("thread_id") or "")
            relative = str(row.get("artifact_relative_path") or "")
            artifact_path = artifact_paths.get((machine, relative), "")
            if (
                not thread_id
                or not artifact_path
                or artifact_ids.get((machine, relative), "") != str(row.get("artifact_id") or "")
            ):
                continue
            row["_artifact_path"] = artifact_path
            current_row = thread_by_id.get(thread_id)
            if not current_row or _thread_timestamp(row.get("thread") or {}) > _thread_timestamp(current_row.get("thread") or {}):
                thread_by_id[thread_id] = row

    remote_threads = list(thread_by_id.values())
    thread_counts = _merge_thread_rows(
        db_path,
        remote_threads,
        project_mapping_by_machine=project_mapping_by_machine,
        path_maps=mappings,
    )
    merged_thread_ids = set(str(value) for value in thread_counts.pop("merged_thread_ids", []))
    imported_thread_ids = set(str(value) for value in thread_counts.pop("ui_thread_ids", []))
    thread_source_machines = {
        str(row.get("thread_id") or ""): str(row.get("source_machine") or "")
        for row in remote_threads
        if str(row.get("thread_id") or "") in imported_thread_ids
    }
    merged_state = _merge_ui_state(
        target_state,
        sources,
        project_mapping_by_machine,
        imported_thread_ids,
        thread_source_machines,
    )
    _atomic_write_json(state_path, merged_state)
    merged_threads = [row for row in remote_threads if str(row.get("thread_id") or "") in imported_thread_ids]
    session_index_added = _merge_session_index(home, merged_threads)
    resolved_pending = _archive_pending_marker(home, "applied")
    summary.update(
        {
            "backup": str(backup),
            "artifact_results": artifact_counts,
            "thread_results": thread_counts,
            "session_index_added": session_index_added,
            "imported_thread_count": len(imported_thread_ids),
            "validated_thread_count": len(merged_thread_ids),
            "resolved_pending": resolved_pending,
        }
    )
    return summary


def apply_codex_state(
    *,
    codex_home: str | Path | None = None,
    shared_root: str | Path | None = None,
    project_registry: str | Path | None = None,
    source_machines: Iterable[str] | None = None,
    path_map: Iterable[str] | None = None,
    yes: bool = False,
    dry_run: bool = False,
    defer_if_running: bool = False,
    current_machine: str | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    if dry_run:
        return _apply_codex_state_unlocked(
            codex_home=codex_home,
            shared_root=shared_root,
            project_registry=project_registry,
            source_machines=source_machines,
            path_map=path_map,
            yes=yes,
            dry_run=True,
            defer_if_running=defer_if_running,
            current_machine=current_machine,
            platform_name=platform_name,
        )
    if not yes:
        raise StateSyncError("state-sync apply mutates native Codex indexes; pass --yes after reviewing status")
    current = _validate_machine_id(current_machine or stable_machine_id())
    resolved_home = _resolve_codex_home(codex_home)
    lock_id = "home-" + hashlib.sha256(_path_key(str(resolved_home), platform_name=_platform_name()).encode("utf-8")).hexdigest()[:24]
    with _apply_lock(lock_id):
        return _apply_codex_state_unlocked(
            codex_home=codex_home,
            shared_root=shared_root,
            project_registry=project_registry,
            source_machines=source_machines,
            path_map=path_map,
            yes=True,
            dry_run=False,
            defer_if_running=defer_if_running,
            current_machine=current,
            platform_name=platform_name,
        )


def _agent_command() -> str:
    configured = os.environ.get("AGENT_BRIDGE_HOOK_AGENT")
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("agent")
    return found or str(Path.home() / ".local" / "bin" / ("agent.cmd" if os.name == "nt" else "agent"))


def _powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _windows_scheduler_script(arguments: list[str], scheduler_log: Path) -> bytes:
    invocation = "& $agent " + " ".join(_powershell_literal(value) for value in arguments[1:])
    body = (
        "$ErrorActionPreference = 'Continue'\r\n"
        f"$agent = {_powershell_literal(arguments[0])}\r\n"
        f"if (-not (Test-Path -LiteralPath $agent)) {{ Add-Content -LiteralPath {_powershell_literal(scheduler_log)} -Value \"state-sync agent command is missing: $agent\"; exit 1 }}\r\n"
        f"{invocation} *>> {_powershell_literal(scheduler_log)}\r\n"
        "$exitCode = $LASTEXITCODE\r\n"
        "if ($null -eq $exitCode) { exit 1 }\r\n"
        "exit $exitCode\r\n"
    )
    return body.encode("utf-8")


def _scheduler_arguments(
    *,
    shared_root: Path,
    codex_home: Path,
    pull: bool,
    machine_id: str,
    project_registry: Path | None = None,
    source_machines: Iterable[str] | None = None,
    path_maps: Iterable[str] | None = None,
) -> list[str]:
    command = [_agent_command(), "code", "state-sync", "sync" if pull else "publish"]
    command.extend(["--shared-root", str(shared_root), "--codex-home", str(codex_home)])
    if project_registry is not None:
        command.extend(["--project-registry", str(project_registry)])
    command.extend(["--machine-id", machine_id, "--quiet"])
    if pull:
        command.extend(["--pull", "--yes", "--defer-if-running"])
        for source_machine in source_machines or []:
            command.extend(["--from-machine", _validate_machine_id(str(source_machine))])
        for path_map in path_maps or []:
            command.extend(["--path-map", str(path_map)])
    return command


def _macos_plist(
    *,
    arguments: list[str],
    interval_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
    machine_id: str | None = None,
) -> bytes:
    path_candidates = [
        str(Path(sys.executable).resolve().parent),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    scheduler_path = ":".join(dict.fromkeys(path for path in path_candidates if Path(path).is_dir()))
    value = {
        "Label": SCHEDULER_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "StartInterval": max(300, interval_seconds),
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": scheduler_path,
            **({"AGENT_BRIDGE_MACHINE_ID": machine_id} if machine_id else {}),
        },
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def install_state_sync_scheduler(
    *,
    platform_name: str | None = None,
    interval_seconds: int = 3600,
    shared_root: str | Path | None = None,
    codex_home: str | Path | None = None,
    project_registry: str | Path | None = None,
    pull: bool = False,
    machine_id: str | None = None,
    source_machines: Iterable[str] | None = None,
    path_maps: Iterable[str] | None = None,
) -> dict[str, Any]:
    platform_value = platform_name or _platform_name()
    home = _resolve_codex_home(codex_home)
    archive = _resolve_archive_root(shared_root, create=True)
    shared_data = archive.parent.parent
    registry_path = Path(project_registry).expanduser().resolve() if project_registry else _default_project_registry()
    if project_registry and (registry_path is None or not registry_path.is_file()):
        raise StateSyncError(f"project registry does not exist: {registry_path}")
    selected_sources = list(source_machines or [])
    selected_path_maps = list(path_maps or [])
    if not pull and (selected_sources or selected_path_maps):
        raise StateSyncError("--from-machine and --path-map require --pull when installing the scheduler")
    _parse_path_maps(selected_path_maps)
    scheduler_machine = _validate_machine_id(machine_id or stable_machine_id())
    logs = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")) / "state-sync" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    arguments = _scheduler_arguments(
        shared_root=shared_data,
        codex_home=home,
        pull=pull,
        machine_id=scheduler_machine,
        project_registry=registry_path,
        source_machines=selected_sources,
        path_maps=selected_path_maps,
    )
    if platform_value == "macos":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULER_LABEL}.plist"
        body = _macos_plist(
            arguments=arguments,
            interval_seconds=interval_seconds,
            stdout_path=logs / "scheduler.stdout.log",
            stderr_path=logs / "scheduler.stderr.log",
            machine_id=scheduler_machine,
        )
        _atomic_write_bytes(plist_path, body)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True, check=False)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise StateSyncError(f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}")
        return {
            "ok": True,
            "platform": platform_value,
            "scheduler": str(plist_path),
            "interval_seconds": max(300, interval_seconds),
            "pull": pull,
            "machine_id": scheduler_machine,
            "arguments": arguments,
        }
    if platform_value == "windows":
        state_root = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")) / "state-sync"
        state_root.mkdir(parents=True, exist_ok=True)
        wrapper = state_root / "run-state-sync.ps1"
        scheduler_log = logs / "scheduler.log"
        _atomic_write_bytes(wrapper, _windows_scheduler_script(arguments, scheduler_log))
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
        task_command = subprocess.list2cmdline(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)]
        )
        minutes = max(5, int(round(interval_seconds / 60)))
        result = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                WINDOWS_TASK_NAME,
                "/TR",
                task_command,
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
            raise StateSyncError(f"schtasks creation failed: {result.stderr.strip() or result.stdout.strip()}")
        return {
            "ok": True,
            "platform": platform_value,
            "scheduler": WINDOWS_TASK_NAME,
            "wrapper": str(wrapper),
            "interval_seconds": minutes * 60,
            "pull": pull,
            "machine_id": scheduler_machine,
            "arguments": arguments,
            "task_command": task_command,
        }
    raise StateSyncError(f"scheduler installation is not implemented for {platform_value}")


def uninstall_state_sync_scheduler(*, platform_name: str | None = None) -> dict[str, Any]:
    platform_value = platform_name or _platform_name()
    if platform_value == "macos":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULER_LABEL}.plist"
        if not plist_path.exists():
            return {"ok": True, "status": "not_installed", "scheduler": str(plist_path)}
        try:
            value = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException) as exc:
            raise StateSyncError(f"refusing to remove modified scheduler {plist_path}: {exc}") from exc
        if value.get("Label") != SCHEDULER_LABEL:
            raise StateSyncError(f"refusing to remove scheduler with unexpected label: {plist_path}")
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True, check=False)
        plist_path.unlink()
        return {"ok": True, "status": "removed", "scheduler": str(plist_path)}
    if platform_value == "windows":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 and "cannot find" not in (result.stderr + result.stdout).lower():
            raise StateSyncError(f"schtasks removal failed: {result.stderr.strip() or result.stdout.strip()}")
        return {"ok": True, "status": "removed", "scheduler": WINDOWS_TASK_NAME}
    raise StateSyncError(f"scheduler removal is not implemented for {platform_value}")


def state_sync_status(
    *,
    shared_root: str | Path | None = None,
    codex_home: str | Path | None = None,
    current_machine: str | None = None,
) -> dict[str, Any]:
    archive = _resolve_archive_root(shared_root, create=False)
    platform_value = _platform_name()
    scheduler: dict[str, Any]
    if platform_value == "macos":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SCHEDULER_LABEL}.plist"
        scheduler = {"installed": path.is_file(), "path": str(path)}
        if path.is_file():
            try:
                plist = plistlib.loads(path.read_bytes())
                scheduler["machine_id"] = str(
                    (plist.get("EnvironmentVariables") or {}).get("AGENT_BRIDGE_MACHINE_ID") or ""
                )
            except (OSError, plistlib.InvalidFileException):
                scheduler["configuration"] = "unreadable"
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{SCHEDULER_LABEL}"],
                capture_output=True,
                text=True,
                check=False,
            )
            scheduler["loaded"] = result.returncode == 0
            if result.returncode == 0:
                state_match = re.search(r"^\s*state = (.+)$", result.stdout, flags=re.MULTILINE)
                exit_match = re.search(r"^\s*last exit code = (-?\d+)$", result.stdout, flags=re.MULTILINE)
                scheduler["state"] = state_match.group(1).strip() if state_match else "unknown"
                scheduler["last_exit_code"] = int(exit_match.group(1)) if exit_match else None
    elif platform_value == "windows":
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        scheduler = {"installed": result.returncode == 0, "name": WINDOWS_TASK_NAME}
    else:
        scheduler = {"installed": False, "supported": False}
    home = _resolve_codex_home(codex_home)
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "archive_root": str(archive),
        "current_machine": current_machine or stable_machine_id(),
        "codex_home": str(home),
        "codex_desktop_running": _codex_desktop_running(platform_value),
        "pending_apply": str(home / ".local-state-sync-pending.json") if (home / ".local-state-sync-pending.json").is_file() else "",
        "scheduler": scheduler,
        "sources": list_state_sync_sources(shared_root=shared_root, current_machine=current_machine),
    }


def state_sync_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code state-sync",
        description=(
            "Incrementally synchronize Codex sessions and project structure without replacing native state. "
            "Copies raw prompt/tool content, attachments, and generated images as-is; use only a trusted private root "
            "and never apply untrusted manifests (the archive is not encrypted or cryptographically source-authenticated)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def shared_options(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--shared-root", help="SharedAgentData root; defaults to configured OneDrive discovery")
        command_parser.add_argument("--codex-home", help="Codex home; defaults to CODEX_HOME or ~/.codex")
        command_parser.add_argument("--project-registry", help="Optional SharedAgentConversations projects.json path")

    publish = sub.add_parser(
        "publish",
        help="Publish a baseline or only changed artifacts when a prior manifest exists",
        description=(
            "Publish included Codex session artifacts as-is to a trusted private root. "
            "The archive is not encrypted and does not scan or redact prompt, tool, attachment, or image content."
        ),
    )
    shared_options(publish)
    publish.add_argument("--machine-id")
    publish.add_argument("--settle-seconds", type=int, default=60)
    publish.add_argument("--compression-level", type=int, default=1)
    publish.add_argument("--metadata-only", action="store_true")
    publish.add_argument("--quiet", action="store_true")

    apply_parser = sub.add_parser(
        "apply",
        help="Additively import sessions/projects from one or more machines",
        description=(
            "Additively import from a trusted private root. Metadata hashes detect damage, not source identity; "
            "never apply an untrusted manifest."
        ),
    )
    shared_options(apply_parser)
    apply_parser.add_argument("--from-machine", action="append", default=[])
    apply_parser.add_argument("--machine-id", help="Override this machine identity (used by schedulers)")
    apply_parser.add_argument("--path-map", action="append", default=[])
    apply_parser.add_argument("--yes", action="store_true")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--defer-if-running", action="store_true")

    sync = sub.add_parser(
        "sync",
        help="Publish local changes and optionally import remote changes",
        description=(
            "Publish raw session artifacts as-is and optionally import trusted remote manifests. "
            "The archive is not encrypted, content-scanned, or cryptographically source-authenticated."
        ),
    )
    shared_options(sync)
    sync.add_argument("--pull", action="store_true")
    sync.add_argument("--machine-id", help="Override this machine identity (used by schedulers)")
    sync.add_argument("--from-machine", action="append", default=[])
    sync.add_argument("--path-map", action="append", default=[])
    sync.add_argument("--yes", action="store_true")
    sync.add_argument("--defer-if-running", action="store_true")
    sync.add_argument("--settle-seconds", type=int, default=60)
    sync.add_argument("--compression-level", type=int, default=1)
    sync.add_argument("--quiet", action="store_true")

    status = sub.add_parser("status", help="Show published machines, scheduler state, and pending apply status")
    shared_options(status)
    status.add_argument("--machine-id", help="Override this machine identity")

    install = sub.add_parser("install-scheduler", help="Install an hourly publisher or explicitly opted-in pull scheduler")
    shared_options(install)
    install.add_argument("--platform", default="auto", choices=["auto", "macos", "windows", "linux"])
    install.add_argument("--interval-seconds", type=int, default=3600)
    install.add_argument("--pull", action="store_true", help="Also import when Codex Desktop is not running")
    install.add_argument("--machine-id", help="Pin the scheduled publisher to this machine identity")
    install.add_argument("--from-machine", action="append", default=[], help="Pin a trusted pull source")
    install.add_argument("--path-map", action="append", default=[], help="Pin a source=target project path mapping")

    uninstall = sub.add_parser("uninstall-scheduler", help="Remove only the exact Agent Bridge state-sync scheduler")
    uninstall.add_argument("--platform", default="auto", choices=["auto", "macos", "windows", "linux"])

    args = parser.parse_args(argv)
    if args.command == "publish":
        result = publish_codex_state(
            codex_home=args.codex_home,
            shared_root=args.shared_root,
            project_registry=args.project_registry,
            machine_id=args.machine_id,
            settle_seconds=args.settle_seconds,
            compression_level=args.compression_level,
            metadata_only=args.metadata_only,
            progress=None if args.quiet else lambda message: print(message, file=sys.stderr),
        )
    elif args.command == "apply":
        result = apply_codex_state(
            codex_home=args.codex_home,
            shared_root=args.shared_root,
            project_registry=args.project_registry,
            source_machines=args.from_machine,
            path_map=args.path_map,
            yes=args.yes,
            dry_run=args.dry_run,
            defer_if_running=args.defer_if_running,
            current_machine=args.machine_id,
        )
    elif args.command == "sync":
        published = publish_codex_state(
            codex_home=args.codex_home,
            shared_root=args.shared_root,
            project_registry=args.project_registry,
            machine_id=args.machine_id,
            settle_seconds=args.settle_seconds,
            compression_level=args.compression_level,
            progress=None if args.quiet else lambda message: print(message, file=sys.stderr),
        )
        applied: dict[str, Any] = {"ok": True, "status": "pull_disabled"}
        if args.pull:
            applied = apply_codex_state(
                codex_home=args.codex_home,
                shared_root=args.shared_root,
                project_registry=args.project_registry,
                source_machines=args.from_machine,
                path_map=args.path_map,
                yes=args.yes,
                defer_if_running=args.defer_if_running,
                current_machine=args.machine_id,
            )
        result = {"ok": bool(published.get("ok") and applied.get("ok")), "published": published, "applied": applied}
    elif args.command == "status":
        result = state_sync_status(
            shared_root=args.shared_root,
            codex_home=args.codex_home,
            current_machine=args.machine_id,
        )
    elif args.command == "install-scheduler":
        platform_value = _platform_name() if args.platform == "auto" else args.platform
        result = install_state_sync_scheduler(
            platform_name=platform_value,
            interval_seconds=args.interval_seconds,
            shared_root=args.shared_root,
            codex_home=args.codex_home,
            project_registry=args.project_registry,
            pull=args.pull,
            machine_id=args.machine_id,
            source_machines=args.from_machine,
            path_maps=args.path_map,
        )
    else:
        platform_value = _platform_name() if args.platform == "auto" else args.platform
        result = uninstall_state_sync_scheduler(platform_name=platform_value)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1
