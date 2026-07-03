#!/usr/bin/env python3
"""Generic local agent bridge.

The public entry point is:

    agent code bridge

The bridge invokes fresh, bounded headless turns of configured agent CLIs. It is
filesystem/process based by design: no daemon, no IPC, and no assumption that
the caller is Codex or Claude.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import unquote, urlparse

from .correlation import add_meta_args, child_turn_meta, ensure_run_meta, extract_meta, format_meta, iso_now, safe_fragment, utc_stamp
from .findings import (
    create_finding,
    format_findings,
    format_verdicts,
    list_findings,
    list_verdicts,
    read_finding,
    record_verdict,
)
from .trace import emit_event, events_path, format_events, load_events
from .workflow import (
    WorkflowError,
    format_inspection,
    format_report,
    inspect_workflow_run,
    list_workflows,
    load_workflow,
    plan_workflow_run,
    run_workflow,
)


BRIDGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BRIDGE_DIR / "agents.json"
STATE_DIR = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")).expanduser()
TRANSCRIPT_DIR = STATE_DIR / "transcripts"
BRIDGE_LOG = STATE_DIR / "bridge_agents.log"
MEDIA_DIR = STATE_DIR / "media"
CONNECTION_STATE = STATE_DIR / "connections.json"
CLAUDE_AUTH_STATE = STATE_DIR / "claude-auth.json"
PROJECT_DIR = Path.cwd()
SHARED_BRIDGE_DIR_NAME = "Agent-Bridge"
SHARED_REGISTRY_DIR_NAME = "registry"
SHARED_SKILL_LINK_NAME = "agent-bridge"
SKILLS_VAULT_DEFAULT = Path(os.environ.get("AGENT_BRIDGE_SKILLS_VAULT", Path.home() / "Code/skills-vault")).expanduser()
DEFAULT_BUDGET_USD = "0.50"
DEFAULT_REPAIR_BUDGET_USD = "0.05"
DEFAULT_MAX_AUTO_BUDGET_USD = "1.00"
BUDGET_RETRY_LADDER = [0.10, 0.20, 0.50, 1.00, 2.00, 5.00]
HEIC_SUFFIXES = {".heic", ".heif"}
ANTHROPIC_ENV_NAMES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
SKILLS_CLIENTS = ("codex", "claude", "agents", "grok")
QUOTED_HEIC_PATH_RE = re.compile(
    r"""(?P<quote>['"`])(?P<path>(?:file://)?(?:~|/|\.{1,2}/|[A-Za-z]:[\\/])[^'"`\n]*?\.(?:heic|heif))(?P=quote)""",
    re.IGNORECASE,
)
UNQUOTED_HEIC_PATH_RE = re.compile(
    r"""(?P<path>(?:file://)?(?:~|/|\.{1,2}/|[A-Za-z]:[\\/])[^\s\]\)>,;:]+?\.(?:heic|heif))(?=$|[\s\]\)>,;:])""",
    re.IGNORECASE,
)


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class SpawnDecision:
    mode: str
    score: int
    reasons: list[str]


@dataclass(frozen=True)
class PromptMedia:
    prompt: str
    media_dirs: list[Path]
    conversions: list[tuple[Path, Path]]
    failures: list[tuple[Path, str]]


@dataclass(frozen=True)
class AgentRunResult:
    return_code: int
    output: str
    transcript: Path | None = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    budget_usd: str
    output: str
    repaired_auth: bool = False


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        raise BridgeError(f"{path} must define a non-empty agents list")
    seen: set[str] = set()
    for agent in agents:
        agent_id = agent.get("id")
        if not isinstance(agent_id, str) or not agent_id:
            raise BridgeError(f"{path} contains an agent without an id")
        if agent_id in seen:
            raise BridgeError(f"{path} contains duplicate agent id {agent_id!r}")
        seen.add(agent_id)
    return data


def agent_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {agent["id"]: agent for agent in config["agents"]}


def discover_project_dir() -> Path:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(Path.cwd()), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if output:
            return Path(output).resolve()
    except (OSError, subprocess.CalledProcessError):
        pass
    return Path.cwd().resolve()


def run_git(args: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(PROJECT_DIR), *args],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def resolve_command(agent: dict[str, Any]) -> str:
    env_name = agent.get("env_command")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    command = agent.get("command")
    if not command:
        raise BridgeError(f"agent {agent['id']} has no command")
    resolved = shutil.which(command)
    return resolved or command


def print_agent_list(agents: dict[str, dict[str, Any]]) -> None:
    for index, agent_id in enumerate(agents, start=1):
        agent = agents[agent_id]
        label = agent.get("label", agent_id)
        description = agent.get("description", "")
        suffix = f" - {description}" if description else ""
        print(f"{index}. {agent_id} ({label}){suffix}")


def split_selection(raw: str) -> list[str]:
    return [part.strip().lower() for part in raw.replace(",", " ").split() if part.strip()]


def resolve_agent_ids(raw: str, agents: dict[str, dict[str, Any]]) -> list[str]:
    if raw.strip().lower() in {"all", "*"}:
        return list(agents)
    resolved: list[str] = []
    ids = list(agents)
    for part in split_selection(raw):
        if part.isdigit():
            index = int(part)
            if index < 1 or index > len(ids):
                raise BridgeError(f"agent index {index} is out of range")
            agent_id = ids[index - 1]
        else:
            matches = [agent_id for agent_id in ids if agent_id == part or agent_id.startswith(part)]
            if len(matches) != 1:
                raise BridgeError(f"agent selection {part!r} did not match exactly one agent")
            agent_id = matches[0]
        if agent_id not in resolved:
            resolved.append(agent_id)
    return resolved


def prompt_line(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def interactive_options(args: argparse.Namespace, agents: dict[str, dict[str, Any]]) -> argparse.Namespace:
    if not sys.stdin.isatty():
        missing = []
        if not args.source:
            missing.append("--from")
        if not args.targets:
            missing.append("--to")
        if missing:
            raise BridgeError(f"non-interactive bridge call is missing: {', '.join(missing)}")
        return args

    print("Available agents:")
    print_agent_list(agents)
    print("")

    if not args.source:
        args.source = prompt_line("Calling agent or instance", os.environ.get("AGENT_BRIDGE_CALLER", "human"))

    if not args.targets:
        args.targets = prompt_line("Target agent(s), comma-separated names/numbers or 'all'")

    if not args.mode:
        args.mode = prompt_line("Mode: review or code", "review")

    if not args.prompt:
        print("Task prompt. End with a blank line:")
        lines: list[str] = []
        while True:
            line = input()
            if not line:
                break
            lines.append(line)
        args.prompt = "\n".join(lines).strip()

    return args


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return ""


def _load_connection_state() -> dict[str, Any]:
    if not CONNECTION_STATE.exists():
        return {"schema_version": "1.0", "agents": {}}
    try:
        data = json.loads(CONNECTION_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "1.0", "agents": {}}
    if not isinstance(data, dict):
        return {"schema_version": "1.0", "agents": {}}
    data.setdefault("schema_version", "1.0")
    if not isinstance(data.get("agents"), dict):
        data["agents"] = {}
    return data


def _write_connection_state(data: dict[str, Any]) -> None:
    CONNECTION_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONNECTION_STATE.with_suffix(CONNECTION_STATE.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(CONNECTION_STATE)


def _agent_connection_state(agent_id: str) -> dict[str, Any]:
    data = _load_connection_state()
    agents = data.setdefault("agents", {})
    row = agents.setdefault(agent_id, {})
    return row if isinstance(row, dict) else {}


def record_agent_connection(agent_id: str, **updates: Any) -> None:
    data = _load_connection_state()
    agents = data.setdefault("agents", {})
    row = agents.setdefault(agent_id, {})
    if not isinstance(row, dict):
        row = {}
        agents[agent_id] = row
    row.update({key: value for key, value in updates.items() if value is not None})
    row["updated_at"] = iso_now()
    _write_connection_state(data)


def _budget_float(value: str | float | int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise BridgeError(f"budget must be a number, got {value!r}") from exc


def _format_budget(value: str | float | int) -> str:
    number = _budget_float(value)
    text = f"{number:.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def calibrated_budget(agent_id: str, requested: str, *, enabled: bool = True) -> str:
    if not enabled:
        return requested
    row = _agent_connection_state(agent_id)
    stored = row.get("calibrated_budget_usd")
    if not stored:
        return requested
    try:
        return _format_budget(max(_budget_float(requested), _budget_float(stored)))
    except BridgeError:
        return requested


def next_budget(current: str, max_budget: str) -> str | None:
    current_value = _budget_float(current)
    max_value = _budget_float(max_budget)
    for candidate in BUDGET_RETRY_LADDER:
        if candidate > current_value + 0.000001:
            return _format_budget(candidate) if candidate <= max_value + 0.000001 else None
    doubled = current_value * 2
    return _format_budget(doubled) if doubled <= max_value + 0.000001 else None


def is_budget_error(output: str) -> bool:
    return "Exceeded USD budget" in output


def is_auth_error(output: str) -> bool:
    lowered = output.lower()
    return "failed to authenticate" in lowered or "invalid authentication credentials" in lowered or "401" in lowered


def _iter_prompt_heic_candidates(prompt: str) -> list[str]:
    candidates: list[str] = []
    for pattern in (QUOTED_HEIC_PATH_RE, UNQUOTED_HEIC_PATH_RE):
        for match in pattern.finditer(prompt):
            candidates.append(match.group("path"))
    return candidates


def _resolve_prompt_path(raw_path: str, *, project_dir: Path) -> Path:
    text = raw_path.strip()
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path.resolve()


def discover_prompt_heic_inputs(prompt: str, *, project_dir: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for raw_path in _iter_prompt_heic_candidates(prompt):
        path = _resolve_prompt_path(raw_path, project_dir=project_dir)
        if path.suffix.lower() not in HEIC_SUFFIXES or not path.exists() or not path.is_file():
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            found.append(path)
    return found


def _media_cache_dir(project_dir: Path) -> Path:
    digest = hashlib.sha256(str(project_dir).encode("utf-8")).hexdigest()[:12]
    return MEDIA_DIR / f"{safe_fragment(project_dir.name)}-{digest}"


def _converted_media_path(source: Path, *, project_dir: Path) -> Path:
    stat = source.stat()
    key = f"{source}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()[:12]
    return _media_cache_dir(project_dir) / f"{safe_fragment(source.stem)}-{digest}.png"


def _heic_converter_command(source: Path, output: Path) -> list[str]:
    override = os.environ.get("AGENT_BRIDGE_HEIC_CONVERTER")
    if override:
        return [*shlex.split(override), str(source), str(output)]

    if platform.system() == "Darwin":
        sips = shutil.which("sips")
        if sips:
            return [sips, "-s", "format", "png", str(source), "--out", str(output)]

    magick = shutil.which("magick")
    if magick:
        return [magick, str(source), str(output)]

    convert = shutil.which("convert")
    if convert:
        return [convert, str(source), str(output)]

    raise BridgeError("no HEIC converter found; install ImageMagick or use macOS sips")


def convert_heic_to_png(source: Path, *, project_dir: Path) -> Path:
    output = _converted_media_path(source, project_dir=project_dir)
    if output.exists() and output.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f"{output.stem}.tmp{output.suffix}")
    if tmp.exists():
        tmp.unlink()
    cmd = _heic_converter_command(source, tmp)
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise BridgeError(detail or f"converter exited {exc.returncode}") from exc
    except OSError as exc:
        raise BridgeError(str(exc)) from exc

    if not tmp.exists():
        raise BridgeError("converter did not produce an output file")
    tmp.replace(output)
    return output


def prepare_prompt_media(prompt: str, *, project_dir: Path) -> PromptMedia:
    inputs = discover_prompt_heic_inputs(prompt, project_dir=project_dir)
    if not inputs:
        return PromptMedia(prompt=prompt, media_dirs=[], conversions=[], failures=[])

    conversions: list[tuple[Path, Path]] = []
    failures: list[tuple[Path, str]] = []
    for source in inputs:
        try:
            conversions.append((source, convert_heic_to_png(source, project_dir=project_dir)))
        except BridgeError as exc:
            failures.append((source, str(exc)))

    lines = ["[AGENT BRIDGE MEDIA]"]
    if conversions:
        lines.append("Converted HEIC/HEIF inputs to PNG for agent compatibility:")
        lines.extend(f"- {source} -> {output}" for source, output in conversions)
        lines.append("Use the PNG path if the target agent cannot decode HEIC/HEIF directly.")
    if failures:
        lines.append("Could not convert these HEIC/HEIF inputs:")
        lines.extend(f"- {source}: {reason}" for source, reason in failures)

    media_dirs = sorted({output.parent for _, output in conversions}, key=str)
    return PromptMedia(
        prompt=f"{prompt.rstrip()}\n\n" + "\n".join(lines) + "\n",
        media_dirs=media_dirs,
        conversions=conversions,
        failures=failures,
    )


IMPLEMENTATION_TERMS = {
    "add",
    "build",
    "change",
    "create",
    "fix",
    "implement",
    "refactor",
    "update",
}
COMPLEXITY_TERMS = {
    "api",
    "backwards compatible",
    "compatibility",
    "concurrency",
    "controller",
    "migration",
    "schema",
    "security",
    "trace",
    "workflow",
}
REVIEW_ONLY_TERMS = {
    "assess",
    "audit",
    "check",
    "inspect",
    "review",
    "smoke",
    "summarize",
}
VAGUE_TERMS = {
    "quick",
    "basic",
    "maybe",
    "thing",
    "this",
    "unclear",
}
FILE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".json", ".md", ".toml", ".yaml", ".yml", ".sh")


def _contains_any(text: str, terms: set[str], words: set[str]) -> bool:
    return any((term in text if " " in term else term in words) for term in terms)


def assess_spawn_decision(prompt: str, *, policy: str, max_turns: int) -> SpawnDecision:
    if policy == "full":
        return SpawnDecision("full_loop", 999, ["forced full loop by --spawn-policy full"])
    if policy == "adversarial-only":
        return SpawnDecision("adversarial_only", 0, ["forced single adversarial review by --spawn-policy adversarial-only"])

    text = " ".join(prompt.lower().split())
    words = text.split()
    word_set = {word.strip("`'\"(),:;.") for word in words}
    score = 0
    reasons: list[str] = []

    has_impl = _contains_any(text, IMPLEMENTATION_TERMS, word_set)
    has_review_only = _contains_any(text, REVIEW_ONLY_TERMS, word_set) and not has_impl
    has_path = any(token.strip("`'\"(),:;").endswith(FILE_SUFFIXES) or "/" in token for token in words)

    if has_impl:
        score += 2
        reasons.append("implementation verb present")
    if len(words) >= 35:
        score += 1
        reasons.append("prompt has enough detail")
    if has_path:
        score += 1
        reasons.append("concrete file or path scope present")
    if _contains_any(text, COMPLEXITY_TERMS, word_set):
        score += 1
        reasons.append("complexity/risk signal present")
    if max_turns > 1:
        score += 1
        reasons.append("caller requested multiple turns")
    if "adversarial" in text or "red team" in text:
        score += 1
        reasons.append("adversarial validation requested")

    if has_review_only:
        score -= 2
        reasons.append("review-only request")
    if len(words) < 12 or _contains_any(text, VAGUE_TERMS, word_set):
        score -= 1
        reasons.append("prompt is short or vague")

    if has_impl and score >= 4:
        return SpawnDecision("full_loop", score, reasons)
    if not reasons:
        reasons.append("insufficient shape/depth signals")
    return SpawnDecision("adversarial_only", score, reasons)


def build_scope(source: str, target: dict[str, Any], mode: str, meta: dict[str, Any] | None = None) -> str:
    branch = run_git(["branch", "--show-current"]) or "unknown"
    head = run_git(["rev-parse", "--short", "HEAD"]) or "unknown"
    status = run_git(["status", "--short", "--branch"]) or "unknown"
    target_label = target.get("label", target["id"])
    action = "edit local files and run local tests" if mode == "code" else "return analysis only"
    no_edit = "" if mode == "code" else " Do not modify files."
    meta = meta or {}
    correlation = format_meta(meta) or "none"
    return f"""[AGENT CODE BRIDGE - {mode.upper()}]
You are {target_label}, invoked headlessly by {source} through a generic local agent bridge.

Project: {PROJECT_DIR}
Branch: {branch}
HEAD: {head}
Correlation: {correlation}
Git status at dispatch:
{status}

Task contract: {action}.{no_edit}

Hard limits: no live Domino actions, no credential use, no deploy, no teardown, no browser
automation unless explicitly requested for local UI verification, no direct GitHub push, and no
secrets. Keep changes scoped to this worktree, preserve the repo's generic/de-identified
positioning, and report files changed plus verification performed.
"""


def fill_template(parts: list[str], values: dict[str, str]) -> list[str]:
    return [part.format(**values) for part in parts]


def command_for_agent(
    agent: dict[str, Any],
    *,
    source: str,
    mode: str,
    prompt: str,
    scope: str,
    budget_usd: str,
    media_dirs: list[Path] | None = None,
) -> list[str]:
    command = resolve_command(agent)
    adapter = agent.get("adapter")
    media_dirs = media_dirs or []
    if adapter == "claude_code":
        permission_mode = "acceptEdits" if mode == "code" else "auto"
        cmd = [
            command,
            "-p",
            prompt,
            "--append-system-prompt",
            scope,
            "--add-dir",
            str(PROJECT_DIR),
            "--permission-mode",
            permission_mode,
            "--max-budget-usd",
            budget_usd,
            "--output-format",
            "text",
        ]
        for media_dir in media_dirs:
            cmd.extend(["--add-dir", str(media_dir)])
        if mode == "review":
            cmd.extend(["--allowedTools", "Read,Grep,Glob"])
        return cmd
    if adapter == "codex_exec":
        sandbox = "workspace-write" if mode == "code" else "read-only"
        combined_prompt = f"{scope}\n\n[BRIDGE REQUEST FROM {source}]\n{prompt}"
        return [
            command,
            "exec",
            combined_prompt,
            "-C",
            str(PROJECT_DIR),
            "-s",
            sandbox,
        ]
    if adapter == "gemini_cli":
        approval_mode = "yolo" if mode == "code" else "plan"
        combined_prompt = f"{scope}\n\n[BRIDGE REQUEST FROM {source}]\n{prompt}"
        cmd = [
            command,
            "-p",
            combined_prompt,
            "--approval-mode",
            approval_mode,
            "--output-format",
            "text",
            "--skip-trust",
        ]
        for d in [str(PROJECT_DIR)] + [str(m) for m in media_dirs]:
            cmd.extend(["--include-directories", d])
        return cmd
    if adapter == "argv":
        templates = agent.get(f"{mode}_args") or agent.get("args")
        if not isinstance(templates, list):
            raise BridgeError(f"agent {agent['id']} adapter=argv needs args or {mode}_args")
        values = {
            "prompt": prompt,
            "scope": scope,
            "project_dir": str(PROJECT_DIR),
            "mode": mode,
            "source": source,
            "target": agent["id"],
            "budget_usd": budget_usd,
            "media_dirs": os.pathsep.join(str(path) for path in media_dirs),
        }
        return [command, *fill_template([str(part) for part in templates], values)]
    raise BridgeError(f"agent {agent['id']} has unsupported adapter {adapter!r}")


def write_header(
    transcript: Path,
    *,
    source: str,
    target: str,
    mode: str,
    prompt: str,
    cmd: list[str],
    meta: dict[str, Any] | None = None,
) -> None:
    safe_cmd = [cmd[0], *("<prompt/scope>" if part.startswith("[AGENT CODE BRIDGE") else part for part in cmd[1:])]
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(f"=== Agent bridge request {utc_stamp()} ===\n")
        handle.write(f"project: {PROJECT_DIR}\nsource: {source}\ntarget: {target}\nmode: {mode}\n")
        if meta:
            handle.write(f"correlation: {format_meta(meta)}\n")
        handle.write(f"command: {shlex.join(safe_cmd)}\n\n")
        handle.write(prompt)
        handle.write("\n\n=== Agent response ===\n")


def _invoke_target_once(
    agent: dict[str, Any],
    *,
    source: str,
    mode: str,
    prompt: str,
    budget_usd: str,
    dry_run: bool,
    meta: dict[str, Any] | None = None,
    media_dirs: list[Path] | None = None,
) -> AgentRunResult:
    meta = meta or {}
    scope = build_scope(source, agent, mode, meta)
    cmd = command_for_agent(
        agent,
        source=source,
        mode=mode,
        prompt=prompt,
        scope=scope,
        budget_usd=budget_usd,
        media_dirs=media_dirs,
    )
    emit_event(
        "agent.dispatched",
        run_id=meta.get("run_id"),
        meta=meta,
        data={"target": agent["id"], "mode": mode, "dry_run": dry_run, "project_dir": str(PROJECT_DIR)},
    )
    if dry_run:
        print(f"[dry-run] {agent['id']}: {shlex.join(cmd)}")
        emit_event(
            "agent.completed",
            run_id=meta.get("run_id"),
            meta=meta,
            data={"target": agent["id"], "mode": mode, "return_code": 0, "dry_run": True},
        )
        return AgentRunResult(return_code=0, output="")

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = safe_fragment(meta.get("run_id", agent["id"]))
    turn = safe_fragment(meta.get("turn_id", utc_stamp()))
    transcript = TRANSCRIPT_DIR / f"{prefix}_{turn}_{agent['id']}_{utc_stamp()}.txt"
    write_header(transcript, source=source, target=agent["id"], mode=mode, prompt=prompt, cmd=cmd, meta=meta)

    output: list[str] = []
    with transcript.open("a", encoding="utf-8") as transcript_handle, BRIDGE_LOG.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            output.append(line)
            transcript_handle.write(line)
            log_handle.write(line)
        rc = process.wait()
    emit_event(
        "agent.completed",
        run_id=meta.get("run_id"),
        meta=meta,
        data={"target": agent["id"], "mode": mode, "return_code": rc, "dry_run": False, "transcript": str(transcript)},
    )
    print(f"\n[transcript] {transcript}", file=sys.stderr)
    return AgentRunResult(return_code=rc, output="".join(output), transcript=transcript)


def invoke_target(
    agent: dict[str, Any],
    *,
    source: str,
    mode: str,
    prompt: str,
    budget_usd: str,
    dry_run: bool,
    meta: dict[str, Any] | None = None,
    media_dirs: list[Path] | None = None,
    budget_auto: bool = True,
    max_auto_budget_usd: str = DEFAULT_MAX_AUTO_BUDGET_USD,
) -> int:
    budget = calibrated_budget(agent["id"], budget_usd, enabled=budget_auto and not dry_run)
    if agent.get("id") == "claude" and not dry_run:
        preflight = claude_auth_preflight(resolve_command(agent))
        if preflight.get("ok") is not True:
            record_agent_connection("claude", last_status="auth_preflight_failed", last_error=preflight.get("status"))
            print(
                "[agent-bridge] claude: auth preflight failed "
                f"({preflight.get('status')}). Run `agent code repair --to claude --repair-auth` if interactive repair is needed.",
                file=sys.stderr,
            )
            return 1
    while True:
        result = _invoke_target_once(
            agent,
            source=source,
            mode=mode,
            prompt=prompt,
            budget_usd=budget,
            dry_run=dry_run,
            meta=meta,
            media_dirs=media_dirs,
        )
        if result.return_code == 0:
            if budget_auto and not dry_run:
                record_agent_connection(agent["id"], calibrated_budget_usd=_format_budget(budget), last_status="ok")
            return 0
        if is_auth_error(result.output):
            record_agent_connection(agent["id"], last_status="auth_failed", last_error="auth")
            print(
                f"[agent-bridge] {agent['id']}: authentication failed. "
                f"Run `agent code repair --to {agent['id']} --repair-auth` to refresh credentials.",
                file=sys.stderr,
            )
            return result.return_code
        if not budget_auto or dry_run or not is_budget_error(result.output):
            return result.return_code
        retry_budget = next_budget(budget, max_auto_budget_usd)
        if retry_budget is None:
            record_agent_connection(agent["id"], last_status="budget_failed", last_error="budget", calibrated_budget_usd=_format_budget(budget))
            print(
                f"[agent-bridge] {agent['id']}: budget {budget} was too low and "
                f"max auto budget {max_auto_budget_usd} was reached.",
                file=sys.stderr,
            )
            return result.return_code
        emit_event(
            "agent.budget_retry",
            run_id=(meta or {}).get("run_id"),
            meta=meta or {},
            data={"target": agent["id"], "from_budget_usd": _format_budget(budget), "to_budget_usd": retry_budget},
        )
        print(f"[agent-bridge] {agent['id']}: budget {budget} was too low; retrying with {retry_budget}", file=sys.stderr)
        budget = retry_budget


def parse_bridge_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent code bridge",
        description="Invoke one or more configured local coding agents through a generic bridge.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to bridge agent config JSON")
    parser.add_argument("--project-dir", help="Project/worktree directory. Defaults to the current git root.")
    parser.add_argument("--from", dest="source", help="Calling agent or instance, e.g. codex, claude, human")
    parser.add_argument("--to", dest="targets", help="Target agent ids, numbers, comma list, or 'all'")
    parser.add_argument("--mode", choices=["review", "code"], help="Bridge mode")
    parser.add_argument("--prompt", help="Task prompt. If omitted in non-interactive mode, stdin is used.")
    parser.add_argument("--budget-usd", default=os.environ.get("AGENT_BRIDGE_BUDGET_USD", DEFAULT_BUDGET_USD))
    parser.add_argument("--no-budget-auto", action="store_true", help="Disable automatic budget retry/calibration")
    parser.add_argument(
        "--max-auto-budget-usd",
        default=os.environ.get("AGENT_BRIDGE_MAX_AUTO_BUDGET_USD", DEFAULT_MAX_AUTO_BUDGET_USD),
        help="Maximum budget cap Agent Bridge may use when retrying budget failures.",
    )
    parser.add_argument("--list", action="store_true", help="List configured agents and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print target commands without invoking agents")
    add_meta_args(parser)
    return parser.parse_args(argv)


def bridge(argv: list[str]) -> int:
    global PROJECT_DIR
    args = parse_bridge_args(argv)
    PROJECT_DIR = Path(args.project_dir).expanduser().resolve() if args.project_dir else discover_project_dir()
    config = load_config(Path(args.config))
    agents = agent_map(config)
    if args.list:
        print_agent_list(agents)
        return 0

    args = interactive_options(args, agents)
    source = args.source or "human"
    mode = args.mode or "review"
    if mode not in {"review", "code"}:
        raise BridgeError("mode must be review or code")
    prompt = read_prompt(args)
    if not prompt:
        raise BridgeError("a task prompt is required")
    if not args.targets:
        raise BridgeError("at least one target agent is required")
    media = prepare_prompt_media(prompt, project_dir=PROJECT_DIR)
    prompt = media.prompt

    targets = resolve_agent_ids(args.targets, agents)
    base_meta = ensure_run_meta(extract_meta(args))
    emit_event(
        "run.created",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={"command": "bridge", "source": source, "mode": mode, "targets": targets, "dry_run": args.dry_run},
    )
    rc = 0
    for target_id in targets:
        target_meta = child_turn_meta(
            base_meta,
            role=target_id,
            attempt=int(base_meta.get("attempt", 1)),
            parent_id=base_meta.get("parent_id"),
        )
        target_rc = invoke_target(
            agents[target_id],
            source=source,
            mode=mode,
            prompt=prompt,
            budget_usd=str(args.budget_usd),
            dry_run=args.dry_run,
            meta=target_meta,
            media_dirs=media.media_dirs,
            budget_auto=not args.no_budget_auto,
            max_auto_budget_usd=str(args.max_auto_budget_usd),
        )
        if target_rc != 0:
            rc = target_rc
    emit_event(
        "run.completed",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={"command": "bridge", "return_code": rc, "dry_run": args.dry_run},
    )
    return rc


def _json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _git_output(repo: Path, args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def skills_vault_repo(repo: str | None = None) -> Path:
    return Path(repo).expanduser().resolve() if repo else SKILLS_VAULT_DEFAULT.resolve()


def skills_vault_git_status(repo: Path) -> dict[str, Any]:
    if not (repo / ".git").exists():
        return {"path": str(repo), "exists": repo.exists(), "git": False}
    status = _git_output(repo, ["status", "--short"])
    return {
        "path": str(repo),
        "exists": True,
        "git": True,
        "branch": _git_output(repo, ["branch", "--show-current"]),
        "commit": _git_output(repo, ["rev-parse", "HEAD"]),
        "short_commit": _git_output(repo, ["rev-parse", "--short", "HEAD"]),
        "dirty": bool(status),
        "status": status,
    }


def load_skills_registry(repo: Path) -> dict[str, Any]:
    registry_path = repo / "registry" / "skills.json"
    if not registry_path.exists():
        raise BridgeError(f"missing skills registry: {registry_path}")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"{registry_path} is not valid JSON: {exc}") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("skills"), list):
        raise BridgeError(f"{registry_path} must contain a skills list")
    return registry


def validate_skills_registry(repo: Path) -> list[str]:
    registry = load_skills_registry(repo)
    errors: list[str] = []
    seen: set[str] = set()
    required = {
        "id",
        "aliases",
        "description",
        "source_paths",
        "harnesses",
        "platforms",
        "install_mode",
        "dependencies",
        "sensitivity",
        "status",
        "content_hash",
    }
    for entry in registry.get("skills", []):
        if not isinstance(entry, dict):
            errors.append("skill entry must be an object")
            continue
        skill_id = entry.get("id")
        if not isinstance(skill_id, str) or not skill_id:
            errors.append("skill entry missing id")
            continue
        if skill_id in seen:
            errors.append(f"{skill_id}: duplicate id")
        seen.add(skill_id)
        missing = sorted(required - set(entry))
        if missing:
            errors.append(f"{skill_id}: missing {', '.join(missing)}")
        if entry.get("status") not in {"active", "inactive", "deprecated"}:
            errors.append(f"{skill_id}: unsupported status {entry.get('status')!r}")
        skill_dir = repo / "skills" / skill_id
        if entry.get("status") == "active" and not (skill_dir / "SKILL.md").exists():
            errors.append(f"{skill_id}: missing SKILL.md")
    return errors


def client_skill_roots(client: str) -> list[Path]:
    home = Path.home()
    if client == "codex":
        return [home / ".codex" / "skills"]
    if client == "claude":
        return [home / ".claude" / "skills"]
    if client == "agents":
        return [home / ".agents" / "skills"]
    if client == "grok":
        return [home / ".grok" / "skills"]
    if client == "all":
        roots: list[Path] = []
        for item in SKILLS_CLIENTS:
            roots.extend(client_skill_roots(item))
        return roots
    raise BridgeError(f"unsupported skills client {client!r}")


def active_registry_skills(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        entry
        for entry in registry.get("skills", [])
        if isinstance(entry, dict) and entry.get("status") == "active" and entry.get("install_mode") == "symlink"
    ]


def ensure_skill_symlink(link_path: Path, target: Path, *, dry_run: bool = False) -> dict[str, str]:
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink():
            try:
                if link_path.resolve() == target.resolve():
                    return {"path": str(link_path), "target": str(target), "status": "already linked"}
            except OSError:
                pass
            if dry_run:
                return {"path": str(link_path), "target": str(target), "status": "would relink"}
            link_path.unlink()
            os.symlink(target, link_path, target_is_directory=True)
            return {"path": str(link_path), "target": str(target), "status": "relinked"}
        return {"path": str(link_path), "target": str(target), "status": "exists; left unchanged"}
    if dry_run:
        return {"path": str(link_path), "target": str(target), "status": "would link"}
    link_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link_path, target_is_directory=True)
    return {"path": str(link_path), "target": str(target), "status": "linked"}


