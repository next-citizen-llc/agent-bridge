"""Manifest-driven removal of explicitly retired skill installations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from typing import Any


MANIFEST = Path(__file__).with_name("retired_skills.json")
_NAME_PATTERN = re.compile(r"^name:\s*['\"]?([^'\"\s]+)", re.MULTILINE)


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _root(name: str) -> Path | None:
    if name == "codex_plugin_cache":
        return _codex_home() / "plugins" / "cache"
    return None


def _load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("retirements"), list):
        raise ValueError("retired skill manifest must use schema_version 1 and a retirements list")
    return payload


def _skill_name(path: Path) -> str | None:
    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        return None
    match = _NAME_PATTERN.search(skill_file.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _empty_tree(path: Path) -> bool:
    return path.is_dir() and not any(item.is_file() or item.is_symlink() for item in path.rglob("*"))


def purge_retired_skills(*, manifest_path: Path = MANIFEST) -> dict[str, Any]:
    """Delete only manifest-listed skill roots after root and identity validation."""
    if os.environ.get("AGENT_BRIDGE_DISABLE_SKILL_PURGE") == "1":
        return {"status": "disabled", "purged": [], "skipped": [], "errors": []}

    report: dict[str, Any] = {"status": "ok", "purged": [], "skipped": [], "errors": []}
    try:
        retirements = _load_manifest(manifest_path)["retirements"]
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "purged": [], "skipped": [], "errors": [str(exc)]}

    for retirement in retirements:
        retirement_id = str(retirement.get("id", "unknown"))
        root = _root(str(retirement.get("root", "")))
        parts = retirement.get("path")
        expected_name = str(retirement.get("skill_name", ""))
        if root is None or not isinstance(parts, list) or not parts or not expected_name:
            report["errors"].append(f"{retirement_id}: invalid retirement entry")
            continue
        pattern = root.joinpath(*(str(part) for part in parts))
        candidates = sorted(root.glob(str(Path(*map(str, parts))))) if root.exists() else []
        if not candidates:
            report["skipped"].append({"id": retirement_id, "path": str(pattern), "reason": "absent"})
            continue
        for candidate in candidates:
            if not _within(candidate, root):
                report["errors"].append(f"{retirement_id}: path escaped allowed root: {candidate}")
                continue
            observed_name = _skill_name(candidate)
            if observed_name != expected_name and not _empty_tree(candidate):
                report["errors"].append(
                    f"{retirement_id}: expected skill {expected_name!r} at {candidate}, found {observed_name!r}"
                )
                continue
            try:
                if candidate.is_symlink():
                    candidate.unlink()
                else:
                    shutil.rmtree(candidate)
                report["purged"].append({"id": retirement_id, "path": str(candidate)})
            except OSError as exc:
                report["errors"].append(f"{retirement_id}: purge failed for {candidate}: {exc}")
    if report["errors"]:
        report["status"] = "degraded"
    return report
