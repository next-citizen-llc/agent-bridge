"""Live Codex hook-trust auditing and bounded repair.

Codex records hook trust in ``~/.codex/config.toml`` as::

    [hooks.state."<config file>:<event>:<group index>:<handler index>"]
    trusted_hash = "sha256:..."

and executes a handler only when that stored hash equals the handler's current
content hash. Both halves of that contract are positional: inserting, removing
or reordering a handler shifts every later handler onto a neighbour's record,
and changing a command string invalidates the handler's own record. A hook that
loses trust is still loaded and still reported as installed by every
configuration-file check -- it is simply skipped, silently.

Reading ``hooks.json`` therefore cannot tell you whether a hook runs. This
module asks Codex instead, over the local app server, and can repair trust for
handlers whose commands come from an owned source root.

Stdlib only, bounded, and safe to call when Codex is absent: every failure path
raises :class:`CodexHooksError` rather than blocking a caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Any

from .correlation import utc_stamp

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
CODEX_CONFIG = CODEX_HOME / "config.toml"
DEFAULT_TIMEOUT_SECONDS = 20

#: Source roots whose hook commands this machine owns. A handler is "owned"
#: when its command references one of these paths, which is what makes
#: automatic trust repair defensible: the command was installed deliberately
#: from a canonical checkout, and its content can be read and reviewed.
#: Override with AGENT_BRIDGE_CODEX_OWNED_ROOTS (os.pathsep-separated).
DEFAULT_OWNED_ROOTS = (
    "~/Code/agent-bridge",
    "~/Code/skills-vault",
    "~/.local/bin/agent",
    "~/.local/share/tristan-ai",
    "~/.codex/skills",
    "~/.codex/hooks",
)

STATE_KEY = re.compile(r'^\s*\[hooks\.state\."(?P<key>[^"]+)"\]\s*$')


class CodexHooksError(RuntimeError):
    """Codex's hook registry could not be read or written."""


def owned_roots() -> list[str]:
    raw = os.environ.get("AGENT_BRIDGE_CODEX_OWNED_ROOTS")
    values = raw.split(os.pathsep) if raw else list(DEFAULT_OWNED_ROOTS)
    resolved: list[str] = []
    for value in values:
        text = value.strip()
        if text:
            resolved.append(str(Path(text).expanduser()))
    return resolved


def is_owned(command: str, roots: list[str] | None = None) -> bool:
    """True when a hook command references an owned source root.

    Matching is textual because a hook command is a shell string, not a path:
    it may carry an interpreter, quoting, ``env`` prefixes and arguments.
    """
    if not command:
        return False
    candidates = roots if roots is not None else owned_roots()
    return any(root and root in command for root in candidates)