def install_vault_skills(repo: Path, *, client: str, dry_run: bool = False) -> dict[str, Any]:
    registry = load_skills_registry(repo)
    roots = client_skill_roots(client)
    results = []
    for root in roots:
        root_client = root.parent.name.lstrip(".")
        for entry in active_registry_skills(registry):
            skill_id = str(entry["id"])
            target = repo / "skills" / skill_id
            if not target.exists():
                results.append({"client": root_client, "skill": skill_id, "path": str(root / skill_id), "status": "missing target"})
                continue
            link = ensure_skill_symlink(root / skill_id, target, dry_run=dry_run)
            link.update({"client": root_client, "skill": skill_id})
            results.append(link)
    return {
        "repo": str(repo),
        "dry_run": dry_run,
        "skills": len(active_registry_skills(registry)),
        "roots": [str(root) for root in roots],
        "results": results,
    }


def skills_install_status(repo: Path, *, client: str) -> dict[str, Any]:
    registry = load_skills_registry(repo)
    roots = client_skill_roots(client)
    rows = []
    for root in roots:
        root_client = root.parent.name.lstrip(".")
        for entry in active_registry_skills(registry):
            skill_id = str(entry["id"])
            target = repo / "skills" / skill_id
            link = root / skill_id
            if link.is_symlink():
                try:
                    status = "linked" if link.resolve() == target.resolve() else "legacy symlink"
                except OSError:
                    status = "broken symlink"
            elif link.exists():
                status = "real path conflict"
            else:
                status = "missing"
            rows.append({"client": root_client, "skill": skill_id, "path": str(link), "target": str(target), "status": status})
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"repo": str(repo), "vault": skills_vault_git_status(repo), "counts": counts, "results": rows}


