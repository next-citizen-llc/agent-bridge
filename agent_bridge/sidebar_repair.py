"""Offline, field-scoped repair for Codex Desktop's Windows project sidebar.

The repair restores only project/sidebar metadata from a known-good global-state
backup. It never replaces the Codex SQLite index, sessions, transcripts, prompt
history, projectless-task list, or unrelated Electron preferences.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import unquote

from .pointer_sync import (
    _atomic_write,
    _is_windows_path,
    _is_wsl_unc,
    _json_bytes,
    _local_state_root,
    _read_json,
    _registry_path,
    _slugify,
    _strip_extended_prefix,
)


SCHEMA_VERSION = "1.0"
PENDING_FILE = "pending-sidebar-repair.json"
PROJECTION_FILE_PREFIX = "windows-sidebar-projection"

# These fields jointly define the local-project/sidebar projection. A missing
# field in the known-good source means it should be absent after repair so that
# Codex can rebuild it from the restored local-project records.
SIDEBAR_TOP_LEVEL_FIELDS = (
    "local-projects",
    "project-order",
    "selected-project",
    "thread-project-assignments",
    "thread-workspace-root-hints",
    "electron-saved-workspace-roots",
    "active-workspace-roots",
    "electron-workspace-root-labels",
    "sidebar-project-thread-orders",
)

ATOM_EXACT_FIELDS = (
    "flat-project-sidebar-preferences-v1",
    "unified-sidebar-project-order-v1",
    "sidebar-collapsed-groups",
)
ATOM_PROJECT_PREFIX = "sidebar-project-expanded-v1-codex:"
ATOM_FOREIGN_COMPOSER_PREFIX = "composer-mode-by-project:"
CODEX_PROCESS_NAMES = frozenset({"chatgpt.exe", "codex.exe", "codex-code-mode-host.exe"})


class SidebarRepairError(ValueError):
    """Raised when a sidebar repair cannot be proven safe to stage or apply."""


def _read_required_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SidebarRepairError(f"could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SidebarRepairError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SidebarRepairError(f"expected a JSON object in {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SidebarRepairError(f"could not hash {path}: {exc}") from exc
    return digest.hexdigest()


def _codex_home(value: str | Path | None = None) -> Path:
    configured = value or os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def _global_state_path(codex_home: str | Path | None = None) -> Path:
    path = _codex_home(codex_home) / ".codex-global-state.json"
    if not path.is_file():
        raise SidebarRepairError(f"Codex global state was not found: {path}")
    return path


def _pending_path() -> Path:
    return _local_state_root() / "pointer-sync" / PENDING_FILE


def _find_source(codex_home: Path, explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SidebarRepairError(f"sidebar repair source does not exist: {path}")
        return path
    candidates = sorted(
        codex_home.glob("backups/sidebar-state-sync-*/.codex-global-state.json"),
        key=lambda path: path.parent.name,
        reverse=True,
    )
    if not candidates:
        raise SidebarRepairError(
            f"no sidebar-state-sync backup exists under {codex_home / 'backups'}; pass --source"
        )
    return candidates[0].resolve()


def _windows_native_path(value: str) -> bool:
    return _is_windows_path(value) and not _is_wsl_unc(value)


def _path_exists(value: str) -> bool:
    try:
        return Path(_strip_extended_prefix(value)).exists()
    except OSError:
        return False


def _path_is_dir(value: str) -> bool:
    try:
        return Path(_strip_extended_prefix(value)).is_dir()
    except OSError:
        return False


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _portable_path(value: str) -> str:
    """Normalize a local path for cross-platform registry comparisons."""

    path = unquote(_strip_extended_prefix(value.strip()))
    if _is_wsl_unc(path):
        match = re.match(r"^\\\\(?:wsl\$|wsl\.localhost)\\[^\\]+\\(.+)$", path, flags=re.IGNORECASE)
        if match:
            path = "/" + match.group(1).replace("\\", "/")
    return path.replace("\\", "/").rstrip("/")


def _path_key(value: str) -> str:
    return _portable_path(value).casefold()


def _is_foreign_runtime_path(value: str) -> bool:
    if _is_wsl_unc(value):
        return True
    portable = _portable_path(value)
    return bool(re.match(r"^/(?:users|home)/|^/mnt/[a-z]/", portable, flags=re.IGNORECASE))


def _foreign_runtime_paths(value: Any, *, location: str = "$") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        if _is_foreign_runtime_path(value):
            found.append((location, value))
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _is_foreign_runtime_path(key):
                found.append((f"{location}.<key>", key))
            found.extend(_foreign_runtime_paths(child, location=f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_foreign_runtime_paths(child, location=f"{location}[{index}]"))
    return found


def _windows_candidates(project: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for field in ("workspace_windows", "workspace_windows_aliases"):
        candidates.extend(_string_values(project.get(field)))
    workspaces = project.get("workspaces")
    for workspace in workspaces if isinstance(workspaces, list) else [workspaces]:
        if not isinstance(workspace, dict) or str(workspace.get("os") or "").casefold() != "windows":
            continue
        path = workspace.get("path")
        if isinstance(path, str):
            candidates.append(path)
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _strip_extended_prefix(unquote(candidate))
        key = _path_key(normalized)
        if key in seen or not _windows_native_path(normalized) or not _path_exists(normalized):
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _registry_aliases(project: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for field in (
        "workspace_windows",
        "workspace_windows_aliases",
        "workspace_linux",
        "workspace_linux_aliases",
        "workspace_macos",
        "workspace_macos_aliases",
    ):
        aliases.extend(_string_values(project.get(field)))
    workspaces = project.get("workspaces")
    for workspace in workspaces if isinstance(workspaces, list) else [workspaces]:
        if isinstance(workspace, dict) and isinstance(workspace.get("path"), str):
            aliases.append(str(workspace["path"]))
    return aliases


class _WindowsPathResolver:
    def __init__(self, *, windows_home: Path, registry: dict[str, Any]) -> None:
        self.windows_home = windows_home.resolve()
        onedrive_roots: list[Path] = []
        for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            configured = os.environ.get(variable)
            if configured:
                configured_path = Path(configured).expanduser()
                configured_key = _path_key(str(configured_path))
                home_key = _path_key(str(self.windows_home))
                if self.windows_home == Path.home().resolve() or configured_key.startswith(home_key + "/"):
                    onedrive_roots.append(configured_path)
        try:
            onedrive_roots.extend(path for path in self.windows_home.glob("OneDrive*") if path.is_dir())
        except OSError:
            pass
        self._onedrive_roots: list[Path] = []
        seen_onedrive: set[str] = set()
        for root in onedrive_roots:
            if not root.is_dir():
                continue
            key = _path_key(str(root))
            if key in seen_onedrive:
                continue
            seen_onedrive.add(key)
            self._onedrive_roots.append(root)
        self._exact: dict[str, list[str]] = {}
        self._names: dict[str, list[str]] = {}
        projects = registry.get("projects") if isinstance(registry.get("projects"), list) else []
        for project in projects:
            if not isinstance(project, dict):
                continue
            candidates = _windows_candidates(project)
            if not candidates:
                continue
            for alias in _registry_aliases(project):
                self._exact.setdefault(_path_key(alias), []).extend(candidates)
            for name in (str(project.get("name") or ""), str(project.get("slug") or "")):
                if name:
                    self._names.setdefault(_slugify(name), []).extend(candidates)

    def _candidate_score(self, value: str) -> tuple[int, int, str]:
        normalized = _portable_path(value).casefold()
        home = _portable_path(str(self.windows_home)).casefold()
        score = 0
        if normalized.startswith(home + "/code/") and _path_exists(str(Path(value) / ".git")):
            score = 100
        else:
            onedrive_relative = ""
            for root in self._onedrive_roots:
                root_key = _portable_path(str(root)).casefold()
                if normalized == root_key or normalized.startswith(root_key + "/"):
                    onedrive_relative = normalized[len(root_key) :].lstrip("/")
                    break
            if onedrive_relative.startswith("sharedprojects/"):
                score = 90
            elif onedrive_relative.startswith("documents/claude/projects/"):
                score = 80
            elif onedrive_relative.startswith("sharedagentskills/"):
                score = 70
            elif onedrive_relative.startswith("sharedagentprojects/"):
                score = 60
            elif onedrive_relative:
                score = 75
            elif _path_exists(str(Path(value) / ".git")):
                score = 50
            else:
                score = 40
        return score, -len(value), normalized

    def _onedrive_candidates(self, provider: str, relative: str) -> list[Path]:
        provider_key = re.sub(r"[^a-z0-9]+", "", provider.casefold())
        ranked = sorted(
            self._onedrive_roots,
            key=lambda root: (
                re.sub(r"[^a-z0-9]+", "", root.name.casefold()) == provider_key,
                "personal" in provider_key and root.name.casefold() == "onedrive",
                -len(str(root)),
            ),
            reverse=True,
        )
        return [root.joinpath(*relative.split("/")) for root in ranked]

    def _heuristic_candidates(self, value: str, project_name: str) -> list[str]:
        portable = _portable_path(value)
        candidates: list[Path] = []
        worktree = re.match(
            r"^/(?:users|home)/[^/]+/\.codex/worktrees/[^/]+/([^/]+)(?:/.*)?$",
            portable,
            re.IGNORECASE,
        )
        if worktree:
            candidates.append(self.windows_home / "Code" / worktree.group(1))
        prefixes = (
            (r"^/users/[^/]+/code/(.+)$", self.windows_home / "Code"),
            (r"^/home/[^/]+/code/(.+)$", self.windows_home / "Code"),
            (r"^/users/[^/]+/documents/claude/projects/(.+)$", self.windows_home / "OneDrive" / "Documents" / "Claude" / "Projects"),
            (r"^/users/[^/]+/documents/(.+)$", self.windows_home / "Documents"),
        )
        for pattern, base in prefixes:
            match = re.match(pattern, portable, re.IGNORECASE)
            if match:
                candidates.append(base.joinpath(*match.group(1).split("/")))
        cloud = re.match(
            r"^/users/[^/]+/library/cloudstorage/(onedrive-[^/]+)/(.+)$",
            portable,
            re.IGNORECASE,
        )
        if cloud:
            candidates.extend(self._onedrive_candidates(cloud.group(1), cloud.group(2)))
        mounted = re.match(r"^/mnt/([a-z])/(.+)$", portable, re.IGNORECASE)
        if mounted:
            candidates.append(Path(f"{mounted.group(1).upper()}:\\").joinpath(*mounted.group(2).split("/")))
        if re.match(r"^/(?:users|home)/[^/]+$", portable, re.IGNORECASE) and _slugify(project_name) in {
            "home",
            _slugify(self.windows_home.name),
        }:
            candidates.append(self.windows_home)

        if re.match(r"^[A-Za-z]:/", portable):
            candidates.append(Path(portable.replace("/", "\\")))
            old_user = re.match(r"^[A-Za-z]:/users/[^/]+/(.+)$", portable, re.IGNORECASE)
            if old_user:
                candidates.append(self.windows_home.joinpath(*old_user.group(1).split("/")))
            old_cloud = re.match(
                r"^[A-Za-z]:/users/[^/]+/library/cloudstorage/(onedrive-[^/]+)/(.+)$",
                portable,
                re.IGNORECASE,
            )
            if old_cloud:
                candidates[0:0] = self._onedrive_candidates(old_cloud.group(1), old_cloud.group(2))

        result: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            text = str(candidate)
            key = _path_key(text)
            if key in seen or not candidate.is_dir():
                continue
            seen.add(key)
            result.append(text)
        return result

    def resolve(self, value: str, *, project_name: str = "") -> str | None:
        candidates = self._heuristic_candidates(value, project_name)
        candidates.extend(self._exact.get(_path_key(value), []))
        if project_name:
            candidates.extend(self._names.get(_slugify(project_name), []))
        valid: dict[str, str] = {}
        for candidate in candidates:
            normalized = _strip_extended_prefix(candidate)
            if _windows_native_path(normalized) and _path_exists(normalized):
                valid.setdefault(_path_key(normalized), normalized)
        if not valid:
            return None
        return max(valid.values(), key=self._candidate_score)


_DROP_VALUE = object()


def _sanitize_foreign_paths(
    value: Any,
    *,
    resolver: _WindowsPathResolver,
    project_name: str = "",
    fallback: str | None = None,
) -> Any:
    if isinstance(value, str):
        if not _is_foreign_runtime_path(value):
            return value
        return resolver.resolve(value, project_name=project_name) or fallback or _DROP_VALUE
    if isinstance(value, list):
        result: list[Any] = []
        for child in value:
            sanitized = _sanitize_foreign_paths(
                child,
                resolver=resolver,
                project_name=project_name,
                fallback=fallback,
            )
            if sanitized is not _DROP_VALUE:
                result.append(sanitized)
        return result
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, child in value.items():
            sanitized_key = _sanitize_foreign_paths(
                key,
                resolver=resolver,
                project_name=project_name,
                fallback=fallback,
            )
            sanitized_child = _sanitize_foreign_paths(
                child,
                resolver=resolver,
                project_name=project_name,
                fallback=fallback,
            )
            if sanitized_key is _DROP_VALUE or sanitized_child is _DROP_VALUE:
                continue
            result[sanitized_key] = sanitized_child
        return result
    return copy.deepcopy(value)


def _source_projects(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    projects = source.get("local-projects")
    if not isinstance(projects, dict) or not projects:
        return [], ["source local-projects is missing or empty"]
    rows: list[dict[str, Any]] = []
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            issues.append(f"project {project_id} is not an object")
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list) or not roots or not all(isinstance(root, str) for root in roots):
            issues.append(f"project {project_id} has no valid rootPaths list")
            continue
        for root in roots:
            if not _windows_native_path(root):
                issues.append(f"project {project_id} has a non-Windows-native root: {root}")
            elif not _path_is_dir(root):
                issues.append(f"project {project_id} root does not exist: {root}")
        rows.append(
            {
                "project_id": str(project_id),
                "name": str(project.get("name") or project_id),
                "root_paths": list(roots),
            }
        )
    order = source.get("project-order")
    if not isinstance(order, list) or any(str(project_id) not in projects for project_id in order):
        issues.append("source project-order is missing or references unknown projects")
    project_ids = {str(project_id) for project_id in projects}
    selected = source.get("selected-project")
    if isinstance(selected, dict) and selected.get("projectKind") == "local":
        selected_id = str(selected.get("projectId") or "")
        if selected_id not in project_ids:
            issues.append("source selected-project references an unknown local project")
    assignments = source.get("thread-project-assignments")
    if isinstance(assignments, dict):
        for thread_id, assignment in assignments.items():
            if not isinstance(assignment, dict):
                issues.append(f"thread assignment {thread_id} is not an object")
                continue
            project_id = str(assignment.get("projectId") or "")
            if project_id and project_id not in project_ids:
                issues.append(f"thread assignment {thread_id} references unknown project {project_id}")
            for field in ("cwd", "path"):
                value = assignment.get(field)
                if isinstance(value, str) and value and not _windows_native_path(value):
                    issues.append(f"thread assignment {thread_id} has non-Windows {field}: {value}")
                elif isinstance(value, str) and value and not _path_is_dir(value):
                    issues.append(f"thread assignment {thread_id} {field} does not exist: {value}")
    hints = source.get("thread-workspace-root-hints")
    if isinstance(hints, dict):
        for thread_id, value in hints.items():
            if not isinstance(value, str) or not _windows_native_path(value):
                issues.append(f"thread workspace hint {thread_id} is not Windows-native")
            elif not _path_is_dir(value):
                issues.append(f"thread workspace hint {thread_id} does not exist: {value}")
    for field in ("electron-saved-workspace-roots", "active-workspace-roots"):
        values = source.get(field)
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, str) or not _windows_native_path(value):
                    issues.append(f"source {field} contains a non-Windows-native path")
                elif not _path_is_dir(value):
                    issues.append(f"source {field} contains a path that does not exist: {value}")
    labels = source.get("electron-workspace-root-labels")
    if isinstance(labels, dict):
        for value in labels:
            if not _windows_native_path(str(value)):
                issues.append("source electron-workspace-root-labels contains a non-Windows-native path")
            elif not _path_is_dir(str(value)):
                issues.append("source electron-workspace-root-labels contains a path that does not exist")
    restored_view = {key: source[key] for key in SIDEBAR_TOP_LEVEL_FIELDS if key in source}
    raw_atom = source.get("electron-persisted-atom-state")
    if isinstance(raw_atom, dict):
        restored_view["electron-persisted-atom-state"] = {
            key: value
            for key, value in raw_atom.items()
            if key in ATOM_EXACT_FIELDS or key.startswith(ATOM_PROJECT_PREFIX)
        }
    for location, value in _foreign_runtime_paths(restored_view):
        issues.append(f"source contains a residual foreign runtime path at {location}: {value}")
    return rows, issues


def _load_project_registry(explicit: str | Path | None = None) -> tuple[Path | None, dict[str, Any]]:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise SidebarRepairError(f"project registry does not exist: {path}")
    else:
        path = _registry_path()
    if path is None:
        return None, {"projects": []}
    value = _read_json(path, {})
    if not isinstance(value, dict):
        raise SidebarRepairError(f"project registry is not a JSON object: {path}")
    return path, value


def _unique_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _path_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _build_windows_projection(
    live: dict[str, Any],
    *,
    resolver: _WindowsPathResolver,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_projects = live.get("local-projects")
    if not isinstance(raw_projects, dict) or not raw_projects:
        raise SidebarRepairError("live local-projects is missing or empty")

    projects: dict[str, Any] = {}
    root_map: dict[str, str] = {}
    primary_roots: dict[str, str] = {}
    mappings: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for raw_project_id, raw_project in raw_projects.items():
        project_id = str(raw_project_id)
        if not isinstance(raw_project, dict):
            dropped.append({"project_id": project_id, "name": project_id, "reason": "project is not an object"})
            continue
        name = str(raw_project.get("name") or project_id)
        roots = _string_values(raw_project.get("rootPaths"))
        if project_id.startswith("g-p-"):
            dropped.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "roots": roots,
                    "reason": "ChatGPT projects are managed separately from local Codex projects",
                }
            )
            continue
        mapped_roots: list[str] = []
        for root in roots:
            mapped = resolver.resolve(root, project_name=name)
            if not mapped:
                continue
            mapped_roots.append(mapped)
            root_map[_path_key(root)] = mapped
            mappings.append({"project_id": project_id, "name": name, "from": root, "to": mapped})
        mapped_roots = _unique_paths(mapped_roots)
        if not mapped_roots:
            dropped.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "roots": roots,
                    "reason": "no verified Windows-native folder exists",
                }
            )
            continue
        project = _sanitize_foreign_paths(
            raw_project,
            resolver=resolver,
            project_name=name,
            fallback=mapped_roots[0],
        )
        if not isinstance(project, dict):
            raise SidebarRepairError(f"project {project_id} could not be sanitized")
        project["rootPaths"] = mapped_roots
        projects[project_id] = project
        primary_roots[project_id] = mapped_roots[0]

    if not projects:
        raise SidebarRepairError("no imported project could be mapped to a verified Windows-native folder")

    source: dict[str, Any] = {"local-projects": projects}
    raw_order = live.get("project-order")
    ordered = [str(project_id) for project_id in raw_order] if isinstance(raw_order, list) else []
    order = [project_id for project_id in ordered if project_id in projects]
    order.extend(project_id for project_id in projects if project_id not in order)
    source["project-order"] = order

    selected = live.get("selected-project")
    if isinstance(selected, dict):
        if selected.get("projectKind") != "local" or str(selected.get("projectId") or "") in projects:
            selected_id = str(selected.get("projectId") or "")
            sanitized_selected = _sanitize_foreign_paths(
                selected,
                resolver=resolver,
                project_name=str(projects.get(selected_id, {}).get("name") or ""),
                fallback=primary_roots.get(selected_id),
            )
            if isinstance(sanitized_selected, dict):
                source["selected-project"] = sanitized_selected

    source["electron-saved-workspace-roots"] = _unique_paths(
        [root for project in projects.values() for root in _string_values(project.get("rootPaths"))]
    )
    active = live.get("active-workspace-roots")
    if isinstance(active, list):
        mapped_active = [
            root_map.get(_path_key(value)) or resolver.resolve(value)
            for value in active
            if isinstance(value, str)
        ]
        source["active-workspace-roots"] = _unique_paths([value for value in mapped_active if value])

    labels: dict[str, Any] = {}
    raw_labels = live.get("electron-workspace-root-labels")
    if isinstance(raw_labels, dict):
        for raw_root, label in raw_labels.items():
            mapped = root_map.get(_path_key(str(raw_root))) or resolver.resolve(str(raw_root), project_name=str(label))
            if mapped:
                labels[mapped] = copy.deepcopy(label)
    for project in projects.values():
        for root in _string_values(project.get("rootPaths")):
            labels.setdefault(root, str(project.get("name") or Path(root).name))
    source["electron-workspace-root-labels"] = labels

    assignments: dict[str, Any] = {}
    raw_assignments = live.get("thread-project-assignments")
    if isinstance(raw_assignments, dict):
        for raw_thread_id, raw_assignment in raw_assignments.items():
            if not isinstance(raw_assignment, dict):
                continue
            project_id = str(raw_assignment.get("projectId") or "")
            if project_id not in projects:
                continue
            project_name = str(projects[project_id].get("name") or project_id)
            assignment = _sanitize_foreign_paths(
                raw_assignment,
                resolver=resolver,
                project_name=project_name,
                fallback=primary_roots[project_id],
            )
            if not isinstance(assignment, dict):
                continue
            for field in ("cwd", "path"):
                value = assignment.get(field)
                if isinstance(value, str) and value:
                    assignment[field] = (
                        root_map.get(_path_key(value))
                        or resolver.resolve(value, project_name=project_name)
                        or primary_roots[project_id]
                    )
            assignments[str(raw_thread_id)] = assignment
    source["thread-project-assignments"] = assignments

    hints: dict[str, str] = {}
    raw_hints = live.get("thread-workspace-root-hints")
    if isinstance(raw_hints, dict):
        for raw_thread_id, raw_hint in raw_hints.items():
            thread_id = str(raw_thread_id)
            assignment = assignments.get(thread_id)
            if not assignment:
                continue
            project_id = str(assignment.get("projectId") or "")
            if project_id not in projects:
                continue
            mapped = None
            if isinstance(raw_hint, str):
                mapped = root_map.get(_path_key(raw_hint)) or resolver.resolve(
                    raw_hint,
                    project_name=str(projects[project_id].get("name") or project_id),
                )
            hints[thread_id] = mapped or str(assignment.get("cwd") or primary_roots[project_id])
    source["thread-workspace-root-hints"] = hints

    raw_thread_orders = live.get("sidebar-project-thread-orders")
    if isinstance(raw_thread_orders, dict):
        source["sidebar-project-thread-orders"] = {
            str(project_id): copy.deepcopy(thread_ids)
            for project_id, thread_ids in raw_thread_orders.items()
            if str(project_id) in projects
        }

    raw_atom = live.get("electron-persisted-atom-state")
    atom: dict[str, Any] = {}
    if isinstance(raw_atom, dict):
        if "flat-project-sidebar-preferences-v1" in raw_atom:
            sanitized_preferences = _sanitize_foreign_paths(
                raw_atom["flat-project-sidebar-preferences-v1"],
                resolver=resolver,
            )
            if sanitized_preferences is not _DROP_VALUE:
                atom["flat-project-sidebar-preferences-v1"] = sanitized_preferences
        for project_id in projects:
            key = f"{ATOM_PROJECT_PREFIX}{project_id}"
            if key in raw_atom:
                atom[key] = copy.deepcopy(raw_atom[key])
    atom["unified-sidebar-project-order-v1"] = [f"codex:project:{project_id}" for project_id in order]
    source["electron-persisted-atom-state"] = atom

    details = {
        "mapped_project_count": len(projects),
        "dropped_project_count": len(dropped),
        "path_mapping_count": len(mappings),
        "mapped_paths": mappings,
        "dropped_projects": dropped,
        "assignment_count": len(assignments),
    }
    return source, details


def _windows_path_repair_plan(
    *,
    codex_home: str | Path | None = None,
    project_registry: str | Path | None = None,
    windows_home: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    home = _codex_home(codex_home)
    live_path = _global_state_path(home)
    live = _read_required_json(live_path)
    registry_path, registry = _load_project_registry(project_registry)
    resolver = _WindowsPathResolver(
        windows_home=Path(windows_home).expanduser() if windows_home else Path.home(),
        registry=registry,
    )
    source, mapping_details = _build_windows_projection(live, resolver=resolver)
    projects, issues = _source_projects(source)
    repaired, repair_details = _repair_projection(live, source)
    processes = _codex_processes()
    report = {
        "ok": not issues,
        "status": "ready" if not issues else "invalid_projection",
        "schema_version": SCHEMA_VERSION,
        "live_state": str(live_path),
        "project_registry": str(registry_path) if registry_path else None,
        "projects": projects,
        "project_count": len(projects),
        "validation_issues": issues,
        "changed": _json_bytes(live) != _json_bytes(repaired),
        "codex_processes": processes,
        "can_apply_now": not issues and not processes,
        **mapping_details,
        **repair_details,
    }
    return source, report


def preview_windows_path_repair(
    *,
    codex_home: str | Path | None = None,
    project_registry: str | Path | None = None,
    windows_home: str | Path | None = None,
) -> dict[str, Any]:
    _, report = _windows_path_repair_plan(
        codex_home=codex_home,
        project_registry=project_registry,
        windows_home=windows_home,
    )
    return report


def stage_windows_path_repair(
    *,
    codex_home: str | Path | None = None,
    project_registry: str | Path | None = None,
    windows_home: str | Path | None = None,
) -> dict[str, Any]:
    source, report = _windows_path_repair_plan(
        codex_home=codex_home,
        project_registry=project_registry,
        windows_home=windows_home,
    )
    if not report["ok"]:
        raise SidebarRepairError(
            "Windows path projection failed validation: " + "; ".join(report["validation_issues"])
        )
    body = _json_bytes(source)
    digest = hashlib.sha256(body).hexdigest()
    source_path = _local_state_root() / "pointer-sync" / f"{PROJECTION_FILE_PREFIX}-{digest[:16]}.json"
    _atomic_write(source_path, body)
    staged = stage_sidebar_repair(codex_home=codex_home, source=source_path)
    return {
        **staged,
        "projection_source": str(source_path),
        "project_registry": report["project_registry"],
        "mapped_project_count": report["mapped_project_count"],
        "dropped_project_count": report["dropped_project_count"],
        "path_mapping_count": report["path_mapping_count"],
        "mapped_paths": report["mapped_paths"],
        "dropped_projects": report["dropped_projects"],
    }


def apply_windows_path_repair(
    *,
    codex_home: str | Path | None = None,
    project_registry: str | Path | None = None,
    windows_home: str | Path | None = None,
) -> dict[str, Any]:
    source, report = _windows_path_repair_plan(
        codex_home=codex_home,
        project_registry=project_registry,
        windows_home=windows_home,
    )
    if report["codex_processes"]:
        names = ", ".join(f"{row['name']}:{row['pid']}" for row in report["codex_processes"])
        raise SidebarRepairError(f"close Codex before applying Windows path repair; active process(es): {names}")
    if not report["ok"]:
        raise SidebarRepairError(
            "Windows path projection failed validation: " + "; ".join(report["validation_issues"])
        )
    body = _json_bytes(source)
    source_hash = hashlib.sha256(body).hexdigest()
    source_path = _local_state_root() / "pointer-sync" / f"{PROJECTION_FILE_PREFIX}-{source_hash[:16]}.json"
    _atomic_write(source_path, body)
    live_hash = _sha256(Path(str(report["live_state"])))
    result = apply_sidebar_repair(
        codex_home=codex_home,
        source=source_path,
        expected_source_sha256=source_hash,
        expected_live_sha256=live_hash,
    )
    pending = _pending_path()
    if pending.is_file():
        pending.unlink()
        result["pending_removed"] = str(pending)
    result.update(
        {
            "projection_source": str(source_path),
            "project_registry": report["project_registry"],
            "mapped_project_count": report["mapped_project_count"],
            "dropped_project_count": report["dropped_project_count"],
            "path_mapping_count": report["path_mapping_count"],
            "mapped_paths": report["mapped_paths"],
            "dropped_projects": report["dropped_projects"],
        }
    )
    return result


def _managed_atom_keys(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    return {
        key
        for key in value
        if key in ATOM_EXACT_FIELDS or key.startswith(ATOM_PROJECT_PREFIX)
    }


def _foreign_composer_keys(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    result: set[str] = set()
    for key in value:
        if not key.startswith(ATOM_FOREIGN_COMPOSER_PREFIX):
            continue
        payload = key[len(ATOM_FOREIGN_COMPOSER_PREFIX) :]
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError:
            decoded = payload
        if isinstance(decoded, list):
            candidates = [item for item in decoded if isinstance(item, str)]
        else:
            candidates = [decoded] if isinstance(decoded, str) else []
        if any(candidate.startswith("/") or _is_wsl_unc(candidate) for candidate in candidates):
            result.add(key)
    return result


def _codex_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # An unavailable process probe is a stop condition, not permission to
        # overwrite state whose writer status is unknown.
        return [{"name": "process-probe-unavailable", "pid": None}]
    if result.returncode != 0:
        return [{"name": "process-probe-unavailable", "pid": None}]
    rows: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2 or row[0].casefold() not in CODEX_PROCESS_NAMES:
            continue
        try:
            pid: int | None = int(row[1])
        except ValueError:
            pid = None
        rows.append({"name": row[0], "pid": pid})
    return rows


def _repair_projection(live: dict[str, Any], source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    repaired = copy.deepcopy(live)
    replaced: list[str] = []
    removed: list[str] = []
    for key in SIDEBAR_TOP_LEVEL_FIELDS:
        if key in source:
            repaired[key] = copy.deepcopy(source[key])
            replaced.append(key)
        elif key in repaired:
            repaired.pop(key, None)
            removed.append(key)

    raw_source_atom = source.get("electron-persisted-atom-state")
    source_atom = raw_source_atom if isinstance(raw_source_atom, dict) else {}
    raw_live_atom = repaired.get("electron-persisted-atom-state")
    managed_source = _managed_atom_keys(source_atom)
    if isinstance(raw_live_atom, dict):
        live_atom: dict[str, Any] | None = raw_live_atom
    elif managed_source:
        live_atom = {}
        repaired["electron-persisted-atom-state"] = live_atom
    else:
        live_atom = None
    if live_atom is None:
        managed_live: set[str] = set()
        foreign_composer: set[str] = set()
    else:
        managed_live = _managed_atom_keys(live_atom)
        foreign_composer = _foreign_composer_keys(live_atom)
        for key in sorted(managed_live | foreign_composer):
            live_atom.pop(key, None)
        for key in sorted(managed_source):
            live_atom[key] = copy.deepcopy(source_atom[key])
    details = {
        "top_level_replaced": replaced,
        "top_level_removed": removed,
        "atom_removed_count": len(managed_live | foreign_composer),
        "atom_restored_count": len(managed_source),
    }
    return repaired, details


def preview_sidebar_repair(
    *,
    codex_home: str | Path | None = None,
    source: str | Path | None = None,
) -> dict[str, Any]:
    home = _codex_home(codex_home)
    live_path = _global_state_path(home)
    source_path = _find_source(home, source)
    live = _read_required_json(live_path)
    known_good = _read_required_json(source_path)
    projects, issues = _source_projects(known_good)
    repaired, details = _repair_projection(live, known_good)
    processes = _codex_processes()
    changed = _json_bytes(live) != _json_bytes(repaired)
    return {
        "ok": not issues,
        "status": "ready" if not issues else "invalid_source",
        "schema_version": SCHEMA_VERSION,
        "live_state": str(live_path),
        "source": str(source_path),
        "source_sha256": _sha256(source_path),
        "projects": projects,
        "project_count": len(projects),
        "validation_issues": issues,
        "changed": changed,
        "codex_processes": processes,
        "can_apply_now": not issues and not processes,
        "preserved": [
            "state_5.sqlite",
            "sessions",
            "projectless-thread-ids",
            "thread-projectless-output-directories",
            "prompt-history",
            "unrelated Electron preferences",
        ],
        **details,
    }


def stage_sidebar_repair(
    *,
    codex_home: str | Path | None = None,
    source: str | Path | None = None,
) -> dict[str, Any]:
    preview = preview_sidebar_repair(codex_home=codex_home, source=source)
    if not preview["ok"]:
        raise SidebarRepairError("sidebar repair source failed validation: " + "; ".join(preview["validation_issues"]))
    descriptor = {
        "schema_version": SCHEMA_VERSION,
        "kind": "agent_bridge_pending_sidebar_repair",
        "staged_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "live_state": preview["live_state"],
        "live_sha256": _sha256(Path(str(preview["live_state"]))),
        "source": preview["source"],
        "source_sha256": preview["source_sha256"],
        "project_count": preview["project_count"],
        "requires_codex_closed": True,
    }
    path = _pending_path()
    _atomic_write(path, _json_bytes(descriptor))
    return {"ok": True, "status": "staged", "pending": str(path), "can_apply_now": preview["can_apply_now"], **descriptor}


def _load_pending() -> dict[str, Any]:
    path = _pending_path()
    if not path.is_file():
        raise SidebarRepairError(f"no staged sidebar repair exists: {path}")
    value = _read_required_json(path)
    if value.get("kind") != "agent_bridge_pending_sidebar_repair" or value.get("schema_version") != SCHEMA_VERSION:
        raise SidebarRepairError(f"unrecognized pending sidebar repair descriptor: {path}")
    return value


def apply_sidebar_repair(
    *,
    codex_home: str | Path | None = None,
    source: str | Path | None = None,
    expected_source_sha256: str | None = None,
    expected_live_sha256: str | None = None,
) -> dict[str, Any]:
    processes = _codex_processes()
    if processes:
        names = ", ".join(f"{row['name']}:{row['pid']}" for row in processes)
        raise SidebarRepairError(f"close Codex before applying sidebar repair; active process(es): {names}")
    home = _codex_home(codex_home)
    live_path = _global_state_path(home)
    source_path = _find_source(home, source)
    source_hash = _sha256(source_path)
    if expected_source_sha256 and source_hash != expected_source_sha256:
        raise SidebarRepairError("sidebar repair source changed after staging; refusing to apply")
    live_hash = _sha256(live_path)
    if expected_live_sha256 and live_hash != expected_live_sha256:
        raise SidebarRepairError(
            "live Codex sidebar state changed after staging; restage after Codex is fully closed"
        )
    live = _read_required_json(live_path)
    known_good = _read_required_json(source_path)
    projects, issues = _source_projects(known_good)
    if issues:
        raise SidebarRepairError("sidebar repair source failed validation: " + "; ".join(issues))
    repaired, details = _repair_projection(live, known_good)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = home / "backups" / f"sidebar-pointer-repair-{stamp}"
    backup_root.mkdir(parents=True, exist_ok=False)
    companion_path = live_path.with_name(f"{live_path.name}.bak")
    targets = [live_path, companion_path]
    backup_paths: dict[Path, Path] = {}
    existed: dict[Path, bool] = {}
    manifest_rows: list[dict[str, Any]] = []
    for target in targets:
        target_existed = target.is_file()
        existed[target] = target_existed
        backup_path = backup_root / target.name
        backup_paths[target] = backup_path
        if target_existed:
            shutil.copy2(target, backup_path)
        manifest_rows.append(
            {
                "target": str(target),
                "existed": target_existed,
                "backup": str(backup_path) if target_existed else None,
                "sha256": _sha256(backup_path) if target_existed else None,
            }
        )
    manifest_path = backup_root / "manifest.json"
    _atomic_write(
        manifest_path,
        _json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "files": manifest_rows,
            }
        ),
    )
    repaired_body = _json_bytes(repaired)
    try:
        for target in targets:
            _atomic_write(target, repaired_body)
        # Re-open both atomically replaced files before declaring success. Codex
        # maintains the companion .bak file and may restore it at startup.
        for target in targets:
            verified = _read_required_json(target)
            verified_projects, verified_issues = _source_projects(verified)
            if (
                verified_issues
                or [row["project_id"] for row in verified_projects]
                != [row["project_id"] for row in projects]
                or _json_bytes(verified) != repaired_body
            ):
                raise SidebarRepairError(f"post-write validation failed for {target}")
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in targets:
            try:
                if existed[target]:
                    _atomic_write(target, backup_paths[target].read_bytes())
                else:
                    target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        if rollback_errors:
            raise SidebarRepairError(
                f"post-write validation failed and rollback failed; recover from {backup_root}: "
                + "; ".join(rollback_errors)
            ) from exc
        raise SidebarRepairError(
            f"post-write validation failed; original global state was restored from {backup_root}"
        ) from exc
    return {
        "ok": True,
        "status": "applied",
        "live_state": str(live_path),
        "source": str(source_path),
        "source_sha256": source_hash,
        "live_sha256_before": live_hash,
        "backup": str(backup_paths[live_path]),
        "backups": {str(target): str(backup_paths[target]) if existed[target] else None for target in targets},
        "backup_manifest": str(manifest_path),
        "updated_state_files": [str(target) for target in targets],
        "project_count": len(projects),
        **details,
    }


def apply_pending_sidebar_repair() -> dict[str, Any]:
    descriptor = _load_pending()
    result = apply_sidebar_repair(
        codex_home=Path(str(descriptor["live_state"])).parent,
        source=str(descriptor["source"]),
        expected_source_sha256=str(descriptor["source_sha256"]),
        expected_live_sha256=str(descriptor["live_sha256"]) if descriptor.get("live_sha256") else None,
    )
    _pending_path().unlink(missing_ok=True)
    result["pending_removed"] = str(_pending_path())
    return result


def sidebar_repair_status() -> dict[str, Any]:
    path = _pending_path()
    pending: dict[str, Any] | None = None
    if path.is_file():
        try:
            pending = _read_required_json(path)
        except SidebarRepairError as exc:
            pending = {"status": "invalid", "detail": str(exc)}
    processes = _codex_processes()
    return {
        "ok": True,
        "status": "staged" if pending else "not_staged",
        "pending_path": str(path),
        "pending": pending,
        "codex_processes": processes,
        "can_apply_now": bool(pending) and not processes,
    }


def sidebar_repair_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code sidebar-repair",
        description="Preview, stage, or apply an offline Windows Codex project-sidebar or path repair.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "stage"):
        command = sub.add_parser(name)
        command.add_argument("--codex-home")
        command.add_argument("--source")
        command.add_argument(
            "--remap-windows",
            action="store_true",
            help="derive a Windows-native projection from live imported paths instead of restoring a backup",
        )
        command.add_argument("--project-registry")
        command.add_argument("--windows-home")
    apply = sub.add_parser("apply")
    apply.add_argument("--codex-home")
    apply.add_argument("--source")
    apply_remap = sub.add_parser(
        "apply-remap",
        help="build and apply a fresh Windows path projection while Codex is closed",
    )
    apply_remap.add_argument("--codex-home")
    apply_remap.add_argument("--project-registry")
    apply_remap.add_argument("--windows-home")
    sub.add_parser("status")
    sub.add_parser("apply-pending")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SidebarRepairError("sidebar repair is only supported from Windows-native Agent Bridge")
    if args.command == "preview":
        if args.remap_windows:
            if args.source:
                raise SidebarRepairError("--source cannot be combined with --remap-windows")
            result = preview_windows_path_repair(
                codex_home=args.codex_home,
                project_registry=args.project_registry,
                windows_home=args.windows_home,
            )
        else:
            result = preview_sidebar_repair(codex_home=args.codex_home, source=args.source)
    elif args.command == "stage":
        if args.remap_windows:
            if args.source:
                raise SidebarRepairError("--source cannot be combined with --remap-windows")
            result = stage_windows_path_repair(
                codex_home=args.codex_home,
                project_registry=args.project_registry,
                windows_home=args.windows_home,
            )
        else:
            result = stage_sidebar_repair(codex_home=args.codex_home, source=args.source)
    elif args.command == "apply":
        if _pending_path().is_file():
            raise SidebarRepairError(
                "a staged sidebar repair exists; use apply-pending, or apply-remap for a fresh offline projection"
            )
        result = apply_sidebar_repair(codex_home=args.codex_home, source=args.source)
    elif args.command == "apply-remap":
        result = apply_windows_path_repair(
            codex_home=args.codex_home,
            project_registry=args.project_registry,
            windows_home=args.windows_home,
        )
    elif args.command == "status":
        result = sidebar_repair_status()
    else:
        result = apply_pending_sidebar_repair()
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(sidebar_repair_cmd(sys.argv[1:]))