class _AppServer:
    """Minimal JSONL client for ``codex app-server --stdio``."""

    def __init__(self, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.timeout = max(1, timeout)
        binary = os.environ.get("CODEX_BIN", "codex")
        if shutil.which(binary) is None:
            raise CodexHooksError(f"{binary} is not on PATH; cannot read the live hook registry")
        try:
            self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                [binary, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CodexHooksError(f"could not start the Codex app server: {exc}") from exc
        self._messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self.process.stdout
        if stream is None:
            return
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                with self._lock:
                    self._messages.append(value)
                self._ready.set()

    def send(self, value: dict[str, Any]) -> None:
        stream = self.process.stdin
        if stream is None:
            raise CodexHooksError("the Codex app server closed its input stream")
        try:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")
            stream.flush()
        except (OSError, ValueError) as exc:
            raise CodexHooksError(f"could not write to the Codex app server: {exc}") from exc

    def wait(self, request_id: int) -> dict[str, Any]:
        deadline = threading.Event()
        timer = threading.Timer(self.timeout, deadline.set)
        timer.start()
        try:
            while True:
                with self._lock:
                    for index, message in enumerate(self._messages):
                        if message.get("id") == request_id:
                            found = self._messages.pop(index)
                            if "error" in found:
                                raise CodexHooksError(f"Codex app server rejected request {request_id}: {found['error']}")
                            return found
                if deadline.is_set():
                    raise CodexHooksError(f"the Codex app server did not answer request {request_id} within {self.timeout}s")
                if self.process.poll() is not None:
                    raise CodexHooksError("the Codex app server exited before answering")
                self._ready.wait(0.1)
                self._ready.clear()
        finally:
            timer.cancel()

    def handshake(self) -> None:
        self.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-bridge-hook-audit", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        self.wait(1)
        self.send({"method": "initialized", "params": {}})

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()


def _normalize(hook: dict[str, Any], source_path: str) -> dict[str, Any]:
    key = str(hook.get("key") or "")
    command = str(hook.get("command") or "")
    if not command:
        handler = hook.get("handler")
        if isinstance(handler, dict):
            command = str(handler.get("command") or "")
            if not command and handler.get("server"):
                command = f"mcp:{handler.get('server')}/{handler.get('tool')}"
    trust = str(hook.get("trustStatus") or hook.get("trust_status") or "unknown")
    return {
        "key": key,
        "event": str(hook.get("eventName") or hook.get("event_name") or ""),
        "matcher": hook.get("matcher"),
        "command": command,
        "trust_status": trust,
        "enabled": bool(hook.get("enabled", False)),
        "current_hash": str(hook.get("currentHash") or hook.get("current_hash") or ""),
        "builtin": bool(hook.get("builtin", False)),
        "source_path": source_path,
        "owned": is_owned(command),
        "runs": trust in {"trusted", "managed"} and bool(hook.get("enabled", False)),
    }


def list_hooks(cwds: list[str] | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
    """Return every hook handler Codex has loaded, with its live trust status."""
    targets = cwds or [str(Path.cwd())]
    server = _AppServer(timeout=timeout)
    try:
        server.handshake()
        server.send({"id": 2, "method": "hooks/list", "params": {"cwds": targets}})
        response = server.wait(2)
    finally:
        server.close()
    entries = response.get("result", {}).get("data")
    if not isinstance(entries, list):
        raise CodexHooksError("the Codex hook registry returned no data")
    handlers: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        errors = entry.get("errors")
        if errors:
            raise CodexHooksError(f"the Codex hook registry reported load errors: {errors}")
        source_path = str(entry.get("sourcePath") or entry.get("source_path") or "")
        for hook in entry.get("hooks", []) or []:
            if isinstance(hook, dict):
                handlers.append(_normalize(hook, source_path))
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for handler in handlers:
        marker = f"{handler['source_path']}|{handler['key']}|{handler['command']}"
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(handler)
    return unique


def audit(cwds: list[str] | None = None, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Classify every loaded hook, separating owned breakage from foreign."""
    handlers = list_hooks(cwds=cwds, timeout=timeout)
    blocked = [item for item in handlers if not item["runs"] and not item["builtin"]]
    return {
        "total": len(handlers),
        "running": len([item for item in handlers if item["runs"]]),
        "blocked": blocked,
        "blocked_owned": [item for item in blocked if item["owned"]],
        "blocked_foreign": [item for item in blocked if not item["owned"]],
        "handlers": handlers,
        "stale_state_keys": stale_state_keys(handlers),
    }


def config_state_keys(config_path: Path | None = None) -> list[str]:
    """Every ``hooks.state`` key recorded in Codex's config file."""
    path = config_path or CODEX_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    keys: list[str] = []
    for line in text.splitlines():
        match = STATE_KEY.match(line)
        if match:
            keys.append(match.group("key"))
    return keys


def stale_state_keys(handlers: list[dict[str, Any]], config_path: Path | None = None) -> list[str]:
    """Trust records that no longer address a live handler.

    These are the residue of positional keying: when a handler is removed, its
    record stays behind and the handlers after it inherit the wrong records.
    """
    live = {item["key"] for item in handlers if item["key"]}
    return [key for key in config_state_keys(config_path) if key not in live]


def prune_stale_state(
    handlers: list[dict[str, Any]],
    config_path: Path | None = None,
    apply_changes: bool = False,
) -> dict[str, Any]:
    """Remove trust records that address no live handler. Backs up first."""
    path = config_path or CODEX_CONFIG
    stale = stale_state_keys(handlers, config_path=path)
    result: dict[str, Any] = {"stale": stale, "removed": [], "backup": None, "config_path": str(path)}
    if not stale or not apply_changes:
        return result
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexHooksError(f"could not read {path}: {exc}") from exc
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    dropping = False
    removed: list[str] = []
    for line in lines:
        match = STATE_KEY.match(line.rstrip("\n"))
        if match:
            dropping = match.group("key") in stale
            if dropping:
                removed.append(match.group("key"))
                continue
        elif dropping:
            stripped = line.strip()
            if stripped.startswith("[") or (stripped and "=" not in stripped):
                dropping = False
            else:
                continue
        kept.append(line)
    backup = path.with_name(f"{path.name}.bak-{utc_stamp()}")
    shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("".join(kept), encoding="utf-8")
    os.chmod(temporary, path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    result["removed"] = removed
    result["backup"] = str(backup)
    return result


def repair_trust(
    handlers: list[dict[str, Any]],
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Persist normal trust for owned, blocked handlers.

    Only handlers whose command comes from an owned source root are touched,
    and each is trusted against the hash Codex itself reports for the handler
    as it stands. This is Codex's ordinary trust path -- it is deliberately not
    ``--dangerously-bypass-hook-trust``, which would run every untrusted hook
    in the file including anything a third party added.
    """
    targets = [item for item in handlers if item["owned"] and not item["runs"] and item["current_hash"]]
    if not targets:
        return {"trusted": [], "skipped": []}
    edits = [
        {
            "keyPath": f'hooks.state."{item["key"]}".trusted_hash',
            "value": item["current_hash"],
            "mergeStrategy": "upsert",
        }
        for item in targets
    ]
    server = _AppServer(timeout=timeout)
    try:
        server.handshake()
        server.send(
            {
                "id": 3,
                "method": "config/batchWrite",
                "params": {"edits": edits, "reloadUserConfig": True},
            }
        )
        server.wait(3)
    finally:
        server.close()
    return {
        "trusted": [{"key": item["key"], "event": item["event"], "command": item["command"]} for item in targets],
        "skipped": [
            {"key": item["key"], "event": item["event"], "command": item["command"], "reason": "not an owned source root"}
            for item in handlers
            if not item["runs"] and not item["owned"] and not item["builtin"]
        ],
    }


def format_audit(report: dict[str, Any]) -> list[str]:
    """Human-readable audit lines, blocked hooks first."""
    lines = [
        f"codex hook registry: {report['running']} of {report['total']} handlers run"
        f" ({len(report['blocked_owned'])} owned blocked, {len(report['blocked_foreign'])} foreign blocked)"
    ]
    for item in report["blocked"]:
        owner = "owned" if item["owned"] else "foreign"
        lines.append(
            f"  BLOCKED [{item['trust_status']}, {owner}] {item['event']} {item['key']}: {item['command'][:96]}"
        )
    for key in report["stale_state_keys"]:
        lines.append(f"  STALE trust record with no live handler: {key}")
    return lines
