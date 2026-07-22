"""Evidence-first recovery of interrupted native harness sessions.

The recovery surface deliberately does not import one product's native chat
history into another product.  It reads the local Claude session indexes,
locates the corresponding JSONL evidence, captures current Git/GitHub state,
and writes bounded continuation handoffs under Agent Bridge's private state
directory.  A caller may also enqueue one durable Agent Bridge task per
unfinished session.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from .coord import state_dir, task_attach_artifact, task_create, task_inspect
from .correlation import iso_now, new_id, safe_fragment


def _default_claude_data_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude"


DEFAULT_CLAUDE_DATA_ROOT = _default_claude_data_root()
DEFAULT_CLAUDE_PROJECTS_ROOT = Path.home() / ".claude/projects"
RECOVERY_SCHEMA_VERSION = "1.0"
RECOVERY_KIND = "active_session_recovery"
DISPOSITIONS = {"continue", "complete"}

USAGE_LIMIT_RE = re.compile(
    r"(?i)(usage\s+limit|rate\s+limit|hit\s+(?:your|the)\s+limit|"
    r"maximum\s+usage|out\s+of\s+(?:extra\s+)?usage|credit\s+balance|plan\s+limit)"
)
SECRET_FIELD_RE = re.compile(
    r"(?i)(\b(?:api[ _-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"auth(?:orization)?(?:[_-]?token)?|client[_-]?secret|password|passwd|private[_-]?key|"
    r"credential(?:s)?|cookie|secret)\b\s*[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s,;\"'}]+)"
)
BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|auth|authorization|signature|secret)=)[^&#\s]+"
)
KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})\b"
)
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.DOTALL,
)


class SessionRecoveryError(RuntimeError):
    """Raised when a recovery inventory or package cannot be produced safely."""


@dataclass(frozen=True)
class CommandResult:
    return_code: int
    stdout: str
    stderr: str


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            return dt.datetime.fromisoformat(raw).timestamp()
        except ValueError:
            try:
                return _as_epoch(float(raw))
            except ValueError:
                return None
    return None


def _iso(value: Any) -> str:
    epoch = _as_epoch(value)
    if epoch is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def redact_text(value: str) -> str:
    """Remove common credential shapes from private continuation context."""

    text = value.replace("\x00", "")
    text = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_FIELD_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = BEARER_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = URL_SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = KNOWN_TOKEN_RE.sub("[REDACTED TOKEN]", text)
    return text


def _metadata_files(data_root: Path) -> list[tuple[str, Path]]:
    roots = [
        ("claude_code", data_root / "claude-code-sessions"),
        ("claude_desktop", data_root / "local-agent-mode-sessions"),
    ]
    rows: list[tuple[str, Path]] = []
    for source_kind, root in roots:
        if not root.exists():
            continue
        for path in root.rglob("local_*.json"):
            if path.is_file():
                rows.append((source_kind, path))
    return rows


def _project_candidates(payload: dict[str, Any], *, prefer_selected: bool = False) -> list[str]:
    native_values: list[str] = []
    for key in ("cwd", "originCwd"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            native_values.append(raw.strip())
    selected_values: list[str] = []
    selected = payload.get("userSelectedFolders")
    if isinstance(selected, list):
        for item in selected:
            if isinstance(item, str) and item.strip():
                selected_values.append(item.strip())
            elif isinstance(item, dict):
                raw = item.get("path") or item.get("folder")
                if isinstance(raw, str) and raw.strip():
                    selected_values.append(raw.strip())
    values = selected_values + native_values if prefer_selected else native_values + selected_values
    unique: list[str] = []
    for value in values:
        expanded = str(Path(value).expanduser())
        if expanded not in unique:
            unique.append(expanded)
    return unique


def _build_transcript_index(projects_root: Path, native_ids: set[str]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    if not projects_root.exists() or not native_ids:
        return index
    for path in projects_root.rglob("*.jsonl"):
        if path.stem in native_ids and path.stem not in index:
            index[path.stem] = path
            if len(index) == len(native_ids):
                break
    return index


def _audit_path(metadata_path: Path) -> Path | None:
    candidate = metadata_path.with_suffix("") / "audit.jsonl"
    return candidate if candidate.is_file() else None


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if not isinstance(value, dict):
        return ""
    block_type = str(value.get("type") or "").lower()
    if block_type and block_type not in {"text", "input_text", "output_text", "message"}:
        return ""
    if isinstance(value.get("text"), str):
        return str(value["text"])
    if "content" in value:
        return _extract_text(value["content"])
    return ""


def _signal_text(row: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "error",
        "apiErrorStatus",
        "api_error_status",
        "terminal_reason",
        "stop_reason",
        "result",
        "rate_limit_info",
    ):
        value = row.get(key)
        if isinstance(value, str):
            values.append(value[:2000])
        elif isinstance(value, (dict, list)):
            try:
                values.append(json.dumps(value, sort_keys=True)[:2000])
            except TypeError:
                pass
    return " ".join(values)


def inspect_transcript(
    path: Path | None,
    *,
    include_context: bool = False,
    context_messages: int = 8,
    context_chars: int = 12_000,
) -> dict[str, Any]:
    """Read only bounded message text and terminal signals from a JSONL transcript."""

    if path is None or not path.is_file():
        return {
            "status": "missing_transcript",
            "reasons": ["no matching transcript or audit log was found"],
            "latest_user_at": "",
            "latest_assistant_at": "",
            "context": [],
            "corrupt_lines": 0,
        }

    last_user = -1
    last_assistant_success = -1
    last_api_error = -1
    last_usage_limit = -1
    latest_user_at = ""
    latest_assistant_at = ""
    contexts: list[dict[str, str]] = []
    corrupt = 0
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        return {
            "status": "unreadable_transcript",
            "reasons": [redact_text(str(exc))],
            "latest_user_at": "",
            "latest_assistant_at": "",
            "context": [],
            "corrupt_lines": 0,
        }
    with handle:
        for index, line in enumerate(handle):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue
            if not isinstance(row, dict):
                corrupt += 1
                continue
            row_type = str(row.get("type") or "").lower()
            timestamp = _iso(row.get("timestamp") or row.get("_audit_timestamp"))
            signal = _signal_text(row)
            is_api_error = bool(row.get("isApiErrorMessage") or row.get("is_error"))
            if row_type == "rate_limit_event" or USAGE_LIMIT_RE.search(signal):
                last_usage_limit = index
            if is_api_error or row_type in {"api_error", "retry"}:
                last_api_error = index

            if row_type not in {"user", "assistant"}:
                continue
            if row.get("isSidechain") or row.get("isMeta") or row.get("isSynthetic"):
                continue
            text = _extract_text(row.get("message"))
            if not text and isinstance(row.get("content"), (str, list, dict)):
                text = _extract_text(row.get("content"))
            text = text.strip()
            if row_type == "user":
                last_user = index
                latest_user_at = timestamp or latest_user_at
            elif not is_api_error and text:
                last_assistant_success = index
                latest_assistant_at = timestamp or latest_assistant_at
            if include_context and text:
                contexts.append({"role": row_type, "timestamp": timestamp, "text": redact_text(text)})

    reasons: list[str] = []
    if last_usage_limit >= max(last_user, last_assistant_success):
        status = "blocked_usage_limit"
        reasons.append("the latest turn contains a usage or rate-limit signal")
    elif last_api_error >= max(last_user, last_assistant_success):
        status = "blocked_api_error"
        reasons.append("the latest turn contains an API error")
    elif last_user > last_assistant_success:
        status = "awaiting_assistant"
        reasons.append("the latest user turn has no later successful assistant text")
    else:
        status = "review_required"
        reasons.append("completion cannot be inferred safely from transcript order alone")

    bounded: list[dict[str, str]] = []
    remaining = max(0, context_chars)
    for row in reversed(contexts[-max(0, context_messages) :]):
        if remaining <= 0:
            break
        text = row["text"]
        if len(text) > remaining:
            marker = "\n...[bounded context truncated]...\n"
            if remaining <= len(marker):
                text = text[:remaining]
            else:
                available = remaining - len(marker)
                head = available // 2
                tail = available - head
                text = text[:head] + marker + text[-tail:]
        bounded.append({**row, "text": text})
        remaining -= len(text)
    bounded.reverse()
    return {
        "status": status,
        "reasons": reasons,
        "latest_user_at": latest_user_at,
        "latest_assistant_at": latest_assistant_at,
        "context": bounded,
        "corrupt_lines": corrupt,
    }


def discover_claude_sessions(
    *,
    data_root: Path = DEFAULT_CLAUDE_DATA_ROOT,
    projects_root: Path = DEFAULT_CLAUDE_PROJECTS_ROOT,
) -> list[dict[str, Any]]:
    raw: list[tuple[str, Path, dict[str, Any]]] = []
    native_ids: set[str] = set()
    for source_kind, path in _metadata_files(data_root):
        payload = _read_json(path)
        if not payload:
            continue
        native_id = str(payload.get("cliSessionId") or "").strip()
        if native_id:
            native_ids.add(native_id)
        raw.append((source_kind, path, payload))
    transcript_index = _build_transcript_index(projects_root, native_ids)
    sessions: dict[str, dict[str, Any]] = {}
    for source_kind, metadata_path, payload in raw:
        session_id = str(payload.get("sessionId") or metadata_path.stem).strip()
        if not session_id:
            continue
        native_id = str(payload.get("cliSessionId") or "").strip()
        transcript = _audit_path(metadata_path) if source_kind == "claude_desktop" else None
        transcript_kind = "claude_desktop_audit" if transcript else ""
        if transcript is None and native_id:
            transcript = transcript_index.get(native_id)
            if transcript:
                transcript_kind = "claude_code_jsonl"
        signal = inspect_transcript(transcript)
        activity_candidates = [
            _as_epoch(payload.get("lastActivityAt")),
            _as_epoch(signal.get("latest_user_at")),
            _as_epoch(signal.get("latest_assistant_at")),
            _mtime(metadata_path),
        ]
        last_activity_epoch = max(value for value in activity_candidates if value is not None)
        record = {
            "session_id": session_id,
            "native_session_id": native_id,
            "source": source_kind,
            "title": str(payload.get("title") or session_id),
            "last_activity_at": _iso(last_activity_epoch),
            "last_activity_epoch": last_activity_epoch,
            "created_at": _iso(payload.get("createdAt")),
            "archived": bool(payload.get("isArchived")),
            "project_candidates": _project_candidates(payload, prefer_selected=source_kind == "claude_desktop"),
            "branch": str(payload.get("branch") or payload.get("gitBranch") or ""),
            "metadata_path": str(metadata_path),
            "transcript_path": str(transcript) if transcript else "",
            "transcript_kind": transcript_kind,
        }
        record["signal"] = signal
        current = sessions.get(session_id)
        if current is None or float(record["last_activity_epoch"]) >= float(current["last_activity_epoch"]):
            sessions[session_id] = record
    return sorted(sessions.values(), key=lambda row: float(row["last_activity_epoch"]), reverse=True)


def filter_sessions(
    sessions: Iterable[dict[str, Any]],
    *,
    session_ids: Iterable[str] = (),
    since_hours: float | None = 168.0,
    include_archived: bool = False,
    title: str = "",
    limit: int | None = 50,
    now: float | None = None,
) -> list[dict[str, Any]]:
    requested = {value for value in session_ids if value}
    cutoff = None if since_hours is None else (now if now is not None else time.time()) - max(0.0, since_hours) * 3600.0
    title_folded = title.casefold().strip()
    rows: list[dict[str, Any]] = []
    for row in sessions:
        if requested and row["session_id"] not in requested:
            continue
        if not requested and not include_archived and row.get("archived"):
            continue
        if not requested and cutoff is not None and float(row.get("last_activity_epoch") or 0) < cutoff:
            continue
        if title_folded and title_folded not in str(row.get("title") or "").casefold():
            continue
        rows.append(row)
        if limit is not None and limit > 0 and len(rows) >= limit:
            break
    return rows


def _run(args: list[str], *, cwd: Path, timeout: int = 15) -> CommandResult:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(124, "", redact_text(str(exc)))
    return CommandResult(proc.returncode, redact_text(proc.stdout.strip()), redact_text(proc.stderr.strip()))


def _sanitize_remote(remote: str) -> str:
    if "://" not in remote:
        return redact_text(remote)
    parsed = urlsplit(remote)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def _github_slug(remote: str) -> str:
    value = remote.strip()
    match = re.search(r"github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?$", value)
    return match.group(1) if match else ""


def choose_project_dir(record: dict[str, Any], override: str | None = None) -> Path | None:
    candidates = [override] if override else []
    candidates.extend(record.get("project_candidates") or [])
    for value in candidates:
        if not value:
            continue
        path = Path(str(value)).expanduser()
        if path.is_dir():
            return path.resolve()
    return None


def collect_git_evidence(project_dir: Path | None, *, verify_github: bool = False) -> dict[str, Any]:
    if project_dir is None:
        return {"status": "missing_project", "project_dir": "", "github": {"status": "not_checked"}}
    root_result = _run(["git", "rev-parse", "--show-toplevel"], cwd=project_dir)
    if root_result.return_code != 0 or not root_result.stdout:
        return {
            "status": "not_a_git_worktree",
            "project_dir": str(project_dir),
            "error": root_result.stderr,
            "github": {"status": "not_checked"},
        }
    root = Path(root_result.stdout).resolve()
    branch = _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=root).stdout
    head = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout
    status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root).stdout
    remote = _run(["git", "remote", "get-url", "origin"], cwd=root).stdout
    evidence: dict[str, Any] = {
        "status": "ok",
        "project_dir": str(project_dir),
        "git_root": str(root),
        "branch": branch or "DETACHED",
        "head": head,
        "worktree_clean": not bool(status),
        "worktree_status": status.splitlines()[:200],
        "origin": _sanitize_remote(remote),
        "github": {"status": "not_checked"},
    }
    if not verify_github:
        return evidence
    slug = _github_slug(remote)
    if not slug or not branch:
        evidence["github"] = {"status": "unavailable", "reason": "GitHub repository or branch could not be resolved"}
        return evidence
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "20",
            "--json",
            "number,title,state,isDraft,url,headRefName,baseRefName",
        ],
        cwd=root,
        timeout=20,
    )
    if result.return_code != 0:
        evidence["github"] = {"status": "unavailable", "reason": result.stderr or "gh pr list failed"}
        return evidence
    try:
        pulls = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        evidence["github"] = {"status": "unavailable", "reason": "gh returned invalid JSON"}
        return evidence
    evidence["github"] = {"status": "ok", "repository": slug, "pull_requests": pulls}
    return evidence


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(value.rstrip() + "\n", encoding="utf-8")
    temp.replace(path)


def _assert_private_output_root(path: Path) -> None:
    existing = path.expanduser().resolve()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir():
        return
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=existing)
    if result.return_code != 0 or not result.stdout:
        return
    git_root = Path(result.stdout).resolve()
    requested = path.expanduser().resolve()
    try:
        requested.relative_to(git_root)
    except ValueError:
        return
    raise SessionRecoveryError(
        f"recovery output must stay outside Git worktrees; choose a private state path instead of {requested}"
    )


def _task_id(source: str, session_id: str, target: str, source_cursor: str) -> str:
    digest = hashlib.sha256(f"{source}:{session_id}:{target}:{source_cursor}".encode("utf-8")).hexdigest()[:20]
    return f"task_recovery_{digest}"


def _queue_handoff(
    *,
    source: str,
    target: str,
    session: dict[str, Any],
    run_id: str,
    handoff_path: Path,
    project_dir: Path | None,
    source_cursor: str,
) -> dict[str, Any]:
    task_id = _task_id(source, session["session_id"], target, source_cursor)
    existing = task_inspect(task_id)
    if existing.get("events"):
        artifacts = {str(row.get("path")) for row in existing.get("artifacts", []) if isinstance(row, dict)}
        if str(handoff_path) not in artifacts and existing.get("status") not in {"completed", "cancelled"}:
            task_attach_artifact(task_id, path=str(handoff_path), kind="session_handoff")
        return {"status": "existing", "task_id": task_id, "task_status": existing.get("status")}
    task_create(
        f"Continue {session.get('title') or session['session_id']} from {source}",
        task_id=task_id,
        run_id=run_id,
        owner=target,
        data={
            "kind": RECOVERY_KIND,
            "source": source,
            "source_session_id": session["session_id"],
            "source_cursor": source_cursor,
            "target": target,
            "project_dir": str(project_dir) if project_dir else "",
        },
    )
    task_attach_artifact(task_id, path=str(handoff_path), kind="session_handoff")
    return {"status": "created", "task_id": task_id, "task_status": "open"}


def _handoff_markdown(
    *,
    source: str,
    target: str,
    session: dict[str, Any],
    signal: dict[str, Any],
    git: dict[str, Any],
    project_dir: Path | None,
) -> str:
    project = str(project_dir) if project_dir else "unresolved - verify before dispatch"
    lines = [
        f"# Continue: {session.get('title') or session['session_id']}",
        "",
        "This is a bounded local handoff, not an imported native chat history.",
        "",
        "## Routing",
        "",
        f"- Source harness: `{source}`",
        f"- Source session: `{session['session_id']}`",
        f"- Native session: `{session.get('native_session_id') or 'unknown'}`",
        f"- Target harness: `{target}`",
        f"- Project/worktree: `{project}`",
        f"- Source signal: `{signal.get('status', 'unknown')}`",
        "",
        "## Evidence pointers",
        "",
        f"- Metadata: `{session.get('metadata_path') or 'unavailable'}`",
        f"- Transcript/audit log: `{session.get('transcript_path') or 'unavailable'}`",
        "- Raw transcript content was not copied into this bundle.",
        "",
        "## Current Git evidence",
        "",
        f"- Status: `{git.get('status', 'unknown')}`",
        f"- Git root: `{git.get('git_root') or 'unresolved'}`",
        f"- Branch: `{git.get('branch') or 'unresolved'}`",
        f"- HEAD: `{git.get('head') or 'unresolved'}`",
        f"- Worktree clean: `{git.get('worktree_clean', 'unknown')}`",
        f"- GitHub verification: `{(git.get('github') or {}).get('status', 'not_checked')}`",
    ]
    worktree_status = git.get("worktree_status") or []
    if worktree_status:
        lines.extend(["", "Worktree changes to preserve:", "", "```text", *worktree_status, "```"])
    lines.extend(
        [
            "",
            "## Continuation contract",
            "",
            "1. Re-read the source evidence at the paths above when more context is required.",
            "2. Verify live Git, pull-request, artifact, and external state before treating prior claims as current.",
            "3. Preserve the exact project/worktree association and all unrelated local changes.",
            "4. Continue the latest explicit user request; do not duplicate work already proven complete.",
            "5. Keep secrets and raw private transcripts out of repositories, issues, logs, and chat output.",
            "",
            "## Recent bounded context",
            "",
        ]
    )
    context = signal.get("context") or []
    if not context:
        lines.append("No bounded message context was available. Use the source evidence pointers.")
    for row in context:
        role = str(row.get("role") or "message").capitalize()
        timestamp = f" ({row.get('timestamp')})" if row.get("timestamp") else ""
        lines.extend([f"### {role}{timestamp}", "", str(row.get("text") or ""), ""])
    return "\n".join(lines)


def _summary_markdown(manifest: dict[str, Any]) -> str:
    rows = manifest.get("sessions") or []
    continuing = [row for row in rows if row.get("disposition") == "continue"]
    completed = [row for row in rows if row.get("disposition") == "complete"]
    lines = [
        "# Active Session Recovery",
        "",
        f"- Run: `{manifest['run_id']}`",
        f"- Source: `{manifest['source']}`",
        f"- Target: `{manifest['target']}`",
        f"- Continue: `{len(continuing)}`",
        f"- Operator-confirmed complete: `{len(completed)}`",
        "- Native target sessions created: `no`",
        "",
        "Agent Bridge staged private handoffs and optional durable task-ledger entries. The target harness must create or claim one isolated native task per continuation and preserve the listed project/worktree.",
        "",
        "| Disposition | Title | Source signal | Project | Task |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        title = str(row.get("title") or row.get("session_id") or "").replace("|", "\\|")
        project = str(row.get("project_dir") or "unresolved").replace("|", "\\|")
        task = (row.get("queue") or {}).get("task_id") or "not queued"
        lines.append(
            f"| {row.get('disposition')} | {title} | {row.get('source_signal')} | `{project}` | `{task}` |"
        )
    return "\n".join(lines)


def recover_sessions(
    sessions: Iterable[dict[str, Any]],
    *,
    decisions: dict[str, str],
    source: str = "claude",
    target: str = "codex",
    project_overrides: dict[str, str] | None = None,
    verify_github: bool = False,
    enqueue: bool = False,
    context_messages: int = 8,
    context_chars: int = 12_000,
    output_root: Path | None = None,
) -> dict[str, Any]:
    if not decisions:
        raise SessionRecoveryError("select at least one session to continue or mark complete")
    invalid = {value for value in decisions.values() if value not in DISPOSITIONS}
    if invalid:
        raise SessionRecoveryError(f"unknown recovery dispositions: {', '.join(sorted(invalid))}")
    indexed = {str(row.get("session_id")): row for row in sessions}
    missing = sorted(set(decisions) - set(indexed))
    if missing:
        raise SessionRecoveryError(f"session metadata not found: {', '.join(missing)}")
    run_id = new_id("recovery")
    root = (output_root or (state_dir() / "session-recovery")).expanduser().resolve()
    _assert_private_output_root(root)
    run_dir = root / safe_fragment(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "kind": RECOVERY_KIND,
        "run_id": run_id,
        "created_at": iso_now(),
        "source": source,
        "target": target,
        "artifact_dir": str(run_dir),
        "native_history_imported": False,
        "sessions": [],
    }
    overrides = project_overrides or {}
    for session_id, disposition in decisions.items():
        session = indexed[session_id]
        project_dir = choose_project_dir(session, overrides.get(session_id))
        git = collect_git_evidence(project_dir, verify_github=verify_github)
        signal = inspect_transcript(
            Path(session["transcript_path"]) if session.get("transcript_path") else None,
            include_context=disposition == "continue",
            context_messages=context_messages,
            context_chars=context_chars,
        )
        session_dir = run_dir / "sessions" / safe_fragment(session_id)
        evidence = {
            "schema_version": RECOVERY_SCHEMA_VERSION,
            "kind": "session_recovery_evidence",
            "disposition": disposition,
            "session": {key: value for key, value in session.items() if key not in {"signal", "last_activity_epoch"}},
            "signal": {key: value for key, value in signal.items() if key != "context"},
            "git": git,
            "project_dir": str(project_dir) if project_dir else "",
        }
        evidence_path = session_dir / "evidence.json"
        _write_json(evidence_path, evidence)
        handoff_path: Path | None = None
        queue: dict[str, Any] = {"status": "not_requested"}
        target_prompt = ""
        if disposition == "continue":
            handoff_path = session_dir / "handoff.md"
            _write_text(
                handoff_path,
                _handoff_markdown(
                    source=source,
                    target=target,
                    session=session,
                    signal=signal,
                    git=git,
                    project_dir=project_dir,
                ),
            )
            target_prompt = (
                f"Continue the recovered {source} session {session_id!r} in its preserved project/worktree. "
                f"Read {handoff_path}. Re-verify live state before changing anything and do not ask the user "
                "to repeat context already available there."
            )
            if enqueue:
                source_cursor = str(
                    signal.get("latest_user_at")
                    or signal.get("latest_assistant_at")
                    or session.get("last_activity_at")
                    or session.get("native_session_id")
                    or session_id
                )
                queue = _queue_handoff(
                    source=source,
                    target=target,
                    session=session,
                    run_id=run_id,
                    handoff_path=handoff_path,
                    project_dir=project_dir,
                    source_cursor=source_cursor,
                )
        entry = {
            "session_id": session_id,
            "title": session.get("title") or session_id,
            "disposition": disposition,
            "source_signal": signal.get("status"),
            "project_dir": str(project_dir) if project_dir else "",
            "evidence_path": str(evidence_path),
            "handoff_path": str(handoff_path) if handoff_path else "",
            "target_prompt": target_prompt,
            "queue": queue,
        }
        manifest["sessions"].append(entry)
    _write_json(run_dir / "manifest.json", manifest)
    _write_text(run_dir / "summary.md", _summary_markdown(manifest))
    return manifest


def load_recovery_selection(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    payload = _read_json(path)
    if payload is None:
        raise SessionRecoveryError(f"selection file is not valid JSON: {path}")
    rows = payload.get("sessions")
    if not isinstance(rows, list):
        raise SessionRecoveryError("selection file must contain a sessions array")
    decisions: dict[str, str] = {}
    projects: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SessionRecoveryError("each selection row must be an object")
        session_id = str(row.get("session_id") or "").strip()
        disposition = str(row.get("disposition") or "").strip().lower()
        if not session_id or disposition not in DISPOSITIONS:
            raise SessionRecoveryError("each selection row needs session_id and disposition continue|complete")
        existing = decisions.get(session_id)
        if existing and existing != disposition:
            raise SessionRecoveryError(f"conflicting dispositions for session {session_id}")
        decisions[session_id] = disposition
        if row.get("project_dir"):
            projects[session_id] = str(row["project_dir"])
    return decisions, projects


def format_session_inventory(rows: Iterable[dict[str, Any]]) -> str:
    values = list(rows)
    if not values:
        return "No matching Claude sessions found.\n"
    lines = ["STATUS\tUPDATED\tSESSION\tTITLE\tPROJECT"]
    for row in values:
        projects = row.get("project_candidates") or []
        lines.append(
            "\t".join(
                [
                    str((row.get("signal") or {}).get("status") or "unknown"),
                    str(row.get("last_activity_at") or ""),
                    str(row.get("session_id") or ""),
                    str(row.get("title") or "").replace("\t", " "),
                    str(projects[0] if projects else "").replace("\t", " "),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def format_recovery_result(manifest: dict[str, Any]) -> str:
    sessions = manifest.get("sessions") or []
    continuing = sum(1 for row in sessions if row.get("disposition") == "continue")
    completed = sum(1 for row in sessions if row.get("disposition") == "complete")
    queued = sum(1 for row in sessions if (row.get("queue") or {}).get("status") in {"created", "existing"})
    return (
        f"Recovery run: {manifest['run_id']}\n"
        f"Continue: {continuing}\n"
        f"Operator-confirmed complete: {completed}\n"
        f"Durable tasks queued or already present: {queued}\n"
        f"Artifacts: {manifest['artifact_dir']}\n"
        "Native target sessions created: no; the target harness must create or claim one isolated task per handoff.\n"
    )