def format_skills_status(data: dict[str, Any]) -> str:
    lines = [f"Skills vault: {data['repo']}", f"Commit: {data.get('vault', {}).get('short_commit', '')}", "Status counts:"]
    for key, value in sorted(data.get("counts", {}).items()):
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def _comma_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _hook_agent_command(client: str) -> str:
    agent_bin = os.environ.get("AGENT_BRIDGE_HOOK_AGENT", "/Users/tts/.local/bin/agent")
    if agent_bin.lower().endswith((".cmd", ".bat")) or "\\" in agent_bin:
        return f'cmd /d /c ""{agent_bin}" code hook session-start --client {client}"'
    return f"'{agent_bin}' code hook session-start --client {client}"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path.expanduser())
        if key not in seen:
            seen.add(key)
            out.append(path.expanduser())
    return out


def _env_path_candidates(*names: str) -> list[Path]:
    paths: list[Path] = []
    for name in names:
        value = os.environ.get(name)
        if not value:
            continue
        for part in value.split(os.pathsep):
            if part.strip():
                paths.append(Path(part.strip()))
    return paths


def shared_skills_root_candidates() -> list[Path]:
    home = Path.home()
    candidates = _env_path_candidates(
        "AGENT_BRIDGE_SHARED_SKILLS_ROOT",
        "SHARED_AGENT_SKILLS_ROOT",
        "CAREER_SHARED_SKILLS_ROOT",
    )
    for name in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value) / "SharedAgentSkills")
    candidates.extend(
        [
            home / "Library" / "CloudStorage" / "OneDrive-nextcz.com" / "SharedAgentSkills",
            home / "Library" / "CloudStorage" / "OneDrive-Personal" / "SharedAgentSkills",
            home / "OneDrive - Next Cz" / "SharedAgentSkills",
            home / "OneDrive" / "SharedAgentSkills",
        ]
    )
    return _dedupe_paths(candidates)


