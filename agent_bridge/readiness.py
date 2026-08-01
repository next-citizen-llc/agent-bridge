"""Local readiness probes, caches, shared-root resolution, and redacted publication."""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = "1.0"
READINESS_STATES = ("ready", "degraded", "blocked", "unknown")
ERROR_CLASSES = (
    "auth_missing",
    "auth_expired",
    "auth_wrong_identity",
    "mcp_unauthed",
    "network_unreachable",
    "dns_failure",
    "permission_denied",
    "config_missing",
    "context_stale",
    "source_unreachable",
    "unknown",
)
SENSITIVE_DETAIL_RE = re.compile(
    r"(?i)(authorization\s*:\s*(?:(?:bearer|basic)\s+)?\S+|(?:token|password|secret|cookie|api[_-]?key)\s*[=:]\s*\S+|gh[pousr]_[A-Za-z0-9_]+)"
)


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


def roots_config_path() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_ROOTS_CONFIG", Path.home() / ".config/agent-bridge/roots.json")).expanduser()


def readiness_config_path() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_READINESS_CONFIG", Path.home() / ".config/agent-bridge/readiness.json")).expanduser()


def load_readiness_config() -> dict[str, Any]:
    try:
        data = json.loads(readiness_config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def configure_readiness(values: dict[str, Any]) -> dict[str, Any]:
    current = load_readiness_config()
    current.update({key: value for key, value in values.items() if value not in {None, ""}})
    payload = {"schema_version": SCHEMA_VERSION, **current}
    _atomic_json(readiness_config_path(), payload)
    return {"config_file": str(readiness_config_path()), "settings": current}


def load_roots_config() -> dict[str, str]:
    path = roots_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {kind: str(data[kind]) for kind in ("skills", "data", "conversations") if data.get(kind)}


def configure_shared_roots(values: dict[str, str], *, path: Path | None = None) -> dict[str, Any]:
    target = path or roots_config_path()
    current = load_roots_config() if target == roots_config_path() else {}
    current.update({kind: value for kind, value in values.items() if kind in {"skills", "data", "conversations"} and value})
    payload = {"schema_version": SCHEMA_VERSION, **current}
    _atomic_json(target, payload)
    return {"config_file": str(target), "roots": current}


def machine_id() -> str:
    explicit = os.environ.get("AGENT_BRIDGE_MACHINE_ID")
    value = explicit or f"{getpass.getuser()}@{socket.gethostname()}"
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value).strip("_")[:80] or "machine"


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        key = str(expanded)
        if key not in seen:
            seen.add(key)
            result.append(expanded)
    return result


def _env_paths(*names: str) -> list[Path]:
    paths: list[Path] = []
    for name in names:
        value = os.environ.get(name)
        if not value:
            continue
        paths.extend(Path(part.strip()) for part in value.split(os.pathsep) if part.strip())
    return paths


def shared_root_candidates(kind: str) -> list[Path]:
    if kind not in {"skills", "data", "conversations"}:
        raise ValueError(f"unknown shared root kind {kind!r}")
    leaf = {
        "skills": "SharedAgentSkills",
        "data": "SharedAgentData",
        "conversations": "SharedAgentConversations",
    }[kind]
    env_names = {
        "skills": ("AGENT_BRIDGE_SHARED_SKILLS_ROOT", "SHARED_AGENT_SKILLS_ROOT", "CAREER_SHARED_SKILLS_ROOT"),
        "data": ("AGENT_BRIDGE_SHARED_DATA_ROOT", "SHARED_AGENT_DATA_ROOT"),
        "conversations": ("AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT", "SHARED_AGENT_CONVERSATIONS_ROOT"),
    }[kind]
    candidates = _env_paths(*env_names)
    configured = load_roots_config().get(kind)
    if configured:
        candidates.append(Path(configured))
    for variable in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / leaf)
    if candidates:
        return _dedupe(candidates)
    home = Path.home()
    cloud = home / "Library" / "CloudStorage"
    try:
        discovered_accounts = _bounded_call(
            lambda: sorted(path for path in cloud.glob("OneDrive-*") if path.is_dir()),
            timeout=0.5,
        )
    except (OSError, TimeoutError):
        discovered_accounts = []
    personal_accounts = [path for path in discovered_accounts if path.name == "OneDrive-Personal"]
    business_accounts = [path for path in discovered_accounts if path.name != "OneDrive-Personal"]
    preferred_accounts = (
        [*personal_accounts, *business_accounts]
        if kind in {"skills", "conversations"}
        else [*business_accounts, *personal_accounts]
    )
    candidates.extend(account / leaf for account in preferred_accounts)
    candidates.append(home / "OneDrive" / leaf)
    return _dedupe(candidates)


