"""Drift detection for repos an operator tracks alongside Agent Bridge.

Two distinct failure modes matter here, and only the first is a sync problem:

1. A checkout is behind ``origin`` and nobody noticed. Repos declared ``apply``
   are fast-forwarded through the same hardened updater the bridge uses.
2. Work exists only as uncommitted local edits, so it is not on any remote and
   no amount of pulling would ever retrieve it on another machine. That is
   invisible to a sync check, so it is reported separately.

Repos that routinely carry dirty working trees (generated output, build
artifacts) are declared ``report`` and are never mutated.

Which repos a machine tracks is declared in a local config file outside this
repository. Agent Bridge ships with an empty registry and never embeds the
identity, location, or contents of the repos an operator points it at.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

from .updater import (
    _atomic_json,
    _git,
    _git_value,
    _remote_matches,
    iso_now,
    parse_iso,
    safe_slug,
    state_dir,
    update_bridge,
)


SCHEMA_VERSION = "1.0"
# Managed repos are checked far less often than the bridge: they do not gate
# startup correctness, and a fetch per repo per session is wasted latency.
DEFAULT_MANAGED_INTERVAL_SECONDS = 3600
DEFAULT_MANAGED_TIMEOUT_SECONDS = 10

# Statuses that mean "a human needs to look at this", in descending severity.
WARNING_STATUSES = (
    "blocked_diverged",
    "blocked_ahead",
    "uncommitted_canonical",
    "behind",
    "blocked_dirty",
    "blocked_remote",
    "blocked_branch",
    "dirty",
    "not_git",
    "error",
)

# Statuses where drift was never actually determined. These must never be folded
# into "all current": an unchecked repo is not a clean repo.
INDETERMINATE_STATUSES = ("busy", "offline", "disabled", "unknown")


def _unsettled(status: str) -> bool:
    return status in WARNING_STATUSES or status in INDETERMINATE_STATUSES

# Ships empty on purpose. Which repos a machine tracks is local operator
# configuration, not a property of Agent Bridge, so no repo names, clone URLs,
# or filesystem paths are baked into this tool. With no config file present the
# sweep is inert and silent.
DEFAULT_REGISTRY: list[dict[str, Any]] = []

# Written to config_path() to declare repos on a machine:
#
#   {"repos": [
#     {
#       "id": "example-docs",             # required; also the state-file key
#       "label": "example docs",          # optional; defaults to id
#       "path": "~/Code/example-docs",    # required
#       "expected_remote": "https://github.com/example/example-docs.git",
#       "branch": "main",                 # optional; defaults to main
#       "mode": "apply",                  # apply = ff-only sync, report = read-only
#       "canonical_paths": ["src"]        # optional; uncommitted work here is called out
#     }
#   ]}
#
# Use "apply" only for a repo whose tree is normally clean. A repo that carries
# generated output is routinely dirty and would sit permanently in blocked_dirty;
# declare those "report".
CONFIG_EXAMPLE = {
    "repos": [
        {
            "id": "example-docs",
            "label": "example docs",
            "path": "~/Code/example-docs",
            "expected_remote": "https://github.com/example/example-docs.git",
            "branch": "main",
            "mode": "report",
            "canonical_paths": ["src"],
        }
    ]
}


def config_path() -> Path:
    """Machine-local registry. Repo identities never live in the tool itself."""
    return state_dir() / "managed-repos.json"


def managed_disabled() -> bool:
    return os.environ.get("AGENT_BRIDGE_DISABLE_MANAGED_REPOS", "").lower() in {"1", "true", "yes"}


def load_registry() -> list[dict[str, Any]]:
    """Registry from the machine-local config file, else the built-in defaults."""
    entries: list[dict[str, Any]] = DEFAULT_REGISTRY
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict) and isinstance(raw.get("repos"), list):
        entries = [item for item in raw["repos"] if isinstance(item, dict) and item.get("id")]
    elif isinstance(raw, list):
        entries = [item for item in raw if isinstance(item, dict) and item.get("id")]

    resolved: list[dict[str, Any]] = []
    for entry in entries:
        merged = dict(entry)
        override = os.environ.get(f"AGENT_BRIDGE_MANAGED_{safe_slug(str(entry['id'])).upper().replace('-', '_')}_PATH")
        if override:
            merged["path"] = override
        merged.setdefault("label", str(entry["id"]))
        merged.setdefault("branch", "main")
        merged.setdefault("mode", "report")
        merged.setdefault("canonical_paths", [])
        resolved.append(merged)
    return resolved


def managed_state_path(repo_id: str) -> Path:
    return state_dir() / "update" / f"managed-{safe_slug(repo_id)}.json"


def load_managed_state(repo_id: str) -> dict[str, Any]:
    try:
        data = json.loads(managed_state_path(repo_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _counts(repo: Path, branch: str, *, timeout: int) -> tuple[int, int]:
    """Commits (behind, ahead) relative to origin/<branch>."""
    value = _git_value(repo, ["rev-list", "--left-right", "--count", f"refs/remotes/origin/{branch}...HEAD"], timeout=timeout)
    parts = value.split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _porcelain(repo: Path, paths: list[str], *, timeout: int) -> list[str]:
    args = ["status", "--porcelain"]
    if paths:
        args += ["--", *paths]
    rc, out = _git(repo, args, timeout=timeout)
    if rc != 0 or not out:
        return []
    return [line for line in out.splitlines() if line.strip()]


def check_managed_repo(
    entry: dict[str, Any],
    *,
    timeout: int = DEFAULT_MANAGED_TIMEOUT_SECONDS,
    interval_seconds: int = DEFAULT_MANAGED_INTERVAL_SECONDS,
    force: bool = False,
) -> dict[str, Any]:
    repo_id = str(entry["id"])
    branch_name = str(entry.get("branch", "main"))
    mode = str(entry.get("mode", "report"))
    canonical_paths = [str(p) for p in entry.get("canonical_paths", [])]
    repo = Path(str(entry["path"])).expanduser()

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "agent-bridge.managed-repo",
        "id": repo_id,
        "label": str(entry.get("label", repo_id)),
        "mode": mode,
        "repo": str(repo),
        "checked_at": iso_now(),
        "status": "unknown",
        "behind": 0,
        "ahead": 0,
        "dirty_files": 0,
        "uncommitted_canonical": [],
        "changed": False,
        "detail": "",
    }

    if not repo.exists():
        # Expected on machines that do not hold this repo; not an error.
        result.update({"status": "absent", "detail": "checkout not present on this machine"})
        return result
    if not (repo / ".git").exists():
        result.update({"status": "not_git", "detail": "path exists but is not a git checkout"})
        return _write_managed(result)

    cached = _cached_managed(repo, repo_id, interval_seconds=interval_seconds, force=force)
    if cached is not None:
        return cached

    if mode == "apply":
        # Reuse the bridge's hardened path: ff-only, clean-only, remote-verified.
        # refresh is a no-op because a managed repo has no installation step.
        update = update_bridge(
            repo,
            action="apply",
            force=True,
            interval_seconds=0,
            timeout=timeout,
            expected_remote=str(entry.get("expected_remote", "")),
            slug=repo_id,
            branch_name=branch_name,
            refresh=lambda _repo, _timeout: (True, "no installation step for a managed repo"),
        )
        result["changed"] = bool(update.get("changed"))
        result["detail"] = str(update.get("detail", ""))
        mapped = {
            "updated": "updated",
            "current": "current",
            "current_cached": "current",
            "offline": "offline",
            "offline_cached": "offline",
            "disabled": "disabled",
            "busy": "busy",
        }
        result["status"] = mapped.get(str(update.get("status")), str(update.get("status", "unknown")))
        if result["status"] == "blocked_dirty":
            # update_bridge rejects a dirty tree before it fetches, so the
            # remote-tracking ref is stale and the behind count would read 0.
            # Without this read-only fetch a dirty repo that is also badly
            # behind reports as merely "dirty" and the staleness disappears.
            fetch_env = dict(os.environ)
            fetch_env["GIT_TERMINAL_PROMPT"] = "0"
            _git(repo, ["fetch", "--prune", "origin", branch_name], timeout=timeout, env=fetch_env)
    else:
        remote = _git_value(repo, ["remote", "get-url", "origin"], timeout=3)
        expected = str(entry.get("expected_remote", ""))
        if expected and not _remote_matches(remote, expected):
            result.update({"status": "blocked_remote", "detail": "origin does not match the configured canonical repository"})
            return _write_managed(result)
        fetch_env = dict(os.environ)
        fetch_env["GIT_TERMINAL_PROMPT"] = "0"
        rc, _ = _git(repo, ["fetch", "--prune", "origin", branch_name], timeout=timeout, env=fetch_env)
        if rc != 0:
            result.update({"status": "offline", "detail": f"git fetch failed or timed out (exit {rc})"})
            return _write_managed(result)
        result["status"] = "current"
        result["detail"] = "read-only check; this repo is never modified automatically"

    result["local_revision"] = _git_value(repo, ["rev-parse", "HEAD"], timeout=3)
    behind, ahead = _counts(repo, branch_name, timeout=3)
    result["behind"], result["ahead"] = behind, ahead
    dirty = _porcelain(repo, [], timeout=3)
    result["dirty_files"] = len(dirty)
    result["uncommitted_canonical"] = _porcelain(repo, canonical_paths, timeout=3) if canonical_paths else []

    # Severity ordering: history divergence first, then unpushed canonical work,
    # then plain staleness. A cosmetically dirty tree is the weakest signal.
    if result["status"] not in {"offline", "disabled", "busy", "blocked_remote", "blocked_branch"}:
        if behind and ahead:
            result["status"] = "blocked_diverged"
        elif ahead:
            result["status"] = "blocked_ahead"
        elif result["uncommitted_canonical"]:
            result["status"] = "uncommitted_canonical"
        elif behind:
            result["status"] = "behind" if mode == "report" else "blocked_dirty"
        elif result["changed"]:
            result["status"] = "updated"
        elif dirty:
            result["status"] = "dirty"
        else:
            result["status"] = "current"
    return _write_managed(result)


def _write_managed(result: dict[str, Any]) -> dict[str, Any]:
    _atomic_json(managed_state_path(str(result["id"])), result)
    return result


def _cached_managed(repo: Path, repo_id: str, *, interval_seconds: int, force: bool) -> dict[str, Any] | None:
    if force or interval_seconds <= 0:
        return None
    previous = load_managed_state(repo_id)
    checked = parse_iso(previous.get("checked_at"))
    if checked is None:
        return None
    age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
    if age > interval_seconds:
        return None
    # Never let a cache hide a state that needs attention or was never
    # determined, and never trust a cache once HEAD has moved.
    if _unsettled(str(previous.get("status", "unknown"))):
        return None
    head = _git_value(repo, ["rev-parse", "HEAD"], timeout=3)
    if not head or head != previous.get("local_revision"):
        return None
    result = dict(previous)
    result.update({"status": str(previous.get("status", "current")), "cached": True, "cache_age_seconds": int(age), "changed": False})
    return result


def sync_managed_repos(
    *,
    timeout: int = DEFAULT_MANAGED_TIMEOUT_SECONDS,
    interval_seconds: int = DEFAULT_MANAGED_INTERVAL_SECONDS,
    force: bool = False,
    only: list[str] | None = None,
) -> list[dict[str, Any]]:
    if managed_disabled():
        return []
    results: list[dict[str, Any]] = []
    for entry in load_registry():
        if only and str(entry["id"]) not in only:
            continue
        try:
            results.append(check_managed_repo(entry, timeout=timeout, interval_seconds=interval_seconds, force=force))
        except Exception as exc:  # a managed repo must never break startup
            results.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "agent-bridge.managed-repo",
                    "id": str(entry.get("id", "unknown")),
                    "label": str(entry.get("label", entry.get("id", "unknown"))),
                    "mode": str(entry.get("mode", "report")),
                    "repo": str(entry.get("path", "")),
                    "checked_at": iso_now(),
                    "status": "error",
                    "behind": 0,
                    "ahead": 0,
                    "dirty_files": 0,
                    "uncommitted_canonical": [],
                    "changed": False,
                    "detail": f"check failed safely: {type(exc).__name__}",
                }
            )
    return results


def describe_managed_repo(result: dict[str, Any]) -> str:
    """Every notable fact about a repo, not just its highest-severity status.

    A repo can be ahead AND holding uncommitted canonical work at once. Reporting
    only the worst status hides the rest, which is how drift stays invisible.
    """
    label = result.get("label", result.get("id", "repo"))
    status = str(result.get("status", "unknown"))
    behind = int(result.get("behind", 0) or 0)
    ahead = int(result.get("ahead", 0) or 0)
    canonical = result.get("uncommitted_canonical") or []
    dirty = int(result.get("dirty_files", 0) or 0)

    # Terminal states describe themselves; no drift counts are meaningful.
    terminal = {
        "blocked_remote": "origin does not match the expected canonical repository",
        "blocked_branch": "is not on its canonical branch",
        "not_git": "path is not a git checkout",
        "offline": "could not be reached, so drift is unverified; using the local revision",
        "busy": "was already being checked by another session, so drift is unverified",
        "disabled": "automatic checking is disabled, so drift is unverified",
        "unknown": "returned no determinate state",
        "error": str(result.get("detail") or "check failed safely"),
    }
    if status in terminal:
        return f"{label} {terminal[status]}"

    facts: list[str] = []
    if behind and ahead:
        facts.append(f"DIVERGED ({ahead} ahead, {behind} behind), not touched")
    elif ahead:
        facts.append(f"{ahead} commit(s) AHEAD of origin and unpushed")
    elif behind:
        if status == "blocked_dirty":
            facts.append(f"{behind} commit(s) behind but has local changes, so it was not updated")
        else:
            facts.append(f"{behind} commit(s) BEHIND origin (report-only; pull manually)")
    if canonical:
        facts.append(f"{len(canonical)} UNCOMMITTED canonical file(s) that exist on no remote")
    if status == "updated":
        facts.append("fast-forwarded to origin")
    if not facts and dirty:
        facts.append(f"{dirty} uncommitted file(s) outside canonical paths")
    return f"{label} has " + ", ".join(facts) if facts else ""


def format_managed_repos(results: list[dict[str, Any]]) -> str:
    """Startup-line text. Warnings are stated explicitly; silence means clean."""
    if not results:
        return ""
    present = [r for r in results if str(r.get("status")) != "absent"]
    if not present:
        return ""
    warnings = [r for r in present if str(r.get("status")) in WARNING_STATUSES]
    unverified = [r for r in present if str(r.get("status")) in INDETERMINATE_STATUSES]
    updated = [r for r in present if str(r.get("status")) == "updated"]

    if not warnings and not unverified:
        applied = f", {len(updated)} fast-forwarded" if updated else ""
        return f" Canonical repos: {len(present)} checked, all current{applied}."

    clauses = [describe_managed_repo(r) for r in (*warnings, *unverified)]
    clauses = [c for c in clauses if c]
    flagged = len(warnings) + len(unverified)
    text = f" Canonical repo ATTENTION ({flagged} of {len(present)}): " + "; ".join(clauses) + "."
    if updated:
        text += f" Fast-forwarded: {', '.join(str(r.get('label', r.get('id'))) for r in updated)}."
    return text + " Run `agent code repos status` for detail."