def resolve_shared_skills_root(
    root: str | None = None,
    *,
    create: bool = False,
    required: bool = True,
    require_bridge_dir: bool = False,
) -> Path | None:
    if root:
        resolved = Path(root).expanduser()
        if create:
            resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    env_candidates = _env_path_candidates(
        "AGENT_BRIDGE_SHARED_SKILLS_ROOT",
        "SHARED_AGENT_SKILLS_ROOT",
        "CAREER_SHARED_SKILLS_ROOT",
    )
    if create and env_candidates:
        env_candidates[0].mkdir(parents=True, exist_ok=True)
        return env_candidates[0]

    candidates = shared_skills_root_candidates()
    for candidate in candidates:
        if (candidate / SHARED_BRIDGE_DIR_NAME).exists():
            return candidate
    if not require_bridge_dir:
        for candidate in candidates:
            if candidate.exists():
                return candidate
    if create and candidates:
        candidates[0].mkdir(parents=True, exist_ok=True)
        return candidates[0]
    if required:
        searched = ", ".join(str(path) for path in candidates)
        raise BridgeError(f"could not find a shared AgentSkills root; searched: {searched}")
    return None


def shared_bridge_dir(root: str | None = None, *, create: bool = False, required: bool = True) -> Path | None:
    skills_root = resolve_shared_skills_root(root, create=create, required=required, require_bridge_dir=not create)
    if skills_root is None:
        return None
    bridge_dir = skills_root / SHARED_BRIDGE_DIR_NAME
    if create:
        bridge_dir.mkdir(parents=True, exist_ok=True)
    return bridge_dir


def _git_root_for_path(path: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _harness_machine_id() -> str:
    explicit = os.environ.get("AGENT_BRIDGE_MACHINE_ID")
    if explicit:
        return safe_fragment(explicit)
    return safe_fragment(f"{getpass.getuser()}@{socket.gethostname()}")


def register_harness(client: str, *, root: str | None = None, status: str = "active") -> dict[str, Any]:
    bridge_dir = shared_bridge_dir(root, create=True)
    assert bridge_dir is not None
    registry_dir = bridge_dir / SHARED_REGISTRY_DIR_NAME
    registry_dir.mkdir(parents=True, exist_ok=True)

    cwd = os.environ.get("PWD") or str(Path.cwd())
    client_id = safe_fragment(client)
    machine_id = _harness_machine_id()
    path = registry_dir / f"{machine_id}.{client_id}.json"
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "updated_at": iso_now(),
        "status": status,
        "client": client,
        "machine_id": machine_id,
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd": cwd,
        "git_root": _git_root_for_path(cwd),
        "agent_command": shutil.which("agent") or os.environ.get("AGENT_BRIDGE_HOOK_AGENT") or "",
        "bridge_repo": str(BRIDGE_DIR.parent),
        "mailbox_mcp": str(BRIDGE_DIR / "mailbox_mcp.py"),
        "state_dir": str(STATE_DIR),
        "shared_bridge_dir": str(bridge_dir),
        "registry_file": str(path),
    }
    vault_status = skills_vault_git_status(SKILLS_VAULT_DEFAULT)
    if vault_status.get("exists"):
        record["skills_vault"] = vault_status
    if client == "claude":
        auth_state = cached_claude_auth_state()
        if auth_state:
            record["claude_auth"] = {
                "ok": auth_state.get("ok"),
                "status": auth_state.get("status"),
                "checked_at": auth_state.get("checked_at"),
                "cached": True,
            }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return record


def maybe_register_harness(client: str) -> dict[str, Any] | None:
    if os.environ.get("AGENT_BRIDGE_DISABLE_SHARED_REGISTRY") in {"1", "true", "TRUE", "yes"}:
        return None
    root = resolve_shared_skills_root(required=False, require_bridge_dir=True)
    if root is None:
        return None
    try:
        return register_harness(client, root=str(root))
    except OSError:
        return None