def resolve_shared_roots(*, create: bool = False) -> dict[str, Any]:
    roots: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    for kind in ("skills", "data", "conversations"):
        candidates = shared_root_candidates(kind)
        existing: list[Path] = []
        unavailable: list[Path] = []
        for path in candidates:
            try:
                if _bounded_call(path.exists, timeout=0.25):
                    existing.append(path)
            except (OSError, TimeoutError):
                unavailable.append(path)
        selected = existing[0] if existing else (candidates[0] if candidates else None)
        selected_exists = selected in existing if selected is not None else False
        if create and selected is not None:
            selected.mkdir(parents=True, exist_ok=True)
            selected_exists = True
        explicit = bool(_env_paths(*{
            "skills": ("AGENT_BRIDGE_SHARED_SKILLS_ROOT", "SHARED_AGENT_SKILLS_ROOT", "CAREER_SHARED_SKILLS_ROOT"),
            "data": ("AGENT_BRIDGE_SHARED_DATA_ROOT", "SHARED_AGENT_DATA_ROOT"),
            "conversations": ("AGENT_BRIDGE_SHARED_CONVERSATIONS_ROOT", "SHARED_AGENT_CONVERSATIONS_ROOT"),
        }[kind])) or bool(load_roots_config().get(kind))
        if len(existing) > 1 and not explicit:
            conflicts.append({"kind": kind, "paths": [str(path) for path in existing]})
        roots[kind] = {
            "selected": str(selected) if selected is not None else "",
            "exists": selected_exists,
            "explicit": explicit,
            "candidates": [str(path) for path in candidates],
            "existing": [str(path) for path in existing],
            "unavailable": [str(path) for path in unavailable],
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "roots": roots,
        "conflicts": conflicts,
        "ok": not conflicts and all(row["exists"] for row in roots.values()),
    }


def _safe_fragment(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "-" for char in value).strip("-") or "unknown"


def readiness_path(client: str, surface: str, scope: str) -> Path:
    return state_dir() / "readiness" / f"{_safe_fragment(machine_id())}.{_safe_fragment(client)}.{_safe_fragment(surface)}.{_safe_fragment(scope)}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)


def _atomic_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text(encoding="utf-8") == rendered:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o600)
    tmp.replace(path)
    return True


