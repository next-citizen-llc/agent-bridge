"""Safe, cached, fast-forward-only refresh of the canonical Agent Bridge checkout."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable


SCHEMA_VERSION = "1.0"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_EXPECTED_REMOTE = "https://github.com/next-citizen-llc/agent-bridge.git"


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def state_dir() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")).expanduser()


def update_state_path() -> Path:
    return state_dir() / "update" / "status.json"


def load_update_state() -> dict[str, Any]:
    try:
        data = json.loads(update_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def _run(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(1, timeout),
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def _git(repo: Path, args: list[str], *, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str]:
    return _run(["git", "-C", str(repo), *args], cwd=repo, timeout=timeout, env=env)


def _git_value(repo: Path, args: list[str], *, timeout: int) -> str:
    rc, output = _git(repo, args, timeout=timeout)
    return output.strip() if rc == 0 else ""


def bridge_revision(repo: Path) -> str:
    return _git_value(repo, ["rev-parse", "HEAD"], timeout=3)


def _remote_matches(actual: str, expected: str) -> bool:
    def normalize(value: str) -> str:
        text = value.strip().rstrip("/")
        if text.endswith(".git"):
            text = text[:-4]
        if text.startswith("git@github.com:"):
            text = "https://github.com/" + text.split(":", 1)[1]
        if text.startswith("ssh://git@github.com/"):
            text = "https://github.com/" + text.split("github.com/", 1)[1]
        return text.lower()

    return bool(actual) and normalize(actual) == normalize(expected)


def _lock_dir() -> Path:
    return state_dir() / "update" / "update.lock"


def _acquire_lock(*, stale_seconds: int = 300) -> bool:
    path = _lock_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir()
        (path / "owner.json").write_text(
            json.dumps({"pid": os.getpid(), "created_at": iso_now()}) + "\n",
            encoding="utf-8",
        )
        return True
    except FileExistsError:
        try:
            age = dt.datetime.now().timestamp() - path.stat().st_mtime
        except OSError:
            return False
        if age <= stale_seconds:
            return False
        shutil.rmtree(path, ignore_errors=True)
        try:
            path.mkdir()
            return True
        except FileExistsError:
            return False


def _release_lock() -> None:
    shutil.rmtree(_lock_dir(), ignore_errors=True)


def _base_result(repo: Path, action: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "agent-bridge.update",
        "action": action,
        "checked_at": iso_now(),
        "repo": str(repo),
        "status": "unknown",
        "changed": False,
        "installed": False,
        "local_revision": "",
        "remote_revision": "",
        "deployed_revision": str(load_update_state().get("deployed_revision", "")),
        "error_class": "",
        "detail": "",
    }


def _write_result(result: dict[str, Any]) -> dict[str, Any]:
    _atomic_json(update_state_path(), result)
    return result


def _cached_result(repo: Path, *, interval_seconds: int, force: bool) -> dict[str, Any] | None:
    if force or interval_seconds <= 0:
        return None
    previous = load_update_state()
    checked = parse_iso(previous.get("checked_at"))
    if checked is None:
        return None
    age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
    head = bridge_revision(repo)
    if age > interval_seconds or not head or head != previous.get("local_revision"):
        return None
    tracking = _git_value(repo, ["rev-parse", "refs/remotes/origin/main"], timeout=3)
    previous_status = previous.get("status")
    current = (
        previous_status in {"current", "updated", "current_cached"}
        and head == tracking
        and head == previous.get("deployed_revision")
    )
    offline = (
        previous_status in {"offline", "offline_cached"}
        and head == previous.get("deployed_revision")
    )
    if current or offline:
        result = dict(previous)
        result.update(
            {
                "action": "apply",
                "status": "current_cached" if current else "offline_cached",
                "changed": False,
                "cache_age_seconds": int(age),
            }
        )
        return result
    return None


def _refresh_installation(repo: Path, *, timeout: int) -> tuple[bool, str]:
    env = dict(os.environ)
    env["AGENT_BRIDGE_DISABLE_AUTO_UPDATE"] = "1"
    env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    compile_files = [str(path) for path in sorted((repo / "agent_bridge").glob("*.py"))]
    steps: list[list[str]] = [[sys.executable, "-m", "py_compile", *compile_files]]
    if os.name == "nt":
        powershell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        steps.append([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(repo / "scripts/install.ps1"), "-SkipPathUpdate"])
    else:
        steps.append([str(repo / "scripts/install.sh")])
    steps.append([sys.executable, "-m", "agent_bridge.cli", "code", "harness", "install-skill", "--link-client", "all"])
    for command in steps:
        rc, _ = _run(command, cwd=repo, timeout=timeout, env=env)
        if rc != 0:
            return False, f"installation step failed ({Path(command[0]).name}, exit {rc})"
    return True, "compiled and refreshed launcher, hooks, wrappers, and shared skill"


def update_bridge(
    repo: Path,
    *,
    action: str = "apply",
    force: bool = False,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    expected_remote: str = DEFAULT_EXPECTED_REMOTE,
    refresh: Callable[[Path, int], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Check or fast-forward the canonical checkout and refresh local integrations."""
    repo = repo.expanduser().resolve()
    if action not in {"status", "check", "apply"}:
        raise ValueError("action must be status, check, or apply")
    result = _base_result(repo, action)
    if os.environ.get("AGENT_BRIDGE_DISABLE_AUTO_UPDATE") in {"1", "true", "TRUE", "yes"} and action == "apply":
        result.update({"status": "disabled", "detail": "automatic update disabled by environment"})
        return result
    if not (repo / ".git").exists():
        result.update({"status": "blocked", "error_class": "config_missing", "detail": "canonical checkout is not a git repository"})
        return _write_result(result)
    local = bridge_revision(repo)
    result["local_revision"] = local
    result["remote_revision"] = _git_value(repo, ["rev-parse", "refs/remotes/origin/main"], timeout=3)
    if action == "status":
        current = bool(local and local == result["remote_revision"])
        deployed = bool(local and local == result.get("deployed_revision"))
        result.update({"status": "current" if current and deployed else "stale", "detail": "local status only; no network request"})
        return result
    if not _acquire_lock():
        result.update({"status": "busy", "error_class": "unknown", "detail": "another update is already running"})
        return result
    try:
        branch = _git_value(repo, ["branch", "--show-current"], timeout=3)
        result["branch"] = branch
        if branch != "main":
            result.update({"status": "blocked_branch", "error_class": "config_missing", "detail": "automatic updates require the canonical main branch"})
            return _write_result(result)
        rc, dirty = _git(repo, ["status", "--porcelain"], timeout=3)
        if rc != 0 or dirty:
            result.update({"status": "blocked_dirty", "error_class": "permission_denied", "detail": "checkout has local changes; update was not attempted"})
            return _write_result(result)
        remote = _git_value(repo, ["remote", "get-url", "origin"], timeout=3)
        result["remote"] = remote
        if not _remote_matches(remote, expected_remote):
            result.update({"status": "blocked_remote", "error_class": "permission_denied", "detail": "origin does not match the configured canonical repository"})
            return _write_result(result)
        if action == "apply":
            cached = _cached_result(repo, interval_seconds=interval_seconds, force=force)
            if cached is not None:
                return cached
        fetch_env = dict(os.environ)
        fetch_env["GIT_TERMINAL_PROMPT"] = "0"
        rc, _ = _git(repo, ["fetch", "--prune", "origin", "main"], timeout=timeout, env=fetch_env)
        if rc != 0:
            result.update({"status": "offline", "error_class": "network_unreachable", "detail": f"git fetch failed or timed out (exit {rc}); continuing with the installed revision"})
            return _write_result(result)
        remote_revision = _git_value(repo, ["rev-parse", "refs/remotes/origin/main"], timeout=3)
        result["remote_revision"] = remote_revision
        if not remote_revision:
            result.update({"status": "blocked", "error_class": "source_unreachable", "detail": "origin/main could not be resolved after fetch"})
            return _write_result(result)
        if local != remote_revision:
            rc_ancestor, _ = _git(repo, ["merge-base", "--is-ancestor", local, remote_revision], timeout=3)
            rc_remote_ancestor, _ = _git(repo, ["merge-base", "--is-ancestor", remote_revision, local], timeout=3)
            if rc_ancestor != 0:
                status = "blocked_ahead" if rc_remote_ancestor == 0 else "blocked_diverged"
                result.update({"status": status, "error_class": "permission_denied", "detail": "local history is not a fast-forward of origin/main"})
                return _write_result(result)
            if action == "check":
                result.update({"status": "update_available", "detail": "origin/main is ahead; no files were changed"})
                return _write_result(result)
            rc, _ = _git(repo, ["merge", "--ff-only", "refs/remotes/origin/main"], timeout=timeout)
            if rc != 0:
                result.update({"status": "blocked", "error_class": "permission_denied", "detail": f"fast-forward merge failed (exit {rc})"})
                return _write_result(result)
            result["changed"] = True
            local = bridge_revision(repo)
            result["local_revision"] = local
        elif action == "check":
            result.update({"status": "current", "detail": "checkout matches origin/main"})
            return _write_result(result)
        needs_install = result["changed"] or result.get("deployed_revision") != local
        if needs_install:
            install = refresh or (lambda path, seconds: _refresh_installation(path, timeout=seconds))
            ok, detail = install(repo, timeout)
            if not ok:
                result.update({"status": "build_failed", "error_class": "config_missing", "detail": detail, "reexec_required": bool(result["changed"])})
                return _write_result(result)
            result.update({"installed": True, "deployed_revision": local, "detail": detail})
        else:
            result["detail"] = "checkout and installed integrations already match origin/main"
        result.update({"status": "updated" if result["changed"] else "current", "reexec_required": bool(result["changed"])})
        return _write_result(result)
    finally:
        _release_lock()


def format_update(result: dict[str, Any]) -> str:
    local = str(result.get("local_revision", ""))[:12] or "unknown"
    remote = str(result.get("remote_revision", ""))[:12] or "unknown"
    return (
        f"Agent Bridge update: {result.get('status', 'unknown')}\n"
        f"Local/remote: {local}/{remote}\n"
        f"Changed: {'yes' if result.get('changed') else 'no'}\n"
        f"Installed: {'yes' if result.get('installed') else 'no'}\n"
        f"Detail: {result.get('detail', '')}\n"
    )