def _parse_iso_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_harness_registry(root: str | None = None, *, stale_minutes: int = 1440) -> dict[str, Any]:
    bridge_dir = shared_bridge_dir(root, create=False)
    assert bridge_dir is not None
    registry_dir = bridge_dir / SHARED_REGISTRY_DIR_NAME
    now = dt.datetime.now(dt.timezone.utc)
    rows: list[dict[str, Any]] = []
    if registry_dir.exists():
        for path in sorted(registry_dir.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                row = {"client": "unknown", "machine_id": path.stem, "status": "invalid", "error": str(exc)}
            updated = _parse_iso_timestamp(row.get("updated_at"))
            age_seconds = int((now - updated).total_seconds()) if updated else None
            row["registry_file"] = str(path)
            row["age_seconds"] = age_seconds
            row["fresh"] = bool(age_seconds is not None and age_seconds <= stale_minutes * 60 and row.get("status") == "active")
            rows.append(row)
    return {
        "shared_bridge_dir": str(bridge_dir),
        "registry_dir": str(registry_dir),
        "stale_minutes": stale_minutes,
        "harnesses": rows,
    }


def format_harness_registry(data: dict[str, Any]) -> str:
    lines = [
        f"Shared Agent Bridge: {data['shared_bridge_dir']}",
        f"Registry: {data['registry_dir']}",
        f"Stale after: {data['stale_minutes']} minutes",
        "",
    ]
    rows = data.get("harnesses", [])
    if not rows:
        lines.append("(no harness registrations found)")
        return "\n".join(lines) + "\n"
    lines.append("fresh\tclient\tmachine\tstatus\tupdated_at\tgit_root")
    for row in rows:
        fresh = "yes" if row.get("fresh") else "no"
        lines.append(
            "\t".join(
                [
                    fresh,
                    str(row.get("client", "")),
                    str(row.get("machine_id", "")),
                    str(row.get("status", "")),
                    str(row.get("updated_at", "")),
                    str(row.get("git_root", "")),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def _run_capture(cmd: list[str], *, cwd: Path | None = None, timeout: int = 60) -> AgentRunResult:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        return AgentRunResult(return_code=proc.returncode, output=proc.stdout or "")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return AgentRunResult(return_code=124, output=output or f"timed out after {timeout}s")


def _state_is_fresh(row: dict[str, Any], ttl_seconds: int) -> bool:
    checked = _parse_iso_timestamp(row.get("checked_at"))
    if checked is None:
        return False
    age = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds()
    return age <= ttl_seconds


def _credential_presence() -> dict[str, Any]:
    if platform.system() == "Darwin" and shutil.which("security"):
        result = _run_capture(["security", "find-generic-password", "-s", "Claude Code-credentials"], timeout=10)
        return {"backend": "macos-keychain", "present": result.return_code == 0}
    path = Path.home() / ".claude" / ".credentials.json"
    return {"backend": "file", "present": path.exists(), "path": str(path)}


def _run_claude_ready_probe(command: str) -> AgentRunResult:
    prompt = "Reply with exactly READY"
    primary = _run_capture([command, "-p", prompt, "--max-turns", "1"], timeout=20)
    if primary.return_code == 0 or "unknown option" not in primary.output.lower():
        return primary
    return _run_capture([command, "-p", prompt, "--max-budget-usd", "0.20", "--output-format", "text"], timeout=20)


def claude_auth_preflight(command: str, *, refresh: bool = False, ttl_seconds: int = 21600) -> dict[str, Any]:
    if os.environ.get("AGENT_BRIDGE_AUTH_PREFLIGHT") in {"0", "false", "FALSE", "no"}:
        return {"status": "skipped", "ok": True, "checked_at": iso_now(), "reason": "disabled by AGENT_BRIDGE_AUTH_PREFLIGHT"}
    cached = _read_json_file(CLAUDE_AUTH_STATE)
    if not refresh and cached.get("ok") is True and _state_is_fresh(cached, ttl_seconds):
        cached["cached"] = True
        return cached

    poisoned_env = [name for name in ANTHROPIC_ENV_NAMES if os.environ.get(name)]
    credential = _credential_presence()
    version = _run_capture([command, "--version"], timeout=10)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "checked_at": iso_now(),
        "method": "claude-ready-probe",
        "command": command,
        "version": (version.output or "").strip().splitlines()[:1],
        "credential": credential,
        "poisoned_env": poisoned_env,
        "cached": False,
    }
    if poisoned_env:
        payload.update({"ok": False, "status": "env_misconfigured", "error": "ANTHROPIC_* environment variables are set"})
        _write_json_file(CLAUDE_AUTH_STATE, payload)
        return payload
    probe = _run_claude_ready_probe(command)
    ok = probe.return_code == 0 and "READY" in probe.output
    payload.update(
        {
            "ok": ok,
            "status": "ok" if ok else "probe_failed",
            "return_code": probe.return_code,
            "error": "" if ok else (probe.output or "")[:500],
        }
    )
    _write_json_file(CLAUDE_AUTH_STATE, payload)
    return payload


def cached_claude_auth_state() -> dict[str, Any] | None:
    data = _read_json_file(CLAUDE_AUTH_STATE)
    return data or None


def _claude_auth_status(command: str) -> tuple[dict[str, Any] | None, str]:
    result = _run_capture([command, "auth", "status"], timeout=30)
    if result.return_code != 0:
        return None, result.output
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return None, result.output
    return payload if isinstance(payload, dict) else None, result.output


def _run_auth_refresh(command: str, *, email: str | None, sso: bool, timeout: int) -> AgentRunResult:
    _run_capture([command, "auth", "logout"], timeout=30)
    cmd = [command, "auth", "login", "--claudeai"]
    if email:
        cmd.extend(["--email", email])
    if sso:
        cmd.append("--sso")
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_DIR), text=True, timeout=timeout, check=False)
        return AgentRunResult(return_code=proc.returncode, output="")
    except subprocess.TimeoutExpired as exc:
        return AgentRunResult(return_code=124, output=(exc.stdout or "") + (exc.stderr or "") or f"timed out after {timeout}s")


def _budgeted_probe(
    *,
    agent: dict[str, Any],
    prompt: str,
    expected: str,
    budget_usd: str,
    max_auto_budget_usd: str,
) -> ProbeResult:
    command = resolve_command(agent)
    adapter = agent.get("adapter")
    budget = calibrated_budget(agent["id"], budget_usd, enabled=True)
    while True:
        if adapter == "gemini_cli":
            probe_cmd = [command, "-p", prompt, "--output-format", "text", "--skip-trust"]
        else:
            probe_cmd = [command, "-p", prompt, "--max-budget-usd", budget, "--output-format", "text"]
        result = _run_capture(probe_cmd, timeout=120)
        if result.return_code == 0 and expected in result.output:
            record_agent_connection(agent["id"], direct_budget_usd=_format_budget(budget), last_direct_status="ok")
            return ProbeResult(ok=True, budget_usd=_format_budget(budget), output=result.output)
        if not is_budget_error(result.output):
            return ProbeResult(ok=False, budget_usd=_format_budget(budget), output=result.output)
        retry_budget = next_budget(budget, max_auto_budget_usd)
        if retry_budget is None:
            return ProbeResult(ok=False, budget_usd=_format_budget(budget), output=result.output)
        print(f"[agent-bridge] {agent['id']}: direct probe budget {budget} was too low; retrying with {retry_budget}", file=sys.stderr)
        budget = retry_budget


def _repair_claude(
    agent: dict[str, Any],
    *,
    source: str,
    email: str | None,
    sso: bool,
    repair_auth: bool,
    budget_usd: str,
    max_auto_budget_usd: str,
    auth_timeout: int,
    dry_run: bool,
) -> dict[str, Any]:
    command = resolve_command(agent)
    status, status_output = _claude_auth_status(command)
    status_email = status.get("email") if status else None
    print(f"claude auth status: {'ok' if status else 'failed'}")
    if status_email:
        print(f"claude account: {status_email}")

    direct = _budgeted_probe(
        agent=agent,
        prompt="Reply exactly: CLAUDE_DIRECT_OK",
        expected="CLAUDE_DIRECT_OK",
        budget_usd=budget_usd,
        max_auto_budget_usd=max_auto_budget_usd,
    )
    repaired_auth = False
    if not direct.ok and is_auth_error(direct.output) and repair_auth:
        repair_email = email or status_email
        print("claude direct probe failed auth; refreshing Claude login...")
        if dry_run:
            return {"target": agent["id"], "status": "would_repair_auth", "email": repair_email or ""}
        refresh = _run_auth_refresh(command, email=repair_email, sso=sso, timeout=auth_timeout)
        if refresh.return_code != 0:
            record_agent_connection(agent["id"], last_status="auth_repair_failed", last_error=refresh.output)
            return {"target": agent["id"], "status": "auth_repair_failed", "output": refresh.output}
        repaired_auth = True
        status, status_output = _claude_auth_status(command)
        direct = _budgeted_probe(
            agent=agent,
            prompt="Reply exactly: CLAUDE_DIRECT_OK",
            expected="CLAUDE_DIRECT_OK",
            budget_usd=budget_usd,
            max_auto_budget_usd=max_auto_budget_usd,
        )

    if not direct.ok:
        record_agent_connection(agent["id"], last_status="direct_probe_failed", last_error=direct.output)
        print(direct.output, end="" if direct.output.endswith("\n") else "\n")
        return {
            "target": agent["id"],
            "status": "direct_probe_failed",
            "budget_usd": direct.budget_usd,
            "repaired_auth": repaired_auth,
        }

    print(f"claude direct probe: ok at budget {direct.budget_usd}")
    bridge_budget = direct.budget_usd
    rc = invoke_target(
        agent,
        source=source,
        mode="review",
        prompt="Liveness check only. Do not inspect files. Reply exactly: BRIDGE_REPAIR_OK",
        budget_usd=bridge_budget,
        dry_run=dry_run,
        budget_auto=True,
        max_auto_budget_usd=max_auto_budget_usd,
    )
    status_name = "ok" if rc == 0 else "bridge_probe_failed"
    record_agent_connection(agent["id"], last_status=status_name, repaired_auth=repaired_auth)
    return {
        "target": agent["id"],
        "status": status_name,
        "direct_budget_usd": direct.budget_usd,
        "repaired_auth": repaired_auth,
    }


def repair_cmd(argv: list[str]) -> int:
    global PROJECT_DIR
    parser = argparse.ArgumentParser(prog="agent code repair", description="Check and repair configured Agent Bridge target connections.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to bridge agent config JSON")
    parser.add_argument("--project-dir", help="Project/worktree directory. Defaults to the current git root.")
    parser.add_argument("--from", dest="source", default=os.environ.get("AGENT_BRIDGE_CALLER", "human"))
    parser.add_argument("--to", dest="targets", default="claude", help="Target agent ids, numbers, comma list, or 'all'")
    parser.add_argument(
        "--email",
        default=os.environ.get("AGENT_BRIDGE_CLAUDE_EMAIL"),
        help="Email hint for Claude login repair. Defaults to AGENT_BRIDGE_CLAUDE_EMAIL.",
    )
    parser.add_argument("--sso", action="store_true", help="Force Claude SSO login during auth repair.")
    parser.add_argument("--repair-auth", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--budget-usd", default=os.environ.get("AGENT_BRIDGE_REPAIR_BUDGET_USD", DEFAULT_REPAIR_BUDGET_USD))
    parser.add_argument(
        "--max-auto-budget-usd",
        default=os.environ.get("AGENT_BRIDGE_MAX_AUTO_BUDGET_USD", DEFAULT_MAX_AUTO_BUDGET_USD),
        help="Maximum budget cap Agent Bridge may use when calibrating probes.",
    )
    parser.add_argument("--auth-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    PROJECT_DIR = Path(args.project_dir).expanduser().resolve() if args.project_dir else discover_project_dir()
    agents = agent_map(load_config(Path(args.config)))
    targets = resolve_agent_ids(args.targets, agents)
    rows: list[dict[str, Any]] = []
    rc = 0
    for target_id in targets:
        agent = agents[target_id]
        if agent.get("adapter") == "claude_code":
            row = _repair_claude(
                agent,
                source=args.source,
                email=args.email,
                sso=args.sso,
                repair_auth=bool(args.repair_auth),
                budget_usd=str(args.budget_usd),
                max_auto_budget_usd=str(args.max_auto_budget_usd),
                auth_timeout=args.auth_timeout,
                dry_run=args.dry_run,
            )
        else:
            target_rc = invoke_target(
                agent,
                source=args.source,
                mode="review",
                prompt="Liveness check only. Reply exactly: BRIDGE_REPAIR_OK",
                budget_usd=str(args.budget_usd),
                dry_run=args.dry_run,
                budget_auto=True,
                max_auto_budget_usd=str(args.max_auto_budget_usd),
            )
            row = {"target": target_id, "status": "ok" if target_rc == 0 else "bridge_probe_failed"}
        rows.append(row)
        if row.get("status") != "ok":
            rc = 1
    return rc


def render_agent_bridge_skill() -> str:
    return f"""---
name: agent-bridge
description: Use when coordinating Codex, Claude, or other coding harnesses through Agent Bridge; checking shared OneDrive harness status; registering a harness heartbeat; using mailbox MCP; or invoking agent code bridge, loop, workflow, hooks, or harness commands across macOS and Windows machines.
---

# Agent Bridge

Use the installed `agent` command as the front door for local and cross-harness coordination. Prefer the global bridge checkout over project-local copies.

## Fast Checks

- Check shared machine and harness presence: `agent code harness status`
- Register the current harness manually: `agent code harness register --client <codex|claude|other>`
- Check local SessionStart hooks: `agent code hooks status --client both`
- List callable local engines: `agent code bridge --list`
- Inspect trace events: `agent code trace`

## Coordination

- Use `agent code bridge --from <caller> --to <target> --mode review --prompt "..."` for a bounded one-shot review or plan comparison.
- Use `agent code bridge --mode code` only for scoped implementation tasks with an explicit worktree.
- Use `agent code loop` for adversarial builder/critic/verifier loops; keep budgets explicit when cost matters.
- Use mailbox MCP for async handoffs. Mailbox messages are the durable proof path; shell process lifetime is secondary.

## Media Handling

- When a bridge or loop prompt references an existing `.heic` or `.heif` file path, Agent Bridge converts it to PNG under `{MEDIA_DIR}` and appends an `[AGENT BRIDGE MEDIA]` note with the converted path.
- Claude Code dispatches also receive the media cache through `--add-dir`; set `AGENT_BRIDGE_HEIC_CONVERTER` to override the default converter command.

## Connection Repair

- Use `AGENT_BRIDGE_CLAUDE_EMAIL=<email> agent code repair --to claude` when a target CLI reports stale auth, 401 credentials, or budget calibration trouble.
- Bridge and loop dispatches retry `Exceeded USD budget (...)` failures automatically up to `AGENT_BRIDGE_MAX_AUTO_BUDGET_USD` or `--max-auto-budget-usd`, then persist the working cap under `{CONNECTION_STATE}`.
- Use `--no-budget-auto` on bridge or loop calls when an explicit hard budget should fail instead of retrying.

## Shared Registry

The shared OneDrive package lives in a folder named `{SHARED_BRIDGE_DIR_NAME}` under the resolved `SharedAgentSkills` root. Each hooked harness writes one JSON heartbeat under `{SHARED_BRIDGE_DIR_NAME}/{SHARED_REGISTRY_DIR_NAME}/`.

Treat a fresh registry row as "this harness has recently started or resumed and can see the shared folder", not as proof that an existing UI chat is idle or ready to accept work. Use `agent code harness status --json` when another tool needs machine-readable status.

## Path Rules

- Resolve the shared skill root with `AGENT_BRIDGE_SHARED_SKILLS_ROOT`, then `SHARED_AGENT_SKILLS_ROOT`, then the platform OneDrive defaults.
- macOS default bridge repo: `/Users/tts/Code/agent-bridge`
- Windows default bridge repo: `%USERPROFILE%\\Code\\agent-bridge`
- MCP mailbox registrations should point to `agent_bridge/mailbox_mcp.py` in the global bridge repo, not a project-local copy.
"""


def _skill_link_paths(client: str) -> list[Path]:
    home = Path.home()
    if client == "codex":
        return [home / ".codex" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "claude":
        return [home / ".claude" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "gemini":
        return [home / ".gemini" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "agents":
        return [home / ".agents" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "all":
        return _skill_link_paths("codex") + _skill_link_paths("claude") + _skill_link_paths("gemini") + _skill_link_paths("agents")
    return []


def _ensure_skill_link(link_path: Path, target: Path) -> dict[str, str]:
    if link_path.exists() or link_path.is_symlink():
        try:
            if link_path.resolve() == target.resolve():
                return {"path": str(link_path), "status": "already linked"}
        except OSError:
            pass
        return {"path": str(link_path), "status": "exists; left unchanged"}
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return {"path": str(link_path), "status": "linked"}
    except OSError as exc:
        return {"path": str(link_path), "status": f"link failed: {exc}"}


def install_shared_skill(root: str | None = None, *, link_client: str = "all") -> dict[str, Any]:
    bridge_dir = shared_bridge_dir(root, create=True)
    assert bridge_dir is not None
    skill_path = bridge_dir / "SKILL.md"
    content = render_agent_bridge_skill()
    changed = not skill_path.exists() or skill_path.read_text(encoding="utf-8") != content
    if changed:
        skill_path.write_text(content, encoding="utf-8")
    links = [_ensure_skill_link(path, bridge_dir) for path in _skill_link_paths(link_client)]
    return {
        "shared_bridge_dir": str(bridge_dir),
        "skill_path": str(skill_path),
        "changed": changed,
        "links": links,
    }


def format_shared_skill_install(result: dict[str, Any]) -> str:
    lines = [
        f"Shared Agent Bridge: {result['shared_bridge_dir']}",
        f"Skill: {result['skill_path']} ({'updated' if result['changed'] else 'unchanged'})",
    ]
    for link in result.get("links", []):
        lines.append(f"{link['path']}: {link['status']}")
    return "\n".join(lines) + "\n"


def session_start_context(client: str, registration: dict[str, Any] | None = None) -> str:
    cwd = os.environ.get("PWD") or str(Path.cwd())
    git_root = _git_root_for_path(cwd)
    location = f" Current git root: {git_root}." if git_root else ""
    registry = ""
    if registration:
        registry = (
            " Shared registry heartbeat written to "
            f"`{registration['registry_file']}` for machine `{registration['machine_id']}`."
        )
        vault = registration.get("skills_vault")
        if isinstance(vault, dict) and vault.get("short_commit"):
            registry += f" Skills vault: `{vault.get('short_commit')}`{' dirty' if vault.get('dirty') else ''}."
        auth = registration.get("claude_auth")
        if isinstance(auth, dict):
            registry += f" Cached Claude auth: {auth.get('status')} at {auth.get('checked_at')}."
    return (
        "Agent Bridge session bootstrap: global command `agent` is available for bounded local "
        "agent coordination. Use `agent code bridge` for one-shot headless review/code turns and "
        "`agent code loop` for adversarial loops; loop dispatch defaults to `--spawn-policy auto`, "
        "which falls back to one analysis-only adversarial agent unless the task is concrete enough "
        "for a full builder/critic/verifier spawn. Mailbox MCP, when registered, should point to "
        f"`{BRIDGE_DIR / 'mailbox_mcp.py'}`. Use `agent code harness status` to inspect shared "
        "OneDrive harness registrations. Use `agent code skills status` to inspect Git-backed "
        "skill installs. This startup hook never spawns agents or performs Claude login. "
        f"Client: {client}."
        f"{location}{registry}"
    )


def hook_session_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code hook session-start", description="Emit SessionStart hook context.")
    parser.add_argument("--client", choices=["codex", "claude", "gemini"], required=True)
    parser.add_argument("--plain", action="store_true", help="Print plain context instead of hook JSON")
    args = parser.parse_args(argv)
    registration = maybe_register_harness(args.client)
    context = session_start_context(args.client, registration=registration)
    if args.plain:
        print(context)
    else:
        _json_print({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}})
    return 0


def _load_json_config(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise BridgeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BridgeError(f"{path} must contain a JSON object")
    return data


def _write_json_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{utc_stamp()}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _session_start_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise BridgeError("hooks must be a JSON object")
    entries = hooks.setdefault("SessionStart", [])
    if not isinstance(entries, list):
        raise BridgeError("hooks.SessionStart must be a list")
    return entries


def _ensure_command_hook(config: dict[str, Any], command: str) -> bool:
    entries = _session_start_entries(config)
    target_entry = None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("matcher") == "startup|resume" and isinstance(entry.get("hooks"), list):
            target_entry = entry
            break
    if target_entry is None:
        target_entry = {"matcher": "startup|resume", "hooks": []}
        entries.append(target_entry)
    hooks = target_entry["hooks"]
    for hook in hooks:
        if isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command:
            return False
    hooks.append({"type": "command", "command": command})
    return True


def _config_path(client: str) -> Path:
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "hooks.json"
    if client == "claude":
        return home / ".claude" / "settings.json"
    if client == "gemini":
        return home / ".gemini" / "settings.json"
    raise BridgeError(f"unsupported hook client {client!r}")


def install_session_hook(client: str) -> bool:
    if client == "gemini":
        # Gemini uses different hook events (BeforeAgent / AfterTool etc). Bridge context is
        # injected directly into headless prompts. Run `gemini` interactively for full setup.
        print("gemini: hooks use project/agent model (see `gemini hooks`). Bridge coordination injected via prompts.")
        return False
    path = _config_path(client)
    default = {"hooks": {}} if client == "codex" else {}
    config = _load_json_config(path, default)
    changed = _ensure_command_hook(config, _hook_agent_command(client))
    if changed:
        _write_json_config(path, config)
    return changed


def session_hook_installed(client: str) -> bool:
    if client == "gemini":
        return False  # different hook system; status reported at install time
    path = _config_path(client)
    if not path.exists():
        return False
    config = _load_json_config(path, {})
    command = _hook_agent_command(client)
    for entry in config.get("hooks", {}).get("SessionStart", []):
        for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
            if isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def hooks_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code hooks", description="Install or inspect Agent Bridge session hooks.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    install = sub.add_parser("install")
    install.add_argument("--client", choices=["codex", "claude", "gemini", "both"], default="both")
    status = sub.add_parser("status")
    status.add_argument("--client", choices=["codex", "claude", "gemini", "both"], default="both")
    args = parser.parse_args(argv)

    clients = ["codex", "claude", "gemini"] if args.client == "both" else [args.client]
    if args.cmd == "install":
        for client in clients:
            changed = install_session_hook(client)
            verb = "installed" if changed else "already installed"
            print(f"{client}: {verb} ({_config_path(client)})")
        return 0
    for client in clients:
        installed = session_hook_installed(client)
        print(f"{client}: {'installed' if installed else 'not installed'} ({_config_path(client)})")
    return 0


def harness_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code harness", description="Register and inspect shared Agent Bridge harness status.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    install = sub.add_parser("install-skill", help="Install the shared Agent Bridge skill package.")
    install.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    install.add_argument("--link-client", choices=["none", "codex", "claude", "agents", "all"], default="all")
    install.add_argument("--json", action="store_true")

    register = sub.add_parser("register", help="Write a shared registry heartbeat for this harness.")
    register.add_argument("--client", required=True, help="Harness/client name, e.g. codex, claude, cursor, aider.")
    register.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    register.add_argument("--status", default="active")
    register.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show shared Agent Bridge harness registry rows.")
    status.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    status.add_argument("--stale-minutes", type=int, default=1440)
    status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "install-skill":
        result = install_shared_skill(args.root, link_client=args.link_client)
        _json_print(result) if args.json else print(format_shared_skill_install(result), end="")
        return 0
    if args.cmd == "register":
        record = register_harness(args.client, root=args.root, status=args.status)
        _json_print(record) if args.json else print(f"{record['client']}: registered ({record['registry_file']})")
        return 0
    data = load_harness_registry(args.root, stale_minutes=args.stale_minutes)
    _json_print(data) if args.json else print(format_harness_registry(data), end="")
    return 0


def skills_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code skills", description="Inspect and install Git-backed skill vault packages.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    inventory = sub.add_parser("inventory", help="Show the skills-vault registry inventory.")
    inventory.add_argument("--repo", default=str(SKILLS_VAULT_DEFAULT))
    inventory.add_argument("--json", action="store_true")

    validate = sub.add_parser("validate", help="Validate the skills-vault registry.")
    validate.add_argument("--repo", default=str(SKILLS_VAULT_DEFAULT))
    validate.add_argument("--json", action="store_true")

    install = sub.add_parser("install", help="Link active skills into local harness skill roots.")
    install.add_argument("--repo", default=str(SKILLS_VAULT_DEFAULT))
    install.add_argument("--client", choices=[*SKILLS_CLIENTS, "all"], default="all")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show local install status for active skills.")
    status.add_argument("--repo", default=str(SKILLS_VAULT_DEFAULT))
    status.add_argument("--client", choices=[*SKILLS_CLIENTS, "all"], default="all")
    status.add_argument("--json", action="store_true")

    sync = sub.add_parser("sync", help="Fast-forward the skills-vault checkout, optionally installing afterward.")
    sync.add_argument("--repo", default=str(SKILLS_VAULT_DEFAULT))
    sync.add_argument("--client", choices=[*SKILLS_CLIENTS, "all"], default="all")
    sync.add_argument("--ff-only", action="store_true")
    sync.add_argument("--install", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    repo = skills_vault_repo(args.repo)

    if args.cmd == "inventory":
        registry = load_skills_registry(repo)
        active = [entry for entry in registry["skills"] if isinstance(entry, dict) and entry.get("status") == "active"]
        data = {"repo": str(repo), "vault": skills_vault_git_status(repo), "total": len(registry["skills"]), "active": len(active), "skills": registry["skills"]}
        if args.json:
            _json_print(data)
        else:
            print(f"{data['total']} skills ({data['active']} active) in {repo}")
            for entry in registry["skills"]:
                print(f"{entry.get('status', '')}\t{entry.get('id', '')}\t{','.join(entry.get('harnesses', []))}")
        return 0

    if args.cmd == "validate":
        errors = validate_skills_registry(repo)
        data = {"repo": str(repo), "ok": not errors, "errors": errors}
        if args.json:
            _json_print(data)
        elif errors:
            for item in errors:
                print(f"ERROR: {item}", file=sys.stderr)
        else:
            print(f"validated skills registry: {repo}")
        return 0 if not errors else 1

    if args.cmd == "install":
        result = install_vault_skills(repo, client=args.client, dry_run=args.dry_run)
        if args.json:
            _json_print(result)
        else:
            changed = [row for row in result["results"] if row["status"] in {"linked", "relinked", "would link", "would relink"}]
            conflicts = [row for row in result["results"] if row["status"] == "exists; left unchanged"]
            print(f"{len(changed)} link actions; {len(conflicts)} real-path conflicts; repo={repo}")
        return 0

    if args.cmd == "status":
        data = skills_install_status(repo, client=args.client)
        _json_print(data) if args.json else print(format_skills_status(data), end="")
        return 0

    if args.dry_run:
        sync_data: dict[str, Any] = {"repo": str(repo), "dry_run": True, "git": "would pull --ff-only" if args.ff_only else "would pull"}
    else:
        pull_cmd = ["git", "-C", str(repo), "pull"]
        if args.ff_only:
            pull_cmd.append("--ff-only")
        result = _run_capture(pull_cmd, cwd=repo, timeout=120)
        sync_data = {"repo": str(repo), "dry_run": False, "git_return_code": result.return_code, "git_output": result.output}
        if result.return_code != 0:
            _json_print(sync_data) if args.json else print(result.output, file=sys.stderr)
            return result.return_code
    if args.install:
        sync_data["install"] = install_vault_skills(repo, client=args.client, dry_run=args.dry_run)
    _json_print(sync_data) if args.json else print(f"synced {repo}")
    return 0


def auth_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code auth", description="Inspect cached harness auth readiness.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    preflight = sub.add_parser("preflight", help="Refresh auth readiness for a target harness.")
    preflight.add_argument("--to", choices=["claude"], default="claude")
    preflight.add_argument("--refresh", action="store_true")
    preflight.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="Show cached auth readiness.")
    status.add_argument("--to", choices=["claude"], default="claude")
    status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        data = cached_claude_auth_state() or {"ok": False, "status": "missing", "path": str(CLAUDE_AUTH_STATE)}
    else:
        agents = agent_map(load_config(DEFAULT_CONFIG))
        data = claude_auth_preflight(resolve_command(agents["claude"]), refresh=args.refresh)
    if args.json:
        _json_print(data)
    else:
        print(f"claude auth: {data.get('status')} ({'ok' if data.get('ok') else 'not ok'})")
    return 0 if data.get("ok") is True else 1


def trace_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code trace", description="Inspect agent bridge trace events.")
    parser.add_argument("--run-id")
    parser.add_argument("--type", dest="event_type")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = load_events(run_id=args.run_id, event_type=args.event_type)
    if args.json:
        _json_print(rows)
    else:
        print(format_events(rows))
    return 0


def findings_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code findings", description="Create and inspect structured findings.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create")
    create.add_argument("--run-id", required=True)
    create.add_argument("--severity", required=True)
    create.add_argument("--claim", required=True)
    create.add_argument("--evidence", action="append")
    create.add_argument("--reproduction", default="")
    create.add_argument("--status", default="open")
    create.add_argument("--owner-role", default="")
    create.add_argument("--rebuttal", default="")
    create.add_argument("--resolution", default="")
    create.add_argument("--json", action="store_true")

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--run-id")
    list_parser.add_argument("--status")
    list_parser.add_argument("--severity")
    list_parser.add_argument("--json", action="store_true")

    read = sub.add_parser("read")
    read.add_argument("id")
    read.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "create":
        row = create_finding(
            run_id=args.run_id,
            severity=args.severity,
            claim=args.claim,
            evidence=args.evidence,
            reproduction=args.reproduction,
            status=args.status,
            owner_role=args.owner_role,
            rebuttal=args.rebuttal,
            resolution=args.resolution,
        )
        _json_print(row) if args.json else print(row["id"])
        return 0
    if args.cmd == "list":
        rows = list_findings(run_id=args.run_id, status=args.status, severity=args.severity)
        _json_print(rows) if args.json else print(format_findings(rows))
        return 0
    row = read_finding(args.id)
    if row is None:
        raise BridgeError(f"no finding {args.id}")
    _json_print(row) if args.json else print(format_findings([row]))
    return 0


def verdicts_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code verdicts", description="Record and inspect loop verdicts.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    record = sub.add_parser("record")
    record.add_argument("--run-id", required=True)
    record.add_argument("--status", required=True)
    record.add_argument("--summary", required=True)
    record.add_argument("--blocking-finding", action="append", dest="blocking_findings")
    record.add_argument("--evidence", action="append")
    record.add_argument("--json", action="store_true")

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--run-id")
    list_parser.add_argument("--status")
    list_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "record":
        row = record_verdict(
            run_id=args.run_id,
            status=args.status,
            summary=args.summary,
            blocking_findings=_comma_values(args.blocking_findings),
            evidence=args.evidence,
        )
        _json_print(row) if args.json else print(row["id"])
        return 0
    rows = list_verdicts(run_id=args.run_id, status=args.status)
    _json_print(rows) if args.json else print(format_verdicts(rows))
    return 0


def workflow_cmd(argv: list[str]) -> int:
    global PROJECT_DIR
    parser = argparse.ArgumentParser(prog="agent workflow", description="Run portable workflows across configured agent engines.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_parser = sub.add_parser("list", help="List bundled portable workflows.")
    list_parser.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="Show a bundled workflow spec summary.")
    show.add_argument("workflow_id")
    show.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Run a portable workflow.")
    run.add_argument("workflow_id")
    run.add_argument("--question", help="Workflow question or task. If omitted, stdin is used.")
    run.add_argument("--tier", choices=["auto", "shallow", "standard", "deep"], default="auto")
    run.add_argument("--engine", choices=["auto", "codex", "claude"], default="auto")
    run.add_argument("--format", choices=["both", "text", "json"], default="both")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--from", dest="source", default=os.environ.get("AGENT_BRIDGE_CALLER", "human"))
    run.add_argument("--project-dir", help="Project/worktree directory. Defaults to the current git root.")
    run.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to bridge agent config JSON")
    run.add_argument("--model", help="Optional engine model override.")
    run.add_argument("--budget-usd", default=os.environ.get("AGENT_BRIDGE_BUDGET_USD", "0.50"))
    run.add_argument("--dry-run", action="store_true", help="Plan the workflow dispatch without invoking a model.")
    add_meta_args(run)

    inspect = sub.add_parser("inspect", help="Inspect a saved workflow run.")
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        rows = list_workflows()
        if args.json:
            _json_print(rows)
        else:
            for row in rows:
                print(f"{row['id']}\t{row['name']}\t{row.get('description', '')}")
        return 0

    if args.cmd == "show":
        spec = load_workflow(args.workflow_id)
        if args.json:
            _json_print(spec)
        else:
            print(f"{spec['id']} - {spec['name']}")
            print(spec.get("description", ""))
            print("")
            print("Phases:")
            for phase in spec.get("phases", []):
                print(f"- {phase.get('title')}: {phase.get('detail', '')}")
            print("")
            print("Tiers:")
            for tier, cfg in spec.get("tiers", {}).items():
                print(f"- {tier}: {cfg.get('angles')} angles, {cfg.get('fetch')} sources, {cfg.get('claims')} claims")
        return 0

    if args.cmd == "inspect":
        data = inspect_workflow_run(args.run_id)
        _json_print(data) if args.json else print(format_inspection(data), end="")
        return 0

    PROJECT_DIR = Path(args.project_dir).expanduser().resolve() if args.project_dir else discover_project_dir()
    question = args.question or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not question:
        raise BridgeError("a workflow question is required")
    meta = ensure_run_meta(extract_meta(args))
    if args.dry_run:
        plan = plan_workflow_run(
            workflow_id=args.workflow_id,
            question=question,
            tier=args.tier,
            engine=args.engine,
            source=args.source,
            meta=meta,
        )
        _json_print(plan) if args.format == "json" else print(format_workflow_plan(plan), end="")
        return 0

    config = load_config(Path(args.config))
    result = run_workflow(
        workflow_id=args.workflow_id,
        question=question,
        tier=args.tier,
        engine=args.engine,
        source=args.source,
        agents=agent_map(config),
        project_dir=PROJECT_DIR,
        concurrency=args.concurrency,
        fmt=args.format,
        model=args.model,
        budget_usd=str(args.budget_usd),
        meta=meta,
    )
    if args.format == "json":
        _json_print(result)
    elif args.format == "text":
        print(format_report(result), end="")
    else:
        print(format_report(result), end="")
        print("")
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def format_workflow_plan(plan: dict[str, Any]) -> str:
    phases = "\n".join(f"- {phase}" for phase in plan.get("phases", []))
    return (
        f"Workflow: {plan['workflow_id']} ({plan['name']})\n"
        f"Run: {plan['run_id']}\n"
        f"Engine: {plan['engine']}\n"
        f"Tier: {plan['tier']}\n"
        f"Question: {plan['question']}\n"
        f"Artifact dir: {plan['artifact_dir']}\n"
        "Dry run: yes\n\n"
        f"Phases:\n{phases}\n"
    )


def parse_loop_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agent code loop",
        description="Run a bounded builder -> critic -> verifier adversarial loop.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to bridge agent config JSON")
    parser.add_argument("--project-dir", help="Project/worktree directory. Defaults to the current git root.")
    parser.add_argument("--from", dest="source", default=os.environ.get("AGENT_BRIDGE_CALLER", "human"))
    parser.add_argument("--builder", default="codex", help="Agent id for code/build turns")
    parser.add_argument("--critic", default="claude", help="Agent id for adversarial review turns")
    parser.add_argument("--verifier", default="claude", help="Agent id for final verification turns")
    parser.add_argument("--max-turns", type=int, default=1)
    parser.add_argument("--budget-usd", default=os.environ.get("AGENT_BRIDGE_BUDGET_USD", DEFAULT_BUDGET_USD))
    parser.add_argument("--no-budget-auto", action="store_true", help="Disable automatic budget retry/calibration")
    parser.add_argument(
        "--max-auto-budget-usd",
        default=os.environ.get("AGENT_BRIDGE_MAX_AUTO_BUDGET_USD", DEFAULT_MAX_AUTO_BUDGET_USD),
        help="Maximum budget cap Agent Bridge may use when retrying budget failures.",
    )
    parser.add_argument(
        "--spawn-policy",
        choices=["auto", "full", "adversarial-only"],
        default=os.environ.get("AGENT_BRIDGE_SPAWN_POLICY", "auto"),
        help="Dispatch policy. auto gates full loops; adversarial-only dispatches one review agent.",
    )
    parser.add_argument("--prompt", help="Loop task prompt. If omitted in non-interactive mode, stdin is used.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned dispatches without invoking agents")
    add_meta_args(parser)
    return parser.parse_args(argv)


def _loop_prompt(
    *,
    role: str,
    attempt: int,
    original_prompt: str,
    run_id: str,
    loop_id: str,
    decision: SpawnDecision,
) -> str:
    return f"""[ADVERSARIAL LOOP]
Run: {run_id}
Loop: {loop_id}
Role: {role}
Attempt: {attempt}
Dispatch decision: {decision.mode}
Decision score: {decision.score}
Decision reasons: {'; '.join(decision.reasons)}

Original task:
{original_prompt}

Role contract:
- builder: implement the requested change and run focused tests.
- critic: inspect the current worktree for concrete defects and emit structured findings when available.
- verifier: smoke test the current worktree and record whether blocking issues remain.
- adversarial: when full-loop criteria are not met, do one analysis-only adversarial review and say whether a larger spawn is justified.

Report files changed, checks run, and any blocking findings or verdicts.
"""


def loop(argv: list[str]) -> int:
    global PROJECT_DIR
    args = parse_loop_args(argv)
    if args.max_turns < 1:
        raise BridgeError("--max-turns must be at least 1")
    PROJECT_DIR = Path(args.project_dir).expanduser().resolve() if args.project_dir else discover_project_dir()
    config = load_config(Path(args.config))
    agents = agent_map(config)
    for target in (args.builder, args.critic, args.verifier):
        if target not in agents:
            raise BridgeError(f"unknown loop agent {target!r}")

    original_prompt = read_prompt(args)
    if not original_prompt:
        raise BridgeError("a loop task prompt is required")
    media = prepare_prompt_media(original_prompt, project_dir=PROJECT_DIR)
    original_prompt = media.prompt
    decision = assess_spawn_decision(original_prompt, policy=args.spawn_policy, max_turns=args.max_turns)

    base_meta = ensure_run_meta(extract_meta(args))
    base_meta.setdefault("loop_id", base_meta.get("run_id", "run").replace("run_", "loop_", 1))
    emit_event(
        "run.created",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={
            "command": "loop",
            "source": args.source,
            "builder": args.builder,
            "critic": args.critic,
            "verifier": args.verifier,
            "max_turns": args.max_turns,
            "spawn_policy": args.spawn_policy,
            "dispatch_decision": decision.mode,
            "decision_score": decision.score,
            "decision_reasons": decision.reasons,
            "dry_run": args.dry_run,
        },
    )
    emit_event(
        "dispatch.policy_evaluated",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={
            "command": "loop",
            "spawn_policy": args.spawn_policy,
            "decision": decision.mode,
            "score": decision.score,
            "reasons": decision.reasons,
        },
    )

    rc = 0
    parent_id = base_meta.get("parent_id")
    turn_count = args.max_turns if decision.mode == "full_loop" else 1
    for attempt in range(1, turn_count + 1):
        if decision.mode == "full_loop":
            phases = [
                ("builder", args.builder, "code"),
                ("critic", args.critic, "review"),
                ("verifier", args.verifier, "review"),
            ]
        else:
            phases = [("adversarial", args.critic, "review")]
        for role, target_id, mode in phases:
            turn_meta = child_turn_meta(base_meta, role=role, attempt=attempt, parent_id=parent_id)
            prompt = _loop_prompt(
                role=role,
                attempt=attempt,
                original_prompt=original_prompt,
                run_id=str(turn_meta["run_id"]),
                loop_id=str(turn_meta["loop_id"]),
                decision=decision,
            )
            target_rc = invoke_target(
                agents[target_id],
                source=args.source,
                mode=mode,
                prompt=prompt,
                budget_usd=str(args.budget_usd),
                dry_run=args.dry_run,
                meta=turn_meta,
                media_dirs=media.media_dirs,
                budget_auto=not args.no_budget_auto,
                max_auto_budget_usd=str(args.max_auto_budget_usd),
            )
            parent_id = str(turn_meta["turn_id"])
            if target_rc != 0:
                rc = target_rc
                break
        if rc != 0:
            break

    emit_event(
        "run.completed",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={"command": "loop", "return_code": rc, "dry_run": args.dry_run, "events": str(events_path())},
    )
    print(f"run_id: {base_meta['run_id']}")
    print(f"loop_id: {base_meta['loop_id']}")
    print(f"dispatch_decision: {decision.mode}")
    print(f"decision_score: {decision.score}")
    print(f"events: {events_path()}")
    print(f"status: {'ok' if rc == 0 else 'failed'}")
    return rc


def main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "workflow":
        return workflow_cmd(argv[1:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "bridge":
        return bridge(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "loop":
        return loop(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "repair":
        return repair_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "auth":
        return auth_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "trace":
        return trace_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "findings":
        return findings_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "verdicts":
        return verdicts_cmd(argv[2:])
    if len(argv) >= 3 and argv[0] == "code" and argv[1] == "hook" and argv[2] == "session-start":
        return hook_session_start(argv[3:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "hooks":
        return hooks_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "harness":
        return harness_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "skills":
        return skills_cmd(argv[2:])
    if len(argv) >= 1 and argv[0] == "bridge":
        return bridge(argv[1:])
    print("usage: agent code bridge [options]", file=sys.stderr)
    print("       agent code loop [options]", file=sys.stderr)
    print("       agent code repair [options]", file=sys.stderr)
    print("       agent code auth <preflight|status> [options]", file=sys.stderr)
    print("       agent code trace [options]", file=sys.stderr)
    print("       agent code findings <create|list|read> [options]", file=sys.stderr)
    print("       agent code verdicts <record|list> [options]", file=sys.stderr)
    print("       agent code hook session-start [options]", file=sys.stderr)
    print("       agent code hooks <install|status> [options]", file=sys.stderr)
    print("       agent code harness <install-skill|register|status> [options]", file=sys.stderr)
    print("       agent code skills <inventory|validate|install|status|sync> [options]", file=sys.stderr)
    print("       agent workflow <list|show|run|inspect> [options]", file=sys.stderr)
    print("       agent bridge [options]", file=sys.stderr)
    return 2


def main_entry() -> None:
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (BridgeError, WorkflowError, ValueError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (BridgeError, WorkflowError, ValueError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(2)