def _bounded_call(action: Any, *, timeout: float = 2.0) -> Any:
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = action()
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(max(0.01, timeout))
    if worker.is_alive():
        raise TimeoutError(f"shared-state I/O timed out after {timeout}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _bounded_read_text(path: Path, *, timeout: float = 0.5) -> str:
    return str(_bounded_call(lambda: path.read_text(encoding="utf-8"), timeout=timeout))


def _sanitize_detail(detail: str) -> str:
    return SENSITIVE_DETAIL_RE.sub("[redacted]", detail)[:1000]


def _classify_failure(text: str, *, default: str = "unknown") -> str:
    lowered = text.lower()
    if "could not resolve host" in lowered or "name or service not known" in lowered or "nodename nor servname" in lowered:
        return "dns_failure"
    if "timed out" in lowered or "network is unreachable" in lowered or "connection refused" in lowered:
        return "network_unreachable"
    if "permission denied" in lowered or "forbidden" in lowered:
        return "permission_denied"
    if "expired" in lowered:
        return "auth_expired"
    if any(
        phrase in lowered
        for phrase in (
            "not logged in",
            "login required",
            "please run /login",
            "please authenticate",
            "failed to authenticate",
            "invalid authentication credentials",
            "authentication required",
            "unauthorized",
        )
    ):
        return "auth_missing"
    return default


def _check(
    name: str,
    status: str,
    detail: str,
    *,
    required: bool = True,
    error_class: str = "",
    repair_command: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "required": required,
        "error_class": error_class,
        "detail": _sanitize_detail(detail),
        "repair_command": repair_command,
    }


def _run(command: list[str], *, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def _client_command(client: str) -> str:
    env_name = {
        "codex": "CODEX_BIN",
        "claude": "CLAUDE_BIN",
        "grok": "GROK_BIN",
        "agy": "AGY_BIN",
        "ollama": "OLLAMA_BIN",
    }.get(client, "")
    return os.environ.get(env_name, client) if env_name else client


def _binary_check(client: str) -> dict[str, Any]:
    command = _client_command(client)
    resolved = command if Path(command).expanduser().is_file() else shutil.which(command)
    if resolved:
        return _check("client_binary", "ready", resolved)
    return _check(
        "client_binary",
        "blocked",
        f"{command} is not on PATH",
        error_class="config_missing",
        repair_command=f"install {client} or set {client.upper()}_BIN",
    )


def _auth_check(client: str, *, project_dir: Path, timeout: int) -> dict[str, Any]:
    commands = {
        "codex": ["login", "status"],
        "claude": ["auth", "status"],
        "grok": ["models"],
        "agy": ["models"],
    }
    command = commands.get(client)
    if command is None:
        return _check("client_auth", "unknown", f"no authentication probe is defined for {client}", required=False)
    rc, output = _run([_client_command(client), *command], cwd=project_dir, timeout=timeout)
    if rc == 0:
        error_class = _classify_failure(output, default="")
        if error_class in {"auth_missing", "auth_expired", "permission_denied"}:
            return _check(
                "client_auth",
                "blocked",
                output or f"{client} reported an authentication failure",
                error_class=error_class,
                repair_command=f"agent code repair --to {client} --repair-auth",
            )
        detail = output.splitlines()[0] if output else f"{client} probe succeeded"
        return _check("client_auth", "ready", detail)
    error_class = _classify_failure(output, default="auth_missing")
    status = "degraded" if error_class in {"dns_failure", "network_unreachable", "source_unreachable"} else "blocked"
    return _check(
        "client_auth",
        status,
        output or f"probe exited {rc}",
        error_class=error_class,
        repair_command=f"agent code repair --to {client} --repair-auth" if status == "blocked" else "retry after network access is restored",
    )


def _github_check(*, project_dir: Path, timeout: int, expected_login: str = "", required: bool = False) -> dict[str, Any]:
    if not shutil.which("gh"):
        return _check("github_auth", "blocked", "gh is not on PATH", required=required, error_class="config_missing", repair_command="install gh and run gh auth login")
    rc, output = _run(["gh", "auth", "status", "--active", "-h", "github.com"], cwd=project_dir, timeout=timeout)
    if rc != 0:
        error_class = _classify_failure(output, default="auth_missing")
        status = "degraded" if error_class in {"dns_failure", "network_unreachable"} else "blocked"
        return _check("github_auth", status, output or f"gh auth status exited {rc}", required=required, error_class=error_class, repair_command="gh auth login -h github.com")
    if expected_login:
        user_rc, login = _run(["gh", "api", "user", "--jq", ".login"], cwd=project_dir, timeout=timeout)
        if user_rc != 0:
            error_class = _classify_failure(login, default="source_unreachable")
            status = "degraded" if error_class in {"dns_failure", "network_unreachable", "source_unreachable"} else "blocked"
            return _check("github_identity", status, login or f"gh api exited {user_rc}", required=required, error_class=error_class, repair_command="gh auth login -h github.com")
        if login.strip().lower() != expected_login.strip().lower():
            return _check(
                "github_identity",
                "blocked",
                f"authenticated GitHub login {login.strip()!r} does not match expected login {expected_login!r}",
                required=required,
                error_class="auth_wrong_identity",
                repair_command=f"gh auth switch -h github.com -u {expected_login}",
            )
        return _check("github_identity", "ready", f"authenticated as {login.strip()}", required=required)
    return _check("github_auth", "ready", "authenticated to github.com", required=required)


def _ollama_check(timeout: int) -> dict[str, Any]:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if "://" not in host:
        host = "http://" + host
    parsed = urlparse(host)
    hostname = parsed.hostname or ""
    try:
        loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        return _check(
            "ollama_api",
            "blocked",
            "OLLAMA_HOST must resolve to loopback for readiness probes",
            error_class="permission_denied",
            repair_command="set OLLAMA_HOST to http://127.0.0.1:11434",
        )
    url = host + "/api/version"
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _check("ollama_api", "ready", f"reachable ({payload.get('version', 'version unknown')})")
    except HTTPError as exc:
        return _check("ollama_api", "blocked", f"HTTP {exc.code}", error_class="source_unreachable", repair_command="ollama serve")
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return _check("ollama_api", "blocked", str(exc), error_class=_classify_failure(str(exc), default="source_unreachable"), repair_command="ollama serve")


def _mcp_check(client: str) -> dict[str, Any]:
    candidates = {
        "codex": [Path.home() / ".codex" / "config.toml"],
        "claude": [Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json"],
        "grok": [Path.home() / ".grok" / "config.toml", Path.home() / ".claude.json"],
    }.get(client, [])
    existing = [path for path in candidates if path.exists()]
    if not existing:
        return _check("mcp_config", "unknown", "no known MCP config file was found", required=False)
    configured = []
    for path in existing:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "mailbox_mcp.py" in text or "agent-bridge" in text.lower():
            configured.append(path)
    if configured:
        return _check("mcp_config", "ready", ", ".join(str(path) for path in configured), required=False)
    return _check("mcp_config", "degraded", "MCP config exists but no Agent Bridge mailbox registration was detected", required=False, error_class="config_missing")


def _mcp_health_checks(client: str, *, project_dir: Path, timeout: int) -> list[dict[str, Any]]:
    if client == "codex":
        rc, output = _run([_client_command(client), "mcp", "list", "--json"], cwd=project_dir, timeout=timeout)
        if rc != 0:
            return [_check("mcp_health", "degraded", output or f"codex mcp list exited {rc}", required=False, error_class=_classify_failure(output, default="source_unreachable"), repair_command="codex mcp list")]
        try:
            servers = json.loads(output)
        except json.JSONDecodeError:
            return [_check("mcp_health", "degraded", "codex mcp list returned invalid JSON", required=False, error_class="unknown")]
        rows = []
        for server in servers if isinstance(servers, list) else []:
            if not isinstance(server, dict) or not server.get("enabled", True):
                continue
            name = _safe_fragment(str(server.get("name", "unknown")))
            auth = str(server.get("auth_status", "unknown")).lower()
            transport = server.get("transport") if isinstance(server.get("transport"), dict) else {}
            transport_type = str(transport.get("type", "unknown"))
            command = str(transport.get("command", "")) if transport_type == "stdio" else ""
            command_available = True
            if command:
                expanded = Path(command).expanduser()
                command_available = expanded.is_file() if (expanded.is_absolute() or "/" in command or "\\" in command) else bool(shutil.which(command))
            if auth in {"not_logged_in", "expired"}:
                status = "degraded"
                error_class = "mcp_unauthed"
                detail = f"{transport_type}; auth status {auth}"
                repair = f"codex mcp login {name}"
            elif command and not command_available:
                status = "degraded"
                error_class = "source_unreachable"
                detail = f"stdio command is unavailable: {command}"
                repair = f"codex mcp get {name}"
            elif auth in {"logged_in", "authenticated"}:
                status = "ready"
                error_class = ""
                detail = f"{transport_type}; authenticated"
                repair = ""
            else:
                status = "unknown"
                error_class = "unknown"
                detail = f"{transport_type}; configured but live reachability is not probed"
                repair = f"codex mcp get {name}"
            rows.append(
                _check(
                    f"mcp:{name}",
                    status,
                    detail,
                    required=False,
                    error_class=error_class,
                    repair_command=repair,
                )
            )
        return rows or [_check("mcp_health", "unknown", "no enabled Codex MCP servers were reported", required=False)]
    if client == "claude":
        rc, output = _run([_client_command(client), "mcp", "list"], cwd=project_dir, timeout=timeout)
        if rc != 0:
            return [_check("mcp_health", "degraded", output or f"claude mcp list exited {rc}", required=False, error_class=_classify_failure(output, default="source_unreachable"), repair_command="claude mcp list")]
        rows = []
        for line in output.splitlines():
            if ":" not in line or ("Connected" not in line and "Failed" not in line):
                continue
            name = _safe_fragment(line.split(":", 1)[0].strip())
            healthy = "Connected" in line
            rows.append(_check(f"mcp:{name}", "ready" if healthy else "degraded", "connected" if healthy else "health check failed", required=False, error_class="source_unreachable" if not healthy else "", repair_command=f"claude mcp get {name}" if not healthy else ""))
        return rows or [_check("mcp_health", "unknown", "Claude returned no parseable MCP health rows", required=False)]
    if client == "grok":
        rc, output = _run([_client_command(client), "mcp", "doctor", "--json"], cwd=project_dir, timeout=timeout)
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return [_check("mcp_health", "degraded", output or f"grok mcp doctor exited {rc}", required=False, error_class=_classify_failure(output, default="source_unreachable"), repair_command="grok mcp doctor")]
        rows = []
        for server in payload.get("servers", []):
            if not isinstance(server, dict):
                continue
            name = _safe_fragment(str(server.get("name", "unknown")))
            healthy = bool(server.get("healthy"))
            source = str(server.get("source", "unknown"))
            source_id = _safe_fragment(source)
            rows.append(_check(f"mcp:{name}@{source_id}", "ready" if healthy else "degraded", f"source {source}; {'healthy' if healthy else 'health check failed'}", required=False, error_class="source_unreachable" if not healthy else "", repair_command="grok mcp doctor" if not healthy else ""))
        return rows or [_check("mcp_health", "unknown", "Grok returned no MCP health rows", required=False)]
    return [_check("mcp_health", "unknown", f"no live MCP health adapter is defined for {client}", required=False)]


def _gui_surface_checks(client: str) -> list[dict[str, Any]]:
    """Report GUI installation separately and never infer GUI auth from a CLI."""
    app_names = {"codex": "Codex", "claude": "Claude"}
    if client in app_names and sys.platform == "darwin":
        app = Path("/Applications") / f"{app_names[client]}.app"
        installed = app.exists()
        install = _check(
            "gui_install",
            "ready" if installed else "degraded",
            f"{app_names[client]} application {'is installed' if installed else 'was not found'}",
            required=False,
            error_class="" if installed else "config_missing",
            repair_command=f"open -a {app_names[client]}" if installed else f"install the {app_names[client]} application",
        )
    elif client == "grok":
        wrapper = Path.home() / ".local/bin/grok-gui-bridge"
        installed = wrapper.exists()
        install = _check(
            "gui_install",
            "ready" if installed else "degraded",
            f"Grok Edge wrapper {'is installed' if installed else 'was not found'}",
            required=False,
            error_class="" if installed else "config_missing",
            repair_command="agent code hooks install --client grok",
        )
    else:
        install = _check("gui_install", "unknown", f"no non-interactive GUI install probe is defined for {client}", required=False)
    auth_repair = "open -a 'Microsoft Edge' https://grok.com/" if client == "grok" else f"open -a {app_names.get(client, client)}"
    return [
        install,
        _check(
            "gui_auth",
            "unknown",
            "GUI authentication is surface-specific and cannot be inherited from CLI state",
            required=False,
            error_class="unknown",
            repair_command=auth_repair,
        ),
    ]


def validate_readiness_report(report: dict[str, Any]) -> list[str]:
    """Validate the stable readiness contract without a third-party schema runtime."""
    errors: list[str] = []
    required = {
        "schema_version": str,
        "kind": str,
        "scope": str,
        "generated_at": str,
        "expires_at": str,
        "machine_id": str,
        "client": str,
        "surface": str,
        "project_dir": str,
        "overall": str,
        "checks": list,
        "shared_roots": dict,
    }
    for key, expected_type in required.items():
        if not isinstance(report.get(key), expected_type):
            errors.append(f"{key} must be {expected_type.__name__}")
    if report.get("scope") not in {"session", "work"}:
        errors.append("scope must be session or work")
    if report.get("overall") not in READINESS_STATES:
        errors.append("overall is not a readiness state")
    for key in ("generated_at", "expires_at"):
        if parse_iso(report.get(key)) is None:
            errors.append(f"{key} must be a UTC timestamp")
    checks = report.get("checks", [])
    if isinstance(checks, list):
        for index, row in enumerate(checks):
            if not isinstance(row, dict):
                errors.append(f"checks[{index}] must be an object")
                continue
            for key in ("name", "status", "required", "error_class", "detail", "repair_command"):
                if key not in row:
                    errors.append(f"checks[{index}].{key} is required")
            if row.get("status") not in READINESS_STATES:
                errors.append(f"checks[{index}].status is invalid")
            if row.get("error_class") and row.get("error_class") not in ERROR_CLASSES:
                errors.append(f"checks[{index}].error_class is invalid")
    return errors


def _overall(checks: list[dict[str, Any]]) -> str:
    required = [row for row in checks if row["required"]]
    if any(row["status"] == "blocked" for row in required):
        return "blocked"
    if any(row["status"] in {"degraded", "unknown"} for row in required):
        return "degraded"
    if any(not row["required"] and row["status"] in {"blocked", "degraded"} for row in checks):
        return "degraded"
    return "ready"


def run_preflight(
    client: str,
    surface: str,
    *,
    scope: str,
    project_dir: Path,
    timeout: int = 20,
    ttl_seconds: int = 900,
    expected_github_login: str = "",
    context_manifest: str = "",
    require_context: bool = False,
) -> dict[str, Any]:
    if scope not in {"session", "work"}:
        raise ValueError("scope must be session or work")
    config = load_readiness_config()
    expected_github_login = expected_github_login or str(config.get("github_login", ""))
    require_github = bool(expected_github_login) or bool(config.get("require_github")) or os.environ.get("AGENT_BRIDGE_REQUIRE_GITHUB") in {"1", "true", "TRUE", "yes"}
    context_manifest = context_manifest or str(config.get("context_manifest", ""))
    require_context = require_context or bool(config.get("require_context"))
    gui_surface = surface == "gui"
    checks = _gui_surface_checks(client) if gui_surface else [_binary_check(client)]
    if scope == "session" and not gui_surface:
        checks.append(_check("client_auth", "unknown", "live authentication is deferred to work preflight", required=False))
    elif gui_surface:
        pass
    elif client == "ollama":
        checks.append(_ollama_check(timeout))
    else:
        checks.append(_auth_check(client, project_dir=project_dir, timeout=timeout))
    if client != "ollama":
        checks.append(_mcp_check(client))
    if scope == "work":
        if client == "ollama":
            pass
        elif gui_surface:
            checks.append(
                _check(
                    "mcp_health",
                    "unknown",
                    "GUI connector health cannot be inherited from CLI MCP state",
                    required=False,
                    error_class="unknown",
                    repair_command="inspect the connector status in the authenticated GUI session",
                )
            )
        else:
            checks.extend(_mcp_health_checks(client, project_dir=project_dir, timeout=timeout))
        checks.append(_github_check(project_dir=project_dir, timeout=timeout, expected_login=expected_github_login, required=require_github))
        if context_manifest:
            from .context_adapters import ContextAdapterError, context_status

            try:
                context = context_status(Path(context_manifest).expanduser().resolve())
                checks.append(
                    _check(
                        "context",
                        "ready" if context["ok"] else ("blocked" if require_context else "degraded"),
                        "all configured context adapters are current" if context["ok"] else "one or more context adapters are missing, stale, or conflicted",
                        required=require_context,
                        error_class="" if context["ok"] else "context_stale",
                    )
                )
            except (ContextAdapterError, OSError) as exc:
                checks.append(_check("context", "blocked" if require_context else "degraded", str(exc), required=require_context, error_class="context_stale"))
    roots = resolve_shared_roots()
    missing_roots = [kind for kind, row in roots["roots"].items() if not row["exists"]]
    root_status = "degraded" if roots["conflicts"] or missing_roots else "ready"
    explicit = all(row["explicit"] for row in roots["roots"].values())
    if roots["conflicts"]:
        root_detail = "multiple roots exist without explicit overrides"
    elif missing_roots:
        root_detail = f"missing shared roots: {', '.join(missing_roots)}"
    else:
        root_detail = f"{'explicit' if explicit else 'discovered'} roots exist"
    checks.append(
        _check(
            "shared_roots",
            root_status,
            root_detail,
            required=False,
            error_class="config_missing" if roots["conflicts"] or missing_roots else "",
        )
    )
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "agent-bridge.readiness",
        "scope": scope,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + dt.timedelta(seconds=ttl_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "machine_id": machine_id(),
        "client": client,
        "surface": surface,
        "project_dir": str(project_dir),
        "overall": _overall(checks),
        "checks": checks,
        "shared_roots": roots,
    }
    schema_errors = validate_readiness_report(report)
    if schema_errors:
        raise ValueError("invalid readiness report: " + "; ".join(schema_errors))
    _atomic_json(readiness_path(client, surface, scope), report)
    return report


def load_cached_preflight(client: str, surface: str, *, scope: str, allow_stale: bool = False) -> dict[str, Any] | None:
    path = readiness_path(client, surface, scope)
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expires = parse_iso(report.get("expires_at"))
    stale = expires is None or expires < dt.datetime.now(dt.timezone.utc)
    report["cache_file"] = str(path)
    report["stale"] = stale
    return report if allow_stale or not stale else None


def redacted_summary(report: dict[str, Any]) -> dict[str, Any]:
    expires = parse_iso(report.get("expires_at"))
    stale = expires is None or expires < dt.datetime.now(dt.timezone.utc)
    return {
        "schema_version": report.get("schema_version", SCHEMA_VERSION),
        "kind": "agent-bridge.readiness-summary",
        "generated_at": report.get("generated_at", iso_now()),
        "expires_at": report.get("expires_at", ""),
        "machine_id": report.get("machine_id", ""),
        "client": report.get("client", ""),
        "surface": report.get("surface", ""),
        "scope": report.get("scope", ""),
        "overall": report.get("overall", "unknown"),
        "stale": stale,
        "checks": [
            {
                "name": row.get("name", ""),
                "status": row.get("status", "unknown"),
                "required": bool(row.get("required")),
                "error_class": row.get("error_class", ""),
            }
            for row in report.get("checks", [])
            if isinstance(row, dict)
        ],
    }


def publish_readiness(report: dict[str, Any], *, data_root: str = "") -> dict[str, Any]:
    roots = resolve_shared_roots(create=False)
    selected = data_root or roots["roots"]["data"]["selected"]
    if not selected:
        raise ValueError("no SharedAgentData root could be resolved")
    if not data_root and not roots["roots"]["data"]["explicit"]:
        raise ValueError("SharedAgentData must be explicitly configured before publication")
    root = Path(selected).expanduser()
    path = root / "Agent-Bridge" / "readiness" / f"{_safe_fragment(report.get('machine_id', machine_id()))}.{_safe_fragment(report.get('client', 'unknown'))}.{_safe_fragment(report.get('surface', 'unknown'))}.{_safe_fragment(report.get('scope', 'unknown'))}.json"
    summary = redacted_summary(report)
    event_payload = {key: summary[key] for key in ("generated_at", "expires_at", "machine_id", "client", "surface", "scope", "overall", "stale")}
    event_id = hashlib.sha256(json.dumps(event_payload, sort_keys=True).encode("utf-8")).hexdigest()
    def publish_shared() -> tuple[bool, Path]:
        changed = _atomic_json_if_changed(path, summary)
        events = root / "Agent-Bridge" / "readiness-events" / f"{_safe_fragment(summary['machine_id'])}.jsonl"
        event_exists = False
        try:
            with events.open(encoding="utf-8") as handle:
                event_exists = any(json.loads(line).get("event_id") == event_id for line in handle if line.strip())
        except (OSError, json.JSONDecodeError):
            event_exists = False
        if not event_exists:
            events.parent.mkdir(parents=True, exist_ok=True)
            with events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_id": event_id, **event_payload}, sort_keys=True) + "\n")
        return changed, events

    try:
        changed, events = _bounded_call(publish_shared, timeout=2.0)
        summary.update({"published": True, "changed": changed, "published_file": str(path), "event_file": str(events)})
    except (OSError, TimeoutError) as exc:
        if not data_root and not roots["roots"]["data"]["explicit"]:
            raise
        queued = state_dir() / "readiness" / "publish-queue" / path.name
        _atomic_json(queued, summary)
        summary.update({"published": False, "changed": False, "queued_file": str(queued), "error_class": "source_unreachable", "error": _sanitize_detail(str(exc))})
    return summary


def flush_readiness_queue(*, data_root: str = "") -> dict[str, Any]:
    queue = state_dir() / "readiness" / "publish-queue"
    rows: list[dict[str, Any]] = []
    for path in sorted(queue.glob("*.json")) if queue.exists() else []:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            result = publish_readiness(report, data_root=data_root)
            if result.get("published"):
                path.unlink()
            rows.append({"queue_file": str(path), "published": bool(result.get("published"))})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            rows.append({"queue_file": str(path), "published": False, "error": _sanitize_detail(str(exc))})
    return {"schema_version": SCHEMA_VERSION, "flushed": sum(row["published"] for row in rows), "rows": rows}


def aggregate_readiness(*, data_root: str, write: bool = False) -> dict[str, Any]:
    """Rebuild the shared aggregate from authoritative per-surface summaries."""
    root = Path(data_root).expanduser() / "Agent-Bridge" / "readiness"
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        if path.name == "aggregate.json":
            continue
        try:
            row = json.loads(_bounded_read_text(path))
        except (OSError, TimeoutError, json.JSONDecodeError):
            continue
        if row.get("kind") != "agent-bridge.readiness-summary":
            continue
        expires = parse_iso(row.get("expires_at"))
        row["stale"] = expires is None or expires < dt.datetime.now(dt.timezone.utc)
        rows.append(row)
    result = {"schema_version": SCHEMA_VERSION, "kind": "agent-bridge.readiness-aggregate", "generated_at": iso_now(), "rows": rows}
    if write:
        target = root / "aggregate.json"
        _atomic_json_if_changed(target, result)
        result["aggregate_file"] = str(target)
    return result
