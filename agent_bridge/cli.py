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
import threading
from typing import Any
from urllib.parse import unquote, urlparse

from .coord import (
    capability_card,
    capability_cards,
    coord_cmd,
    evaluate_policy,
    explain_incompatibility,
    export_envelopes,
    format_doctor,
    record_run_task,
    run_doctor,
    set_trace_context,
)
from .context_adapters import ContextAdapterError, context_status, install_context_adapters
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
from .optimization import (
    DEFAULT_CACHE_TTL_SECONDS,
    build_scorecard,
    cache_key,
    cache_lookup,
    cache_store,
    cacheability_report,
    call_openai_gateway,
    choose_route,
    compress_context,
    exact_cache_path,
    format_gateway_status,
    format_scorecard,
    gateway_profile,
    gateway_status_rows,
    load_usage,
    parse_usage_metadata,
    tool_cache_path,
    write_usage,
)
from .readiness import (
    aggregate_readiness,
    configure_readiness,
    configure_shared_roots,
    flush_readiness_queue,
    load_cached_preflight,
    machine_id as stable_machine_id,
    publish_readiness,
    resolve_shared_roots,
    run_preflight,
)
from .session_recovery import (
    DEFAULT_CLAUDE_DATA_ROOT,
    DEFAULT_CLAUDE_PROJECTS_ROOT,
    SessionRecoveryError,
    discover_claude_sessions,
    filter_sessions,
    format_recovery_result,
    format_session_inventory,
    load_recovery_selection,
    recover_sessions,
)
from .skill_retirement import purge_retired_skills
from .managed_repos import (
    DEFAULT_MANAGED_INTERVAL_SECONDS,
    DEFAULT_MANAGED_TIMEOUT_SECONDS,
    describe_managed_repo,
    format_managed_repos,
    load_registry as load_managed_registry,
    sync_managed_repos,
)
from .trace import emit_event, events_path, format_events, load_events
from .updater import (
    DEFAULT_EXPECTED_REMOTE,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    bridge_revision,
    format_update,
    load_update_state,
    update_bridge,
)
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
SURFACES_CONFIG = BRIDGE_DIR / "surfaces.json"
STATE_DIR = Path(os.environ.get("AGENT_BRIDGE_STATE_DIR", Path.home() / ".local/state/agent-bridge")).expanduser()
TRANSCRIPT_DIR = STATE_DIR / "transcripts"
BRIDGE_LOG = STATE_DIR / "bridge_agents.log"
MEDIA_DIR = STATE_DIR / "media"
CONNECTION_STATE = STATE_DIR / "connections.json"
PROJECT_DIR = Path.cwd()
SHARED_BRIDGE_DIR_NAME = "Agent-Bridge"
SHARED_REGISTRY_DIR_NAME = "registry"
SHARED_SKILL_LINK_NAME = "agent-bridge"
DEFAULT_BUDGET_USD = "0.50"
DEFAULT_REPAIR_BUDGET_USD = "0.05"
DEFAULT_MAX_AUTO_BUDGET_USD = "1.00"
BUDGET_RETRY_LADDER = [0.10, 0.20, 0.50, 1.00, 2.00, 5.00]
HEIC_SUFFIXES = {".heic", ".heif"}
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
    usage: dict[str, Any] | None = None


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
    return (
        "failed to authenticate" in lowered
        or "invalid authentication credentials" in lowered
        or "not logged in" in lowered
        or "please run /login" in lowered
        or "401" in lowered
    )


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

Hard limits: no live production actions, no credential use, no deploy, no teardown, no browser
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
    adapter = agent.get("adapter")
    command = "openai-compatible-gateway" if adapter == "openai_gateway" else resolve_command(agent)
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
    if adapter == "openai_gateway":
        profile = gateway_profile(agent)
        if not profile:
            raise BridgeError(f"agent {agent['id']} adapter=openai_gateway requires a gateway profile")
        return [
            "openai-compatible-gateway",
            "--base-url",
            profile["base_url"],
            "--model",
            profile["model_alias"],
            "--budget-tag",
            profile["budget_tag"],
        ]
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


def _append_usage_record(
    *,
    run_id: str | None,
    command: str,
    record: dict[str, Any],
    budget_usd: str | None = None,
) -> None:
    if not run_id:
        return
    try:
        existing = load_usage(run_id)
        records = list(existing.get("actual", {}).get("records") or [])
        projected = existing.get("projected") or {}
        warnings = list(existing.get("warnings") or [])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        records = []
        projected = {}
        warnings = []
    records.append(record)
    budget_value = None
    if budget_usd not in {None, ""}:
        try:
            budget_value = float(str(budget_usd))
        except ValueError:
            budget_value = None
    write_usage(
        str(run_id),
        build_scorecard(
            run_id=str(run_id),
            command=command,
            projected=projected,
            records=records,
            budget_usd=budget_value,
            warnings=warnings,
        ),
    )


def _dispatch_metadata(
    *,
    agent: dict[str, Any],
    prompt: str,
    scope: str,
    route_policy: str,
) -> dict[str, Any]:
    profile = gateway_profile(agent)
    cache_report = cacheability_report(
        prefix=scope,
        task=prompt,
        provider=(profile or {}).get("provider", agent.get("id", "")),
        minimum_tokens=int(agent.get("cache_min_tokens", 1024)),
        cache_hint=str(agent.get("cache_hint", "auto")),
    )
    route = choose_route(policy=route_policy, prompt=prompt, cache_status="miss")
    return {
        "gateway": profile,
        "cacheability": cache_report,
        "route": route,
    }


def _usage_record(
    *,
    agent: dict[str, Any],
    mode: str,
    dry_run: bool,
    prompt: str,
    scope: str,
    metadata: dict[str, Any],
    provider_usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider_usage = provider_usage or {}
    gateway = metadata.get("gateway") or {}
    compression = metadata.get("compression") or {}
    input_tokens = provider_usage.get("input_tokens")
    output_tokens = provider_usage.get("output_tokens")
    if input_tokens is None:
        input_tokens = None if not dry_run else cacheability_report(prefix=scope, task=prompt).get("prefix_tokens_estimate", 0) + cacheability_report(prefix=scope, task=prompt).get("task_tokens_estimate", 0)
    return {
        "target": agent.get("id"),
        "mode": mode,
        "dry_run": dry_run,
        "provider": gateway.get("provider") or agent.get("id"),
        "model": gateway.get("model_alias") or agent.get("model") or agent.get("id"),
        "gateway": gateway.get("base_url") if gateway else None,
        "budget_tag": gateway.get("budget_tag") if gateway else "",
        "route": (metadata.get("route") or {}).get("route"),
        "route_reason": (metadata.get("route") or {}).get("reason"),
        "cache_status": "miss",
        "cached_tokens": provider_usage.get("cached_tokens"),
        "cache_creation_tokens": provider_usage.get("cache_creation_tokens"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "compression_saved_tokens": compression.get("saved_tokens", 0),
        "prompt_prefix_fingerprint": (metadata.get("cacheability") or {}).get("prefix_fingerprint"),
        "task_fingerprint": (metadata.get("cacheability") or {}).get("task_fingerprint"),
    }


BRIDGE_CACHE_CLASS = "review"


def _bridge_cache_key(
    *,
    agent: dict[str, Any],
    source: str,
    mode: str,
    prompt: str,
    meta: dict[str, Any],
) -> str:
    """Key a review dispatch on its prompt, scope, and current repo state.

    `cache_key` folds `repo_fingerprint` in, so HEAD plus a `git status --short`
    fingerprint are part of the key. An edited worktree therefore misses instead
    of replaying a review of code that no longer exists.
    """

    return cache_key(
        cache_class=BRIDGE_CACHE_CLASS,
        model=str(agent.get("model") or agent.get("id", "")),
        provider=str(agent.get("id", "")),
        prefix=build_scope(source, agent, mode, meta),
        task=prompt,
        project_dir=PROJECT_DIR,
        tool="bridge-review",
    )


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
    route_policy: str = "standard",
    cache_mode: str = "off",
) -> AgentRunResult:
    """Dispatch, optionally serving an identical prior review from the local cache.

    Only `review` is cacheable. A `code` dispatch mutates the worktree, so a hit
    would report work that never ran; `cache_store` enforces the same boundary
    through `SAFE_CACHE_CLASSES`, which has no `code` member.
    """

    meta = meta or {}
    cacheable = cache_mode == "exact" and mode == "review" and not dry_run
    key = ""

    if cacheable:
        key = _bridge_cache_key(agent=agent, source=source, mode=mode, prompt=prompt, meta=meta)
        hit = cache_lookup(key)
        if hit["status"] == "hit":
            cached = hit["entry"].get("value") or {}
            output = str(cached.get("output", ""))
            print(output)
            print(f"[cache] {agent['id']} review served from local cache {key}", file=sys.stderr)
            emit_event(
                "agent.completed",
                run_id=meta.get("run_id"),
                meta=meta,
                data={
                    "target": agent["id"],
                    "mode": mode,
                    "return_code": 0,
                    "dry_run": False,
                    "cache_status": "hit",
                    "cache_key": key,
                },
            )
            return AgentRunResult(return_code=0, output=output, usage=cached.get("usage"))

    result = _invoke_target_dispatch(
        agent,
        source=source,
        mode=mode,
        prompt=prompt,
        budget_usd=budget_usd,
        dry_run=dry_run,
        meta=meta,
        media_dirs=media_dirs,
        route_policy=route_policy,
    )

    if cacheable and result.return_code == 0:
        cache_store(
            key,
            {"output": result.output, "usage": result.usage},
            cache_class=BRIDGE_CACHE_CLASS,
            semantic_text=prompt,
            metadata={"target": agent["id"], "mode": mode, "run_id": str(meta.get("run_id", ""))},
        )

    return result


def _invoke_target_dispatch(
    agent: dict[str, Any],
    *,
    source: str,
    mode: str,
    prompt: str,
    budget_usd: str,
    dry_run: bool,
    meta: dict[str, Any] | None = None,
    media_dirs: list[Path] | None = None,
    route_policy: str = "standard",
) -> AgentRunResult:
    meta = meta or {}
    scope = build_scope(source, agent, mode, meta)
    dispatch_meta = _dispatch_metadata(agent=agent, prompt=prompt, scope=scope, route_policy=route_policy)
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
        data={
            "target": agent["id"],
            "mode": mode,
            "dry_run": dry_run,
            "project_dir": str(PROJECT_DIR),
            "gateway": dispatch_meta.get("gateway") or {},
            "route": dispatch_meta.get("route"),
            "cacheability": dispatch_meta.get("cacheability"),
        },
    )
    if dry_run:
        print(f"[dry-run] {agent['id']}: {shlex.join(cmd)}")
        route = dispatch_meta["route"]
        cache_report = dispatch_meta["cacheability"]
        gateway = dispatch_meta["gateway"]
        gateway_text = f"{gateway['base_url']} model={gateway['model_alias']} budget_tag={gateway['budget_tag']}" if gateway else "direct"
        print(f"[dry-run] {agent['id']} gateway: {gateway_text}")
        print(f"[dry-run] {agent['id']} route: {route['route']} ({route['reason']})")
        print(
            "[dry-run] "
            f"{agent['id']} prompt_cache: prefix={cache_report['prefix_fingerprint']} "
            f"task={cache_report['task_fingerprint']} "
            f"prefix_tokens~{cache_report['prefix_tokens_estimate']} "
            f"{'cacheable' if cache_report['likely_provider_cacheable'] else 'below-minimum'}"
        )
        _append_usage_record(
            run_id=meta.get("run_id"),
            command="bridge",
            budget_usd=budget_usd,
            record=_usage_record(
                agent=agent,
                mode=mode,
                dry_run=True,
                prompt=prompt,
                scope=scope,
                metadata=dispatch_meta,
            ),
        )
        emit_event(
            "agent.completed",
            run_id=meta.get("run_id"),
            meta=meta,
            data={"target": agent["id"], "mode": mode, "return_code": 0, "dry_run": True},
        )
        return AgentRunResult(return_code=0, output="")

    if agent.get("adapter") == "openai_gateway":
        profile = dispatch_meta.get("gateway")
        if not profile:
            raise BridgeError(f"agent {agent['id']} adapter=openai_gateway requires a gateway profile")
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        prefix = safe_fragment(meta.get("run_id", agent["id"]))
        turn = safe_fragment(meta.get("turn_id", utc_stamp()))
        transcript = TRANSCRIPT_DIR / f"{prefix}_{turn}_{agent['id']}_{utc_stamp()}.txt"
        write_header(transcript, source=source, target=agent["id"], mode=mode, prompt=prompt, cmd=cmd, meta=meta)
        try:
            gateway_result = call_openai_gateway(profile=profile, prompt=prompt, system_prompt=scope)
            output = str(gateway_result.get("output", ""))
            transcript.write_text(transcript.read_text(encoding="utf-8") + output + "\n", encoding="utf-8")
            print(output)
            usage_record = _usage_record(
                agent=agent,
                mode=mode,
                dry_run=False,
                prompt=prompt,
                scope=scope,
                metadata=dispatch_meta,
                provider_usage=gateway_result.get("usage"),
            )
            _append_usage_record(run_id=meta.get("run_id"), command="bridge", budget_usd=budget_usd, record=usage_record)
            emit_event(
                "agent.completed",
                run_id=meta.get("run_id"),
                meta=meta,
                data={"target": agent["id"], "mode": mode, "return_code": 0, "dry_run": False, "transcript": str(transcript), "usage": usage_record},
            )
            return AgentRunResult(return_code=0, output=output, transcript=transcript, usage=usage_record)
        except RuntimeError as exc:
            output = str(exc)
            transcript.write_text(transcript.read_text(encoding="utf-8") + output + "\n", encoding="utf-8")
            emit_event(
                "agent.completed",
                run_id=meta.get("run_id"),
                meta=meta,
                data={"target": agent["id"], "mode": mode, "return_code": 1, "dry_run": False, "transcript": str(transcript), "error": output},
            )
            return AgentRunResult(return_code=1, output=output, transcript=transcript)

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
    usage_record = _usage_record(
        agent=agent,
        mode=mode,
        dry_run=False,
        prompt=prompt,
        scope=scope,
        metadata=dispatch_meta,
        provider_usage=parse_usage_metadata(_maybe_json("".join(output))),
    )
    _append_usage_record(run_id=meta.get("run_id"), command="bridge", budget_usd=budget_usd, record=usage_record)
    emit_event(
        "agent.completed",
        run_id=meta.get("run_id"),
        meta=meta,
        data={"target": agent["id"], "mode": mode, "return_code": rc, "dry_run": False, "transcript": str(transcript), "usage": usage_record},
    )
    print(f"\n[transcript] {transcript}", file=sys.stderr)
    return AgentRunResult(return_code=rc, output="".join(output), transcript=transcript, usage=usage_record)


def _maybe_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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
    route_policy: str = "standard",
    cache_mode: str = "off",
) -> int:
    budget = calibrated_budget(agent["id"], budget_usd, enabled=budget_auto and not dry_run)
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
            route_policy=route_policy,
            cache_mode=cache_mode,
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
    parser.add_argument(
        "--cache-mode",
        choices=["off", "exact"],
        default=os.environ.get("AGENT_BRIDGE_BRIDGE_CACHE_MODE", "off"),
        help=(
            "Serve an identical prior review from the local deterministic cache. "
            "Keyed on prompt, scope, and repo state, so an edited worktree misses. "
            "Review mode only; code dispatches are never cached."
        ),
    )
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
    parser.add_argument("--no-preflight", action="store_true", help="Skip the default authenticated work-readiness gate for code mode")
    parser.add_argument("--require-ready", action="store_true", help="Require a fully ready report, including advisory probes")
    parser.add_argument("--refresh-readiness", action="store_true", help="Refresh readiness instead of using a fresh cache")
    parser.add_argument("--preflight-timeout", type=int, default=20, help="Maximum seconds for each readiness probe")
    parser.add_argument(
        "--route-policy",
        choices=["off", "no-route", "cache-first", "cheap-classifier", "standard", "premium"],
        default=os.environ.get("AGENT_BRIDGE_ROUTE_POLICY", "standard"),
        help="Optional token/spend routing policy for trace and dry-run explanation.",
    )
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
    policy_rc = _enforce_dispatch_policy("bridge", source=source, mode=mode, meta=base_meta)
    if policy_rc is not None:
        return policy_rc
    set_trace_context(base_meta)
    emit_event(
        "run.created",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={"command": "bridge", "source": source, "mode": mode, "targets": targets, "dry_run": args.dry_run},
    )
    record_run_task("created", meta=base_meta, command="bridge", data={"targets": targets, "mode": mode})
    if not args.dry_run:
        for target_id in targets:
            if not _dispatch_readiness_gate(
                target_id,
                mode=mode,
                command="bridge",
                meta=base_meta,
                no_preflight=args.no_preflight,
                require_ready=args.require_ready,
                refresh=args.refresh_readiness,
                timeout=args.preflight_timeout,
            ):
                emit_event("run.completed", run_id=base_meta.get("run_id"), meta=base_meta, data={"command": "bridge", "return_code": 4, "dry_run": False})
                record_run_task("failed", meta=base_meta, command="bridge", data={"return_code": 4, "reason": "readiness_refused"})
                return 4
    if args.dry_run:
        media_suffixes = sorted({path.suffix for path in discover_prompt_heic_inputs(prompt, project_dir=PROJECT_DIR)})
        for target_id in targets:
            card = capability_card(agents[target_id], bridge_dir=BRIDGE_DIR)
            for problem in explain_incompatibility(card, mode=mode, media_suffixes=media_suffixes, project_dir=str(PROJECT_DIR)):
                print(f"[dry-run] incompatibility: {problem}")
    if args.cache_mode == "exact" and mode != "review":
        raise BridgeError(
            "--cache-mode exact applies to --mode review only; a code dispatch mutates the "
            "worktree, so a cache hit would report work that never ran"
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
            route_policy=args.route_policy,
            cache_mode=args.cache_mode,
        )
        if target_rc != 0:
            rc = target_rc
    emit_event(
        "run.completed",
        run_id=base_meta.get("run_id"),
        meta=base_meta,
        data={"command": "bridge", "return_code": rc, "dry_run": args.dry_run},
    )
    record_run_task("artifact_attached", meta=base_meta, command="bridge", artifact={"path": str(events_path()), "kind": "trace"})
    record_run_task("completed" if rc == 0 else "failed", meta=base_meta, command="bridge", data={"return_code": rc})
    return rc


def _enforce_dispatch_policy(action: str, *, source: str, mode: str, meta: dict[str, Any]) -> int | None:
    """Return an exit code when local trust policy blocks a dispatch, else None."""
    decision = evaluate_policy(
        {
            "client": source,
            "machine": _harness_machine_id(),
            "repo": str(PROJECT_DIR),
            "mode": mode,
            "action": action,
            "run_id": meta.get("run_id"),
        }
    )
    if decision["decision"] == "deny":
        print(f"[agent-bridge] policy denied {action} dispatch: {decision['reason']}", file=sys.stderr)
        return 3
    if decision["decision"] == "require_approval" and os.environ.get("AGENT_BRIDGE_APPROVED") != "1":
        print(
            f"[agent-bridge] policy requires approval for {action} dispatch: {decision['reason']}. "
            "Re-run with AGENT_BRIDGE_APPROVED=1 after operator review.",
            file=sys.stderr,
        )
        return 3
    return None


def _dispatch_readiness_gate(
    target_id: str,
    *,
    mode: str,
    command: str,
    meta: dict[str, Any],
    no_preflight: bool,
    require_ready: bool,
    refresh: bool,
    timeout: int,
) -> bool:
    """Cache-first bounded gate with an explicit trace and task-ledger decision."""
    if no_preflight:
        decision = {"target": target_id, "decision": "bypass", "overall": "unknown", "stale": False, "age_seconds": None, "source": "operator"}
        emit_event("dispatch.readiness_evaluated", run_id=meta.get("run_id"), meta=meta, data=decision)
        record_run_task("updated", meta=meta, command=command, data={"readiness": decision})
        return True
    report = None if refresh else load_cached_preflight(target_id, "bridge", scope="work")
    if report is not None:
        try:
            cached_project = Path(str(report.get("project_dir", ""))).expanduser().resolve()
        except (OSError, RuntimeError):
            cached_project = None
        if cached_project != PROJECT_DIR:
            report = None
    source = "cache" if report is not None else "live"
    if report is None:
        try:
            report = run_preflight(
                target_id,
                "bridge",
                scope="work",
                project_dir=PROJECT_DIR,
                timeout=max(1, timeout),
                expected_github_login=os.environ.get("AGENT_BRIDGE_EXPECTED_GITHUB_LOGIN", ""),
            )
        except Exception as exc:
            report = {
                "generated_at": iso_now(),
                "overall": "blocked",
                "checks": [{"name": "preflight_runtime", "required": True, "status": "blocked", "error_class": "source_unreachable", "detail": str(exc)}],
            }
    generated = _parse_iso_timestamp(report.get("generated_at"))
    age_seconds = max(0, int((dt.datetime.now(dt.timezone.utc) - generated).total_seconds())) if generated else None
    stale = bool(report.get("stale"))
    overall = "unknown" if stale else str(report.get("overall", "unknown"))
    checks = report.get("checks", []) if isinstance(report.get("checks"), list) else []
    strict_ready = not stale and bool(checks) and all(row.get("status") == "ready" for row in checks)
    refused = (mode == "code" and overall == "blocked") or (require_ready and not strict_ready)
    action = "refuse" if refused else ("warn" if overall != "ready" else "allow")
    decision = {
        "target": target_id,
        "decision": action,
        "overall": overall,
        "stale": stale,
        "age_seconds": age_seconds,
        "source": source,
        "require_ready": require_ready,
        "strict_ready": strict_ready,
    }
    emit_event("dispatch.readiness_evaluated", run_id=meta.get("run_id"), meta=meta, data=decision)
    record_run_task("updated", meta=meta, command=command, data={"readiness": decision})
    if refused:
        if require_ready:
            blocked = ", ".join(row.get("name", "unknown") for row in checks if row.get("status") != "ready")
        else:
            blocked = ", ".join(
                row.get("name", "unknown") for row in checks if row.get("required") and row.get("status") == "blocked"
            )
        blocked = blocked or overall
        print(
            f"[agent-bridge] readiness refused {target_id} {mode} dispatch: {blocked}. "
            f"Run `agent code preflight work --client {target_id} --surface bridge --refresh` for details, "
            "or use --no-preflight after operator review.",
            file=sys.stderr,
        )
        return False
    if action == "warn":
        print(f"[agent-bridge] readiness warning for {target_id} {mode} dispatch: {overall}; continuing with trace evidence", file=sys.stderr)
    return True


def _json_print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _comma_values(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _hook_agent_command(client: str) -> str:
    agent_bin = os.environ.get("AGENT_BRIDGE_HOOK_AGENT", os.path.expanduser("~/.local/bin/agent"))
    if agent_bin.lower().endswith((".cmd", ".bat")) or "\\" in agent_bin:
        return f'cmd /d /c ""{agent_bin}" code hook session-start --client {client}"'
    return f"'{agent_bin}' code hook session-start --client {client}"


def load_surface_manifest(path: Path = SURFACES_CONFIG) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    surfaces = data.get("surfaces")
    if not isinstance(surfaces, list):
        raise BridgeError(f"{path} must define a surfaces list")
    declared: set[str] = set()
    seen: set[tuple[str, str]] = set()
    required_fields = {"client", "surface", "startup_mechanism", "config_path", "registration_command", "verification_method", "installable"}
    for index, row in enumerate(surfaces):
        if not isinstance(row, dict):
            raise BridgeError(f"{path} surfaces[{index}] must be an object")
        missing = sorted(required_fields - row.keys())
        if missing:
            raise BridgeError(f"{path} surfaces[{index}] is missing: {', '.join(missing)}")
        key = (str(row["client"]), str(row["surface"]))
        if key in seen:
            raise BridgeError(f"{path} contains duplicate surface {key[0]}/{key[1]}")
        seen.add(key)
        declared.add(key[0])
    try:
        configured = load_config(DEFAULT_CONFIG)
    except (BridgeError, OSError, json.JSONDecodeError):
        configured = {}
    for agent in configured.get("agents", []):
        client = str(agent.get("id", ""))
        if client and client not in declared:
            surfaces.append(
                {
                    "client": client,
                    "surface": "cli",
                    "startup_mechanism": "unsupported",
                    "config_path": "",
                    "registration_command": "",
                    "verification_method": "manifest entry required for newly configured agent",
                    "installable": False,
                    "note": "Synthesized because the configured agent has no startup-surface declaration.",
                }
            )
    return data


def infer_surface(client: str) -> str:
    explicit = os.environ.get("AGENT_BRIDGE_SURFACE")
    if explicit:
        return safe_fragment(explicit)
    hints = " ".join(
        value
        for name, value in os.environ.items()
        if name in {"TERM_PROGRAM", "__CFBundleIdentifier", "CODEX_INTERNAL_ORIGINATOR", "CLAUDE_CODE_ENTRYPOINT"}
    ).lower()
    if any(token in hints for token in ("desktop", ".app", "cowork", "gui")):
        return "gui"
    return "cli"


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
    configured = resolve_shared_roots()["roots"]["skills"]["selected"]
    candidates = [Path(configured)] if configured else []
    candidates.extend(_env_path_candidates(
        "AGENT_BRIDGE_SHARED_SKILLS_ROOT",
        "SHARED_AGENT_SKILLS_ROOT",
        "CAREER_SHARED_SKILLS_ROOT",
    ))
    for name in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value) / "SharedAgentSkills")
    candidates.extend(
        [
            home / "Library" / "CloudStorage" / "OneDrive-Personal" / "SharedAgentSkills",
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
        try:
            if _bounded_io((candidate / SHARED_BRIDGE_DIR_NAME).exists, timeout=0.25):
                return candidate
        except (OSError, TimeoutError):
            continue
    if not require_bridge_dir:
        for candidate in candidates:
            try:
                if _bounded_io(candidate.exists, timeout=0.25):
                    return candidate
            except (OSError, TimeoutError):
                continue
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


def _git_revision(repo: Path, ref: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", ref],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _harness_machine_id() -> str:
    """Registry key for this machine. See `readiness.machine_id`.

    Was a second, slightly different copy of that function keyed on
    `socket.gethostname()`, which changes with the active network.
    """

    return stable_machine_id()


def register_harness(
    client: str,
    *,
    root: str | None = None,
    status: str = "active",
    surface: str = "",
    startup_mechanism: str = "manual",
) -> dict[str, Any]:
    bridge_dir = shared_bridge_dir(root, create=True)
    assert bridge_dir is not None
    registry_dir = bridge_dir / SHARED_REGISTRY_DIR_NAME
    registry_dir.mkdir(parents=True, exist_ok=True)

    cwd = os.environ.get("PWD") or str(Path.cwd())
    client_id = safe_fragment(client)
    machine_id = _harness_machine_id()
    surface_id = safe_fragment(surface) if surface else ""
    suffix = f".{surface_id}" if surface_id else ""
    path = registry_dir / f"{machine_id}.{client_id}{suffix}.json"
    update_state = load_update_state()
    bridge_repo = BRIDGE_DIR.parent
    record: dict[str, Any] = {
        "schema_version": "1.0",
        "updated_at": iso_now(),
        "status": status,
        "client": client,
        "surface": surface or "unspecified",
        "startup_mechanism": startup_mechanism,
        "registration_proves_auth": False,
        "machine_id": machine_id,
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cwd": cwd,
        "git_root": _git_root_for_path(cwd),
        "agent_command": shutil.which("agent") or os.environ.get("AGENT_BRIDGE_HOOK_AGENT") or "",
        "bridge_repo": str(bridge_repo),
        "bridge_revision": bridge_revision(bridge_repo),
        "origin_main_revision": _git_revision(bridge_repo, "refs/remotes/origin/main"),
        "deployed_revision": str(update_state.get("deployed_revision", "")),
        "update_status": str(update_state.get("status", "unknown")),
        "update_checked_at": str(update_state.get("checked_at", "")),
        "mailbox_mcp": str(BRIDGE_DIR / "mailbox_mcp.py"),
        "state_dir": str(STATE_DIR),
        "shared_bridge_dir": str(bridge_dir),
        "registry_file": str(path),
    }
    try:
        record["capabilities"] = capability_cards(load_config(DEFAULT_CONFIG), bridge_dir=BRIDGE_DIR)
    except (BridgeError, OSError, json.JSONDecodeError):
        record["capabilities"] = []
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return record


def maybe_register_harness(client: str, *, surface: str = "", startup_mechanism: str = "native-session-hook") -> dict[str, Any] | None:
    if os.environ.get("AGENT_BRIDGE_DISABLE_SHARED_REGISTRY") in {"1", "true", "TRUE", "yes"}:
        return None
    root = resolve_shared_skills_root(required=False, require_bridge_dir=True)
    if root is None:
        return None
    try:
        return _bounded_io(
            lambda: register_harness(client, root=str(root), surface=surface, startup_mechanism=startup_mechanism),
            timeout=2.0,
        )
    except (OSError, TimeoutError):
        return None


def _parse_iso_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _bounded_io(action: Any, *, timeout: float) -> Any:
    """Avoid hanging forever when a cloud-sync placeholder is offline."""
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = action()
        except BaseException as exc:  # Preserve the original filesystem error in the caller.
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
    return str(_bounded_io(lambda: path.read_text(encoding="utf-8"), timeout=timeout))


# A row this old describes a harness that has not started in a month. Keeping it
# makes `harness status` read mostly-dead entries; the registry is a heartbeat
# store, so an unrefreshed row carries no information.
REGISTRY_EXPIRY_DAYS = 30


def load_harness_registry(
    root: str | None = None,
    *,
    stale_minutes: int = 1440,
    prune: bool = True,
) -> dict[str, Any]:
    bridge_dir = shared_bridge_dir(root, create=False)
    assert bridge_dir is not None
    registry_dir = bridge_dir / SHARED_REGISTRY_DIR_NAME
    now = dt.datetime.now(dt.timezone.utc)
    expiry_seconds = REGISTRY_EXPIRY_DAYS * 86400
    rows: list[dict[str, Any]] = []
    pruned: list[str] = []
    if registry_dir.exists():
        for path in sorted(registry_dir.glob("*.json")):
            original_stat = None
            try:
                original_stat = path.stat()
                row = json.loads(_bounded_read_text(path))
            except (OSError, TimeoutError, json.JSONDecodeError) as exc:
                row = {"client": "unknown", "machine_id": path.stem, "status": "invalid", "error": str(exc)}
            updated = _parse_iso_timestamp(row.get("updated_at"))
            age_seconds = int((now - updated).total_seconds()) if updated else None
            if prune and age_seconds is not None and age_seconds > expiry_seconds:
                try:
                    current_stat = path.stat()
                    original_identity = (
                        original_stat.st_ino,
                        original_stat.st_size,
                        original_stat.st_mtime_ns,
                    ) if original_stat is not None else None
                    current_identity = (current_stat.st_ino, current_stat.st_size, current_stat.st_mtime_ns)
                    if original_identity == current_identity:
                        path.unlink()
                        pruned.append(path.name)
                        continue
                except OSError:
                    pass
            row["registry_file"] = str(path)
            row["age_seconds"] = age_seconds
            row["fresh"] = bool(age_seconds is not None and age_seconds <= stale_minutes * 60 and row.get("status") == "active")
            rows.append(row)
    return {
        "shared_bridge_dir": str(bridge_dir),
        "registry_dir": str(registry_dir),
        "stale_minutes": stale_minutes,
        "expiry_days": REGISTRY_EXPIRY_DAYS,
        "pruned": pruned,
        "harnesses": rows,
    }


def format_harness_registry(data: dict[str, Any]) -> str:
    lines = [
        f"Shared Agent Bridge: {data['shared_bridge_dir']}",
        f"Registry: {data['registry_dir']}",
        f"Stale after: {data['stale_minutes']} minutes",
        f"Retention: {data.get('expiry_days', REGISTRY_EXPIRY_DAYS)} days",
        "",
    ]
    pruned = data.get("pruned", [])
    if pruned:
        lines.extend([f"Pruned expired rows: {len(pruned)}", ""])
    rows = data.get("harnesses", [])
    if not rows:
        lines.append("(no harness registrations found)")
        return "\n".join(lines) + "\n"
    lines.append("fresh\tclient\tsurface\tmachine\tstatus\tupdated_at\tgit_root")
    for row in rows:
        fresh = "yes" if row.get("fresh") else "no"
        lines.append(
            "\t".join(
                [
                    fresh,
                    str(row.get("client", "")),
                    str(row.get("surface", "unspecified")),
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
    budget = calibrated_budget(agent["id"], budget_usd, enabled=True)
    while True:
        result = _run_capture(
            [command, "-p", prompt, "--max-budget-usd", budget, "--output-format", "text"],
            timeout=120,
        )
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
description: Use when coordinating Codex, Claude, or other coding harnesses through Agent Bridge; recovering interrupted native sessions after usage or API limits; checking shared OneDrive harness status; registering a harness heartbeat; using mailbox MCP; or invoking agent code bridge, loop, workflow, hooks, or harness commands across macOS and Windows machines.
---

# Agent Bridge

Use the installed `agent` command as the front door for local and cross-harness coordination. Prefer the global bridge checkout over project-local copies.

## Fast Checks

- Check shared machine and harness presence: `agent code harness status`
- Register the current harness manually: `agent code harness register --client <codex|claude|other>`
- Check local SessionStart hooks and wrappers: `agent code hooks status --client all`
- Inspect or force the canonical bridge refresh: `agent code update <status|check|apply>`
- Check tracked repos for drift and uncommitted work: `agent code repos status`
- List the configured managed repos and their modes: `agent code repos list`
- List callable local engines: `agent code bridge --list`
- Inspect trace events: `agent code trace`

## Automatic Update

- Every installed SessionStart hook and GUI wrapper runs a cached, bounded update check before registering the harness.
- Updates only run from a clean `main` checkout whose `origin` matches the canonical repository. They fetch without prompting and accept fast-forwards only.
- A changed revision is compiled and then refreshes the launcher, hooks, wrappers, and Codex, Claude, Grok, and shared `.agents` skill links. The startup hook re-executes once so the new code supplies the current session context.
- Dirty, non-main, ahead, or diverged checkouts are never overwritten. Offline starts continue with the last installed revision and cache the failure briefly.
- Set `AGENT_BRIDGE_DISABLE_AUTO_UPDATE=1` for an emergency startup bypass. Use `agent code update apply --force` to bypass only the normal freshness interval.

## Deprecated Skill Purge

- Every SessionStart hook applies the packaged `retired_skills.json` manifest after updating Agent Bridge and before harness registration.
- Purges are exact and allowlisted: each entry names an approved root, path pattern, and expected `SKILL.md` name. A missing or mismatched skill is reported and left untouched.
- Add a retirement only after its replacement is canonical and published. Never add broad cache, plugin, harness, or skill-root paths.
- Set `AGENT_BRIDGE_DISABLE_SKILL_PURGE=1` for emergency diagnosis; remove the override after correcting the manifest or installation.

## Managed Repos

- The SessionStart hook can also check repos you track alongside the bridge, so shared sources stay consistent across harnesses, machines, and agents. Inspect with `agent code repos status`; list what is configured with `agent code repos list`.
- The registry ships **empty**. Agent Bridge embeds no repo names, clone URLs, or filesystem paths; declare them per machine in `{STATE_DIR / 'managed-repos.json'}`. With no config file the sweep is inert and silent.
- Repos declared `apply` are fast-forwarded through the same hardened path as the bridge: clean declared branch only, verified `origin`, ff-only. Repos declared `report` are read-only and never mutated. Use `report` for any repo that carries generated output, since a routinely dirty tree would pin `apply` in `blocked_dirty` forever.
- Two failure modes are reported separately. Being behind `origin` is a sync problem a pull fixes. Work held as uncommitted local edits exists on no remote, so no pull on any other machine would ever retrieve it; those files are listed individually under `canonical_paths`.
- A dirty checkout blocks its own update and still reports how far behind it is. Warning states, and states where drift was never determined (`busy`, `offline`, `disabled`), are never served from cache and never counted as current.
- Missing checkouts report `absent` rather than failing: not every machine holds every repo.
- Override a single path with `AGENT_BRIDGE_MANAGED_<ID>_PATH`. Set `AGENT_BRIDGE_DISABLE_MANAGED_REPOS=1` to skip the sweep entirely.
- Managed repos default to a one-hour freshness interval, not the bridge's five minutes, because they do not gate startup correctness and a fetch per repo per session is wasted latency.

## Coordination

- Use `agent code bridge --from <caller> --to <target> --mode review --prompt "..."` for a bounded one-shot review or plan comparison.
- Use `agent code bridge --mode code` only for scoped implementation tasks with an explicit worktree.
- Use `agent code loop` for adversarial builder/critic/verifier loops; keep budgets explicit when cost matters.
- Use mailbox MCP for async handoffs. Mailbox messages are the durable proof path; shell process lifetime is secondary.

## Active Session Recovery

- When one harness hits a usage or API limit, start with `agent code sessions inventory` to reconcile its native metadata and transcript pointers.
- Use `agent code sessions recover --continue <session-id> --to codex --enqueue` only after reviewing which sessions are genuinely unfinished. Mark proven completed sessions with `--complete` so they are recorded but not duplicated.
- Recovery artifacts stay under `{STATE_DIR / 'session-recovery'}`. They contain bounded, credential-redacted context and pointers to native evidence, never copied raw transcripts.
- A recovery task is a durable handoff, not proof that a native target chat was created. The target harness must create or claim one isolated task per handoff and preserve the exact project/worktree.
- Re-verify Git, pull-request, artifact, and other live state before continuing. Use `--verify-github` when GitHub PR evidence is part of completion classification.

## Readiness and Context

- Use `agent code preflight session --client <client> --surface <surface>` for a cache-first, startup-safe local check.
- Use `agent code preflight work --client <client> --surface bridge` before authenticated source work. Configure the expected GitHub login and optional canonical context manifest with `agent code preflight configure`.
- Inspect or set the separate SharedAgentSkills, SharedAgentData, and SharedAgentConversations roots with `agent code preflight roots`.
- Code-mode bridge and loop dispatches block on failed required work checks. `--no-preflight` is an explicit operator override, not a default.
- Generate and verify harness-native context adapters with `agent code context install` and `agent code context check`; the canonical content remains outside Agent Bridge.
- Publish only the redacted cached summary with `agent code preflight publish`; never copy local diagnostic details or credentials into shared state.

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

### Row identity

Each row is keyed by `<machine>.<client>[.<surface>]`. Unless
`AGENT_BRIDGE_MACHINE_ID` is set, the machine key combines the local username
with the first eight hexadecimal characters of a SHA-256 hash of the platform's
stable machine identifier (macOS `IOPlatformUUID`, Linux `machine-id`, or
Windows `MachineGuid`). The raw platform identifier is never written to the
shared registry. If no stable identifier is available, Agent Bridge falls back
to the legacy username-and-hostname key.

Registry rows older than `{REGISTRY_EXPIRY_DAYS}` days are pruned during status
inspection. Use `agent code harness status --no-prune` for a read-only view that
retains expired rows.

## Path Rules

- Resolve the shared skill root with `AGENT_BRIDGE_SHARED_SKILLS_ROOT`, then `SHARED_AGENT_SKILLS_ROOT`, then the platform OneDrive defaults.
- macOS default bridge repo: `~/Code/agent-bridge`
- Windows default bridge repo: `%USERPROFILE%\\Code\\agent-bridge`
- MCP mailbox registrations should point to `agent_bridge/mailbox_mcp.py` in the global bridge repo, not a project-local copy.
"""


def _skill_link_paths(client: str) -> list[Path]:
    home = Path.home()
    if client == "codex":
        return [home / ".codex" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "claude":
        return [home / ".claude" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "grok":
        return [home / ".grok" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "agents":
        return [home / ".agents" / "skills" / SHARED_SKILL_LINK_NAME]
    if client == "all":
        return (
            _skill_link_paths("codex")
            + _skill_link_paths("claude")
            + _skill_link_paths("grok")
            + _skill_link_paths("agents")
        )
    return []


def _ensure_skill_link(link_path: Path, target: Path) -> dict[str, str]:
    if link_path.exists() or link_path.is_symlink():
        try:
            if link_path.resolve() == target.resolve():
                return {"path": str(link_path), "status": "already linked"}
        except OSError:
            pass
        if not link_path.is_symlink():
            return {"path": str(link_path), "status": "exists; left unchanged"}
        try:
            link_path.unlink()
        except OSError as exc:
            return {"path": str(link_path), "status": f"retarget failed: {exc}"}
        status = "retargeted"
    else:
        status = "linked"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link_path, target_is_directory=True)
        return {"path": str(link_path), "status": status}
    except OSError as exc:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(link_path), str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if proc.returncode == 0:
                return {"path": str(link_path), "status": "junction-retargeted" if status == "retargeted" else "junction-linked"}
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


def check_shared_skill(root: str | None = None, *, link_client: str = "all") -> dict[str, Any]:
    """Drift checks for the installed shared skill: text freshness plus symlink health."""
    checks: list[dict[str, Any]] = []
    bridge_dir = shared_bridge_dir(root, create=False, required=False)
    if bridge_dir is None or not bridge_dir.exists():
        checks.append({"check": "shared_bridge_dir", "status": "fail", "detail": "no shared Agent-Bridge dir found; run `agent code harness install-skill`"})
        return {"ok": False, "shared_bridge_dir": str(bridge_dir) if bridge_dir else "", "checks": checks}
    skill_path = bridge_dir / "SKILL.md"
    if not skill_path.exists():
        checks.append({"check": "skill_installed", "status": "fail", "detail": f"missing {skill_path}"})
    elif skill_path.read_text(encoding="utf-8") != render_agent_bridge_skill():
        checks.append({"check": "skill_fresh", "status": "fail", "detail": f"{skill_path} drifted from generated text; rerun `agent code harness install-skill`"})
    else:
        checks.append({"check": "skill_fresh", "status": "ok", "detail": str(skill_path)})
    for link in _skill_link_paths(link_client):
        name = f"skill_link:{link.parent.parent.name}"
        if not link.exists() and not link.is_symlink():
            checks.append({"check": name, "status": "skip", "detail": f"{link} not present"})
        else:
            try:
                target = link.resolve() if link.exists() else None
                ok = target == bridge_dir.resolve()
            except OSError:
                target = None
                ok = False
            kind = "symlink" if link.is_symlink() else "directory link"
            detail = f"{link} -> {target or 'broken'} ({kind})"
            checks.append({"check": name, "status": "ok" if ok else "fail", "detail": detail})
    ok = all(row["status"] != "fail" for row in checks)
    return {"ok": ok, "shared_bridge_dir": str(bridge_dir), "checks": checks}


def format_shared_skill_check(result: dict[str, Any]) -> str:
    lines = [f"Shared skill check: {'ok' if result['ok'] else 'drift detected'}"]
    for row in result["checks"]:
        lines.append(f"[{row['status']:>4}] {row['check']}: {row['detail']}")
    return "\n".join(lines) + "\n"


def doctor_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code doctor", description="Check the local harness install for drift and misconfiguration.")
    parser.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    shared_root = resolve_shared_skills_root(args.root, required=False, require_bridge_dir=True)
    report = run_doctor(
        skill_text=render_agent_bridge_skill(),
        shared_root=shared_root,
        bridge_dir=BRIDGE_DIR,
        config_loader=lambda: load_config(DEFAULT_CONFIG),
    )
    roots = resolve_shared_roots()
    for kind, row in roots["roots"].items():
        report["checks"].append(
            {
                "check": f"shared_root:{kind}",
                "status": "ok" if row["exists"] else "fail",
                "detail": f"{row['selected']} ({'explicit' if row['explicit'] else 'discovered'})",
            }
        )
    for conflict in roots["conflicts"]:
        report["checks"].append(
            {
                "check": f"shared_root_conflict:{conflict['kind']}",
                "status": "fail",
                "detail": ", ".join(conflict["paths"]),
            }
        )
    manifest = load_surface_manifest()
    configured_clients = {agent["id"] for agent in load_config(DEFAULT_CONFIG)["agents"]}
    declared_clients = {row["client"] for row in manifest["surfaces"]}
    missing_clients = sorted(configured_clients - declared_clients)
    report["checks"].append(
        {
            "check": "surface_manifest_coverage",
            "status": "ok" if not missing_clients else "fail",
            "detail": "all configured agents have startup surfaces" if not missing_clients else f"missing: {', '.join(missing_clients)}",
        }
    )
    for surface in manifest["surfaces"]:
        if not surface.get("installable") or (surface.get("enabled_by_default") is False and not surface_hook_installed(surface)):
            status = "skip"
        else:
            status = "ok" if surface_hook_installed(surface) else "fail"
        report["checks"].append(
            {
                "check": f"startup_surface:{surface['client']}:{surface['surface']}",
                "status": status,
                "detail": surface.get("startup_mechanism", "unknown"),
            }
        )
    report["failures"] = sum(1 for row in report["checks"] if row["status"] == "fail")
    report["ok"] = report["failures"] == 0
    _json_print(report) if args.json else print(format_doctor(report), end="")
    return 0 if report["ok"] else 1


def format_shared_skill_install(result: dict[str, Any]) -> str:
    lines = [
        f"Shared Agent Bridge: {result['shared_bridge_dir']}",
        f"Skill: {result['skill_path']} ({'updated' if result['changed'] else 'unchanged'})",
    ]
    for link in result.get("links", []):
        lines.append(f"{link['path']}: {link['status']}")
    return "\n".join(lines) + "\n"


def update_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code update",
        description="Safely inspect or fast-forward the canonical Agent Bridge checkout.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "check", "apply"):
        command = sub.add_parser(action)
        command.add_argument(
            "--repo",
            default=os.environ.get("AGENT_BRIDGE_REPO", str(BRIDGE_DIR.parent)),
            help="Canonical Agent Bridge checkout.",
        )
        command.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
        command.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
        command.add_argument("--force", action="store_true", help="Ignore the recent-check cache.")
        command.add_argument(
            "--expected-remote",
            default=os.environ.get("AGENT_BRIDGE_EXPECTED_REMOTE", DEFAULT_EXPECTED_REMOTE),
        )
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = update_bridge(
        Path(args.repo),
        action=args.action,
        force=args.force,
        interval_seconds=max(0, args.interval_seconds),
        timeout=max(1, args.timeout),
        expected_remote=args.expected_remote,
    )
    _json_print(result) if args.json else print(format_update(result), end="")
    success = {"current", "current_cached", "updated"}
    if args.action == "check":
        success.add("update_available")
    return 0 if result.get("status") in success else 1


def repos_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code repos",
        description="Inspect canonical repos beyond Agent Bridge for drift and uncommitted canonical work.",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "list"):
        command = sub.add_parser(action)
        command.add_argument("--json", action="store_true")
        if action == "status":
            command.add_argument("--only", action="append", help="Limit to a repo id; repeatable.")
            command.add_argument("--force", action="store_true", help="Ignore the recent-check cache.")
            command.add_argument("--timeout", type=int, default=DEFAULT_MANAGED_TIMEOUT_SECONDS)
            command.add_argument("--interval-seconds", type=int, default=DEFAULT_MANAGED_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    if args.action == "list":
        registry = load_managed_registry()
        if args.json:
            _json_print({"repos": registry})
            return 0
        for entry in registry:
            print(f"{entry['id']:<24} {entry['mode']:<7} {entry['branch']:<6} {entry['path']}")
        return 0

    results = sync_managed_repos(
        timeout=max(1, args.timeout),
        interval_seconds=max(0, args.interval_seconds),
        force=args.force,
        only=args.only,
    )
    if args.json:
        _json_print({"repos": results})
    else:
        for result in results:
            status = str(result.get("status", "unknown"))
            print(f"{result['id']:<24} {status:<22} behind={result.get('behind', 0)} ahead={result.get('ahead', 0)} dirty={result.get('dirty_files', 0)}")
            note = describe_managed_repo(result)
            if note:
                print(f"    {note}")
            for line in result.get("uncommitted_canonical") or []:
                print(f"    uncommitted canonical: {line}")
        print(format_managed_repos(results).strip())
    # Report-only repos legitimately sit behind or dirty, so a warning is not a
    # command failure. Only a genuinely broken configuration is.
    broken = {"not_git", "blocked_remote", "blocked_branch", "error"}
    return 1 if any(str(r.get("status")) in broken for r in results) else 0


def session_start_context(
    client: str,
    *,
    surface: str,
    registration: dict[str, Any] | None = None,
    readiness: dict[str, Any] | None = None,
    update: dict[str, Any] | None = None,
    managed: list[dict[str, Any]] | None = None,
    retired_skills: dict[str, Any] | None = None,
) -> str:
    cwd = os.environ.get("PWD") or str(Path.cwd())
    git_root = _git_root_for_path(cwd)
    location = f" Current git root: {git_root}." if git_root else ""
    registry = ""
    if registration:
        registry = (
            " Shared registry heartbeat written to "
            f"`{registration['registry_file']}` for machine `{registration['machine_id']}`."
        )
    readiness_text = ""
    if readiness:
        readiness_text = (
            f" Cached session readiness is `{readiness.get('overall', 'unknown')}`; "
            "run `agent code preflight work` before authenticated source work."
        )
    update_result = update or {}
    revision = str(update_result.get("local_revision") or bridge_revision(BRIDGE_DIR.parent))[:12] or "unknown"
    update_text = (
        f" Agent Bridge revision: `{revision}`; startup refresh: "
        f"`{update_result.get('status', 'unknown')}`."
    )
    purged = (retired_skills or {}).get("purged") or []
    retirement_text = (
        f" Deprecated-skill purge removed {len(purged)} installation(s)." if purged else ""
    )
    return (
        "Agent Bridge session bootstrap: global command `agent` is available for bounded local "
        "agent coordination. Use `agent code bridge` for one-shot headless review/code turns and "
        "`agent code loop` for adversarial loops; loop dispatch defaults to `--spawn-policy auto`, "
        "which falls back to one analysis-only adversarial agent unless the task is concrete enough "
        "for a full builder/critic/verifier spawn. Mailbox MCP, when registered, should point to "
        f"`{BRIDGE_DIR / 'mailbox_mcp.py'}`. Use `agent code harness status` to inspect shared "
        "OneDrive harness registrations. This startup hook never spawns agents or mutates the active project; "
        "its bounded updater only fast-forwards clean canonical checkouts and never modifies report-only repos. "
        f"Client: {client}. Surface: {surface}."
        f"{location}{update_text}{retirement_text}{registry}{readiness_text}{format_managed_repos(managed or [])}"
    )


def hook_session_start(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code hook session-start", description="Emit SessionStart hook context.")
    parser.add_argument("--client", required=True)
    parser.add_argument("--surface", default="auto", help="cli, gui, local-api, bridge, or auto detection")
    parser.add_argument("--startup-mechanism", default="native-session-hook")
    parser.add_argument("--plain", action="store_true", help="Print plain context instead of hook JSON")
    parser.add_argument("--skip-update", action="store_true", help="Skip the automatic refresh for this invocation.")
    parser.add_argument(
        "--skip-managed-repos",
        action="store_true",
        help="Skip the canonical managed-repo drift check for this invocation.",
    )
    parser.add_argument(
        "--managed-repo-timeout",
        type=int,
        default=int(os.environ.get("AGENT_BRIDGE_MANAGED_TIMEOUT", DEFAULT_MANAGED_TIMEOUT_SECONDS)),
    )
    parser.add_argument(
        "--managed-repo-interval-seconds",
        type=int,
        default=int(os.environ.get("AGENT_BRIDGE_MANAGED_INTERVAL_SECONDS", DEFAULT_MANAGED_INTERVAL_SECONDS)),
    )
    parser.add_argument(
        "--update-timeout",
        type=int,
        default=int(os.environ.get("AGENT_BRIDGE_UPDATE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)),
    )
    parser.add_argument(
        "--update-interval-seconds",
        type=int,
        default=int(os.environ.get("AGENT_BRIDGE_UPDATE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
    )
    args = parser.parse_args(argv)
    surface = infer_surface(args.client) if args.surface == "auto" else safe_fragment(args.surface)
    repo = Path(os.environ.get("AGENT_BRIDGE_REPO", str(BRIDGE_DIR.parent)))
    if args.skip_update:
        update = dict(load_update_state()) if os.environ.get("AGENT_BRIDGE_UPDATE_REEXEC") == "1" else {}
        update.update(
            {
                "status": update.get("status", "skipped"),
                "local_revision": update.get("local_revision") or bridge_revision(repo),
            }
        )
    else:
        try:
            update = update_bridge(
                repo,
                action="apply",
                interval_seconds=max(0, args.update_interval_seconds),
                timeout=max(1, args.update_timeout),
                expected_remote=os.environ.get("AGENT_BRIDGE_EXPECTED_REMOTE", DEFAULT_EXPECTED_REMOTE),
            )
        except Exception as exc:
            update = {
                "status": "error",
                "local_revision": bridge_revision(repo),
                "detail": f"startup refresh failed safely: {type(exc).__name__}",
            }
        if update.get("reexec_required") and os.environ.get("AGENT_BRIDGE_UPDATE_REEXEC") != "1":
            env = dict(os.environ)
            env["AGENT_BRIDGE_UPDATE_REEXEC"] = "1"
            os.execve(
                sys.executable,
                [
                    sys.executable,
                    "-m",
                    "agent_bridge.cli",
                    "code",
                    "hook",
                    "session-start",
                    *argv,
                    "--skip-update",
                ],
                env,
            )
    try:
        retired_skills = purge_retired_skills()
    except Exception as exc:
        retired_skills = {"status": "error", "purged": [], "errors": [type(exc).__name__]}
    registration = maybe_register_harness(args.client, surface=surface, startup_mechanism=args.startup_mechanism)
    try:
        readiness = run_preflight(
            args.client,
            surface,
            scope="session",
            project_dir=Path(os.environ.get("PWD") or Path.cwd()),
            timeout=2,
            ttl_seconds=900,
        )
    except (OSError, ValueError):
        readiness = None
    # Managed-repo drift never gates startup: it reports, and at most
    # fast-forwards a clean checkout. Any failure degrades to an empty list.
    managed: list[dict[str, Any]] = []
    if not args.skip_managed_repos:
        try:
            managed = sync_managed_repos(
                timeout=max(1, args.managed_repo_timeout),
                interval_seconds=max(0, args.managed_repo_interval_seconds),
            )
        except Exception:
            managed = []
    context = session_start_context(
        args.client,
        surface=surface,
        registration=registration,
        readiness=readiness,
        update=update,
        managed=managed,
        retired_skills=retired_skills,
    )
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


def _remove_command_hook(config: dict[str, Any], command: str) -> bool:
    hooks_config = config.get("hooks")
    if not isinstance(hooks_config, dict):
        return False
    entries = hooks_config.get("SessionStart")
    if not isinstance(entries, list):
        return False
    changed = False
    retained_entries: list[Any] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            retained_entries.append(entry)
            continue
        retained_hooks = [
            hook
            for hook in entry["hooks"]
            if not (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and hook.get("command") == command
            )
        ]
        removed_here = len(retained_hooks) != len(entry["hooks"])
        changed = changed or removed_here
        if (
            removed_here
            and not retained_hooks
            and entry.get("matcher") == "startup|resume"
            and set(entry).issubset({"matcher", "hooks"})
        ):
            continue
        retained_entry = dict(entry)
        retained_entry["hooks"] = retained_hooks
        retained_entries.append(retained_entry)
    if changed:
        hooks_config["SessionStart"] = retained_entries
    return changed


def _config_path(client: str) -> Path:
    home = Path.home()
    if client == "codex":
        return home / ".codex" / "hooks.json"
    if client == "claude":
        return home / ".claude" / "settings.json"
    if client == "grok":
        return home / ".grok" / "hooks" / "agent-bridge.json"
    raise BridgeError(f"unsupported hook client {client!r}")


def install_session_hook(client: str) -> bool:
    path = _config_path(client)
    default = {"hooks": {}}
    config = _load_json_config(path, default)
    changed = _ensure_command_hook(config, _hook_agent_command(client))
    if changed:
        _write_json_config(path, config)
    return changed


def uninstall_session_hook(client: str) -> bool:
    path = _config_path(client)
    if not path.exists():
        return False
    config = _load_json_config(path, {})
    changed = _remove_command_hook(config, _hook_agent_command(client))
    if changed:
        _write_json_config(path, config)
    return changed


def session_hook_installed(client: str) -> bool:
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


def _wrapper_text(surface: dict[str, Any]) -> str:
    client = str(surface["client"])
    surface_name = str(surface["surface"])
    agent_bin = os.environ.get("AGENT_BRIDGE_HOOK_AGENT", os.path.expanduser("~/.local/bin/agent"))
    if os.name == "nt":
        hook = (
            f'call "{agent_bin}" code hook session-start --client {client} --surface {surface_name} '
            "--startup-mechanism wrapper --plain >NUL 2>NUL"
        )
        if surface.get("wrapper_target"):
            launch = f'start "" msedge "{surface["wrapper_target"]}"'
        elif surface.get("wrapper_app"):
            launch = f'start "" "{surface["wrapper_app"]}"'
        else:
            command = shutil.which(client) or client
            launch = f'"{command}" %*'
        return f"@echo off\r\nrem agent-bridge startup wrapper: {client}/{surface_name}\r\n{hook}\r\n{launch}\r\n"
    hook = (
        f"{shlex.quote(agent_bin)} code hook session-start --client {shlex.quote(client)} "
        f"--surface {shlex.quote(surface_name)} --startup-mechanism wrapper --plain >/dev/null || true"
    )
    if surface.get("wrapper_target"):
        target = shlex.quote(str(surface["wrapper_target"]))
        if sys.platform == "darwin":
            launch = f"exec open -a 'Microsoft Edge' {target}"
        else:
            browser = shutil.which("microsoft-edge") or shutil.which("microsoft-edge-stable") or shutil.which("xdg-open") or "xdg-open"
            launch = f"exec {shlex.quote(browser)} {target}"
    elif surface.get("wrapper_app"):
        app = shlex.quote(str(surface["wrapper_app"]))
        launch = f"exec open -a {app}" if sys.platform == "darwin" else f"exec gtk-launch {app}"
    else:
        command = shutil.which(client) or client
        launch = f"exec {shlex.quote(command)} \"$@\""
    return f"#!/bin/sh\n# agent-bridge startup wrapper: {client}/{surface_name}\n{hook}\n{launch}\n"


def _surface_config_path(surface: dict[str, Any]) -> Path:
    path = Path(str(surface["config_path"])).expanduser()
    if os.name == "nt" and surface.get("startup_mechanism") in {"wrapper-required", "service-probe"} and path.suffix.lower() != ".cmd":
        path = path.with_name(path.name + ".cmd")
    return path


def install_surface_wrapper(surface: dict[str, Any]) -> bool:
    path = _surface_config_path(surface)
    expected = _wrapper_text(surface)
    if path.exists() and path.read_text(encoding="utf-8") == expected:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{utc_stamp()}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(expected, encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(0o755)
    tmp.replace(path)
    return True


def uninstall_surface_wrapper(surface: dict[str, Any]) -> str:
    path = _surface_config_path(surface)
    if not path.exists():
        return "not-installed"
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return "modified-preserved"
    if current != _wrapper_text(surface):
        return "modified-preserved"
    path.unlink()
    return "removed"


def surface_hook_installed(surface: dict[str, Any]) -> bool:
    mechanism = surface.get("startup_mechanism")
    if mechanism == "native-session-hook":
        return session_hook_installed(str(surface["client"]))
    if mechanism in {"wrapper-required", "service-probe"} and surface.get("config_path"):
        path = _surface_config_path(surface)
        try:
            return path.exists() and path.read_text(encoding="utf-8") == _wrapper_text(surface)
        except OSError:
            return False
    return False


def hooks_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code hooks",
        description="Install, uninstall, or inspect Agent Bridge session hooks.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    install = sub.add_parser("install")
    install.add_argument("--client", default="all", help="codex, claude, grok, both, or all")
    install.add_argument("--include-inactive", action="store_true", help="Include surfaces disabled by default, such as Claude")
    install.add_argument("--json", action="store_true")
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--client", default="all", help="client name, both, or all")
    uninstall.add_argument("--json", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--client", default="all", help="client name, both, or all")
    status.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest = load_surface_manifest()
    if args.client == "both":
        clients = ["codex", "claude"]
    elif args.client == "all":
        clients = sorted({row["client"] for row in manifest["surfaces"]})
    else:
        clients = [args.client]
    if args.cmd == "install":
        rows: list[dict[str, Any]] = []
        for client in clients:
            client_surfaces = [row for row in manifest["surfaces"] if row.get("client") == client]
            inactive = client_surfaces and all(row.get("enabled_by_default") is False for row in client_surfaces)
            if inactive and not args.include_inactive and args.client == "all":
                for surface in client_surfaces:
                    rows.append({"client": client, "surface": surface["surface"], "status": "inactive", "changed": False, "config_path": str(_surface_config_path(surface))})
                continue
            native_changed: bool | None = None
            for surface in client_surfaces:
                if not surface.get("installable"):
                    rows.append({"client": client, "surface": surface["surface"], "status": surface.get("startup_mechanism", "unsupported"), "changed": False})
                    continue
                mechanism = surface.get("startup_mechanism")
                if mechanism == "native-session-hook":
                    if native_changed is None:
                        native_changed = install_session_hook(client)
                    changed = native_changed
                else:
                    changed = install_surface_wrapper(surface)
                rows.append(
                    {
                        "client": client,
                        "surface": surface["surface"],
                        "status": "installed" if changed else "already-installed",
                        "changed": changed,
                        "config_path": str(_surface_config_path(surface)),
                    }
                )
        if args.json:
            _json_print({"schema_version": manifest.get("schema_version", "1.0"), "hooks": rows})
        else:
            for row in rows:
                path = f" ({row['config_path']})" if row.get("config_path") else ""
                print(f"{row['client']}: {row['status']}{path}")
        return 0
    if args.cmd == "uninstall":
        rows = []
        for client in clients:
            client_surfaces = [row for row in manifest["surfaces"] if row.get("client") == client]
            native_changed: bool | None = None
            for surface in client_surfaces:
                if not surface.get("installable"):
                    rows.append(
                        {
                            "client": client,
                            "surface": surface["surface"],
                            "status": surface.get("startup_mechanism", "unsupported"),
                            "changed": False,
                        }
                    )
                    continue
                mechanism = surface.get("startup_mechanism")
                if mechanism == "native-session-hook":
                    if native_changed is None:
                        native_changed = uninstall_session_hook(client)
                    status_value = "removed" if native_changed else "not-installed"
                else:
                    status_value = uninstall_surface_wrapper(surface)
                rows.append(
                    {
                        "client": client,
                        "surface": surface["surface"],
                        "status": status_value,
                        "changed": status_value == "removed",
                        "config_path": str(_surface_config_path(surface)),
                    }
                )
        if args.json:
            _json_print({"schema_version": manifest.get("schema_version", "1.0"), "hooks": rows})
        else:
            for row in rows:
                path = f" ({row['config_path']})" if row.get("config_path") else ""
                print(f"{row['client']}: {row['status']}{path}")
        return 1 if any(row["status"] == "modified-preserved" for row in rows) else 0
    try:
        registry_rows = load_harness_registry(stale_minutes=1440).get("harnesses", [])
    except (BridgeError, OSError, AssertionError):
        registry_rows = []
    registrations = {
        (str(row.get("client", "")), str(row.get("surface", ""))): row
        for row in registry_rows
        if row.get("machine_id") == _harness_machine_id()
    }
    rows = []
    for surface in manifest["surfaces"]:
        if surface.get("client") not in clients:
            continue
        installable = bool(surface.get("installable"))
        installed = surface_hook_installed(surface) if installable else False
        registration = registrations.get((str(surface.get("client", "")), str(surface.get("surface", ""))))
        registration_status = "fresh" if registration and registration.get("fresh") else ("stale" if registration else "missing")
        status_value = "installed" if installed else (
            "inactive"
            if surface.get("enabled_by_default") is False
            else ("missing" if installable else surface.get("startup_mechanism", "unsupported"))
        )
        if installed and registration_status == "stale":
            status_value = "stale"
        rows.append(
            {
                **surface,
                "config_path": str(_surface_config_path(surface)) if surface.get("config_path") else "",
                "status": status_value,
                "registration_status": registration_status,
                "registration_age_seconds": registration.get("age_seconds") if registration else None,
                "registration_machine_id": registration.get("machine_id", "") if registration else "",
                "registration_project": registration.get("git_root", "") if registration else "",
                "registration_bridge_revision": registration.get("bridge_revision", "") if registration else "",
                "registration_deployed_revision": registration.get("deployed_revision", "") if registration else "",
                "registration_update_status": registration.get("update_status", "") if registration else "",
            }
        )
    result = {"schema_version": manifest.get("schema_version", "1.0"), "hooks": rows}
    if args.json:
        _json_print(result)
    else:
        for row in rows:
            path = f" ({row['config_path']})" if row.get("config_path") else ""
            print(f"{row['client']}/{row['surface']}: {row['status']} [{row['startup_mechanism']}]{path}")
    return 0 if all(row["status"] != "missing" for row in rows) else 1


def harness_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code harness", description="Register and inspect shared Agent Bridge harness status.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    install = sub.add_parser("install-skill", help="Install the shared Agent Bridge skill package.")
    install.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    install.add_argument("--link-client", choices=["none", "codex", "claude", "grok", "agents", "all"], default="all")
    install.add_argument("--check", action="store_true", help="Report skill/symlink drift without writing anything.")
    install.add_argument("--json", action="store_true")

    register = sub.add_parser("register", help="Write a shared registry heartbeat for this harness.")
    register.add_argument("--client", required=True, help="Harness/client name, e.g. codex, grok, ollama.")
    register.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    register.add_argument("--status", default="active")
    register.add_argument("--surface", default="", help="cli, gui, local-api, bridge, or another surface id")
    register.add_argument("--startup-mechanism", default="manual")
    register.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show shared Agent Bridge harness registry rows.")
    status.add_argument("--root", help="SharedAgentSkills root. Defaults to OneDrive/env discovery.")
    status.add_argument("--stale-minutes", type=int, default=1440)
    status.add_argument("--no-prune", action="store_true", help="Retain registry rows older than the retention window.")
    status.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "install-skill":
        if args.check:
            result = check_shared_skill(args.root, link_client=args.link_client)
            _json_print(result) if args.json else print(format_shared_skill_check(result), end="")
            return 0 if result["ok"] else 1
        result = install_shared_skill(args.root, link_client=args.link_client)
        _json_print(result) if args.json else print(format_shared_skill_install(result), end="")
        return 0
    if args.cmd == "register":
        record = register_harness(
            args.client,
            root=args.root,
            status=args.status,
            surface=args.surface,
            startup_mechanism=args.startup_mechanism,
        )
        _json_print(record) if args.json else print(f"{record['client']}: registered ({record['registry_file']})")
        return 0
    data = load_harness_registry(args.root, stale_minutes=args.stale_minutes, prune=not args.no_prune)
    _json_print(data) if args.json else print(format_harness_registry(data), end="")
    return 0


def format_preflight(report: dict[str, Any]) -> str:
    lines = [
        f"Preflight: {report.get('overall', 'unknown')}",
        f"Scope: {report.get('scope', 'unknown')}",
        f"Client/surface: {report.get('client', 'unknown')}/{report.get('surface', 'unknown')}",
        f"Generated: {report.get('generated_at', '')}",
    ]
    if report.get("stale"):
        lines.append("Cache: stale")
    lines.append("")
    for row in report.get("checks", []):
        error = f" ({row['error_class']})" if row.get("error_class") else ""
        required = "required" if row.get("required") else "advisory"
        lines.append(f"[{row.get('status', 'unknown'):>8}] {row.get('name', '')} [{required}]{error}: {row.get('detail', '')}")
    return "\n".join(lines) + "\n"


def preflight_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code preflight",
        description="Inspect session, authenticated work, and shared-root readiness without exposing secrets.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("session", "work"):
        run = sub.add_parser(name)
        run.add_argument("--client", default=os.environ.get("AGENT_BRIDGE_CALLER", "codex"))
        run.add_argument("--surface", default="auto")
        run.add_argument("--project-dir")
        run.add_argument("--timeout", type=int, default=20)
        run.add_argument("--ttl-seconds", type=int, default=900)
        run.add_argument("--expected-github-login", default=os.environ.get("AGENT_BRIDGE_EXPECTED_GITHUB_LOGIN", ""))
        run.add_argument("--context-manifest", default=os.environ.get("AGENT_BRIDGE_CONTEXT_MANIFEST", ""))
        run.add_argument("--require-context", action="store_true")
        run.add_argument("--refresh", action="store_true", help="Ignore a fresh cache entry")
        run.add_argument("--json", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--client", default=os.environ.get("AGENT_BRIDGE_CALLER", "codex"))
    status.add_argument("--surface", default="auto")
    status.add_argument("--scope", choices=["session", "work"], default="session")
    status.add_argument("--json", action="store_true")
    publish = sub.add_parser("publish")
    publish.add_argument("--client", default=os.environ.get("AGENT_BRIDGE_CALLER", "codex"))
    publish.add_argument("--surface", default="auto")
    publish.add_argument("--scope", choices=["session", "work"], default="work")
    publish.add_argument("--data-root", default="")
    publish.add_argument("--json", action="store_true")
    flush = sub.add_parser("flush", help="Retry locally queued readiness publications")
    flush.add_argument("--data-root", default="")
    flush.add_argument("--json", action="store_true")
    aggregate = sub.add_parser("aggregate", help="Rebuild a redacted multi-machine readiness view")
    aggregate.add_argument("--data-root", default="")
    aggregate.add_argument("--write", action="store_true")
    aggregate.add_argument("--json", action="store_true")
    roots = sub.add_parser("roots")
    roots.add_argument("--create", action="store_true")
    roots.add_argument("--set-skills")
    roots.add_argument("--set-data")
    roots.add_argument("--set-conversations")
    roots.add_argument("--json", action="store_true")
    configure = sub.add_parser("configure")
    configure.add_argument("--github-login")
    configure.add_argument("--require-github", action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--context-manifest")
    configure.add_argument("--require-context", action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "configure":
        values = {
            "github_login": args.github_login,
            "require_github": args.require_github,
            "context_manifest": str(Path(args.context_manifest).expanduser().resolve()) if args.context_manifest else None,
            "require_context": args.require_context,
        }
        result = configure_readiness(values)
        _json_print(result) if args.json else print(f"readiness config: {result['config_file']}")
        return 0

    if args.cmd == "roots":
        configured = {
            kind: value
            for kind, value in {
                "skills": args.set_skills,
                "data": args.set_data,
                "conversations": args.set_conversations,
            }.items()
            if value
        }
        config_result = configure_shared_roots(configured) if configured else None
        result = resolve_shared_roots(create=args.create)
        if config_result:
            result["config_file"] = config_result["config_file"]
        if args.json:
            _json_print(result)
        else:
            print(f"Shared roots: {'ok' if result['ok'] else 'attention required'}")
            for kind, row in result["roots"].items():
                source = "explicit" if row["explicit"] else "discovered"
                print(f"{kind}: {row['selected']} ({source}; {'exists' if row['exists'] else 'missing'})")
            for conflict in result["conflicts"]:
                print(f"conflict:{conflict['kind']}: {', '.join(conflict['paths'])}")
        return 0 if result["ok"] else 1

    if args.cmd in {"flush", "aggregate"}:
        data_root = args.data_root
        if not data_root:
            roots_result = resolve_shared_roots()
            data_row = roots_result["roots"]["data"]
            if not data_row["explicit"]:
                raise BridgeError("SharedAgentData must be explicitly configured for shared readiness operations")
            data_root = data_row["selected"]
        result = flush_readiness_queue(data_root=data_root) if args.cmd == "flush" else aggregate_readiness(data_root=data_root, write=args.write)
        if args.json:
            _json_print(result)
        elif args.cmd == "flush":
            print(f"readiness publications flushed: {result['flushed']}")
        else:
            print(f"readiness aggregate rows: {len(result['rows'])}")
        return 0

    surface = infer_surface(args.client) if args.surface == "auto" else safe_fragment(args.surface)
    if args.cmd == "status":
        report = load_cached_preflight(args.client, surface, scope=args.scope, allow_stale=True)
        if report is None:
            raise BridgeError(f"no cached preflight for {args.client}/{surface}; run `agent code preflight session` or `work`")
        _json_print(report) if args.json else print(format_preflight(report), end="")
        return 0 if report.get("overall") != "blocked" else 1
    if args.cmd == "publish":
        report = load_cached_preflight(args.client, surface, scope=args.scope, allow_stale=True)
        if report is None:
            raise BridgeError(f"no cached preflight for {args.client}/{surface}; publication never runs live probes")
        result = publish_readiness(report, data_root=args.data_root)
        if args.json:
            _json_print(result)
        elif result.get("published"):
            print(f"published redacted readiness: {result['published_file']}")
        else:
            print(f"shared root unavailable; queued redacted readiness: {result['queued_file']}")
        return 0 if result.get("published") else 2

    project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else discover_project_dir()
    report = None
    if args.cmd == "session" and not args.refresh:
        report = load_cached_preflight(args.client, surface, scope="session")
    if report is None:
        report = run_preflight(
            args.client,
            surface,
            scope=args.cmd,
            project_dir=project_dir,
            timeout=args.timeout,
            ttl_seconds=args.ttl_seconds,
            expected_github_login=args.expected_github_login,
            context_manifest=args.context_manifest,
            require_context=args.require_context,
        )
    _json_print(report) if args.json else print(format_preflight(report), end="")
    return 0 if report.get("overall") != "blocked" else 1


def context_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code context",
        description="Generate or verify harness-native context adapters from one canonical manifest.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("install", "check", "status"):
        command = sub.add_parser(name)
        command.add_argument("--manifest", required=True)
        command.add_argument("--client", default="")
        if name == "install":
            command.add_argument("--force", action="store_true", help="Replace an existing generated block after review")
        command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    manifest = Path(args.manifest).expanduser().resolve()
    try:
        if args.cmd == "status":
            result = context_status(manifest, client=args.client)
        else:
            result = install_context_adapters(
                manifest,
                client=args.client,
                check=args.cmd == "check",
                force=bool(getattr(args, "force", False)),
            )
    except ContextAdapterError as exc:
        raise BridgeError(str(exc)) from exc
    if args.json:
        _json_print(result)
    else:
        print(f"Context adapters: {'ok' if result['ok'] else 'attention required'}")
        print(f"Canonical hash: {result.get('canonical_hash', '')}")
        for row in result["adapters"]:
            print(f"{row['client']}: {row['status']} ({row['path']})")
        for overlap in result.get("overlaps", []):
            print(f"overlap:{overlap['module']}: {overlap['source']} ({overlap['path']})")
    return 0 if result["ok"] else 1


def trace_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code trace", description="Inspect agent bridge trace events.")
    parser.add_argument("--run-id")
    parser.add_argument("--type", dest="event_type")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--envelope", action="store_true", help="Emit portable event envelopes with traceparent context (JSON)")
    args = parser.parse_args(argv)
    if args.envelope:
        _json_print(export_envelopes(run_id=args.run_id, event_type=args.event_type))
        return 0
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


def gateway_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code gateway", description="Inspect or exercise optional LLM gateway routing.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="Show direct vs gateway-routed agent profiles.")
    status.add_argument("--config", default=str(DEFAULT_CONFIG))
    status.add_argument("--json", action="store_true")

    chat = sub.add_parser("chat", help="Exercise an OpenAI-compatible gateway profile.")
    chat.add_argument("--config", default=str(DEFAULT_CONFIG))
    chat.add_argument("--to", required=True)
    chat.add_argument("--prompt", required=True)
    chat.add_argument("--system", default="You are a concise local test adapter.")
    chat.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    agents = agent_map(load_config(Path(args.config)))
    if args.cmd == "status":
        rows = gateway_status_rows(agents)
        _json_print(rows) if args.json else print(format_gateway_status(rows), end="")
        return 0

    if args.to not in agents:
        raise BridgeError(f"unknown gateway target {args.to!r}")
    profile = gateway_profile(agents[args.to])
    if not profile:
        raise BridgeError(f"agent {args.to!r} has no gateway profile")
    result = call_openai_gateway(profile=profile, prompt=args.prompt, system_prompt=args.system)
    _json_print(result) if args.json else print(result.get("output", ""))
    return 0


def usage_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code usage", description="Inspect Agent Bridge run-level usage scorecards.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = load_usage(args.run_id)
    _json_print(payload) if args.json else print(format_scorecard(payload), end="")
    return 0


def cache_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code cache", description="Inspect deterministic exact/tool-result cache entries.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    key_parser = sub.add_parser("key", help="Compute a cache key for repeatable calls or tool results.")
    key_parser.add_argument("--class", dest="cache_class", default="tool-result")
    key_parser.add_argument("--model", default="")
    key_parser.add_argument("--provider", default="")
    key_parser.add_argument("--prefix", default="")
    key_parser.add_argument("--task", default="")
    key_parser.add_argument("--tool", default="")
    key_parser.add_argument("--tool-args", default="")
    key_parser.add_argument("--project-dir")

    put = sub.add_parser("put")
    put.add_argument("--key", required=True)
    put.add_argument("--value", required=True)
    put.add_argument("--class", dest="cache_class", default="tool-result")
    put.add_argument("--ttl-seconds", type=int, default=DEFAULT_CACHE_TTL_SECONDS)
    put.add_argument("--semantic-text", default="")
    put.add_argument("--tool-result", action="store_true")
    put.add_argument("--json", action="store_true")

    get = sub.add_parser("get")
    get.add_argument("--key", required=True)
    get.add_argument("--semantic-query")
    get.add_argument("--semantic", action="store_true")
    get.add_argument("--threshold", type=float, default=0.9)
    get.add_argument("--tool-result", action="store_true")
    get.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "key":
        project_dir = Path(args.project_dir).expanduser().resolve() if args.project_dir else None
        print(
            cache_key(
                cache_class=args.cache_class,
                model=args.model,
                provider=args.provider,
                prefix=args.prefix,
                task=args.task,
                project_dir=project_dir,
                tool=args.tool,
                tool_args=args.tool_args,
            )
        )
        return 0
    path = tool_cache_path() if getattr(args, "tool_result", False) else exact_cache_path()
    if args.cmd == "put":
        value: Any
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
        entry = cache_store(
            args.key,
            value,
            cache_class=args.cache_class,
            ttl_seconds=args.ttl_seconds,
            semantic_text=args.semantic_text,
            path=path,
        )
        _json_print(entry) if args.json else print(args.key)
        return 0
    result = cache_lookup(
        args.key,
        semantic_query=args.semantic_query,
        semantic_enabled=args.semantic,
        semantic_threshold=args.threshold,
        path=path,
    )
    _json_print(result) if args.json else print(result.get("status", "miss"))
    return 0


def optimize_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent code optimize", description="Dry-run token optimization helpers.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    route = sub.add_parser("route")
    route.add_argument("--policy", default=os.environ.get("AGENT_BRIDGE_ROUTE_POLICY", "standard"))
    route.add_argument("--prompt", required=True)
    route.add_argument("--cache-status", default="miss")
    route.add_argument("--json", action="store_true")

    cacheable = sub.add_parser("cacheability")
    cacheable.add_argument("--prefix", required=True)
    cacheable.add_argument("--task", required=True)
    cacheable.add_argument("--provider", default="")
    cacheable.add_argument("--minimum-tokens", type=int, default=1024)
    cacheable.add_argument("--json", action="store_true")

    compress = sub.add_parser("compress")
    compress.add_argument("--mode", choices=["off", "trim", "summarize", "external"], default="off")
    compress.add_argument("--max-chars", type=int, default=8000)
    compress.add_argument("--command")
    compress.add_argument("--text")
    compress.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "route":
        result = choose_route(policy=args.policy, prompt=args.prompt, cache_status=args.cache_status)
        _json_print(result) if args.json else print(f"{result['route']}: {result['reason']}")
        return 0
    if args.cmd == "cacheability":
        result = cacheability_report(
            prefix=args.prefix,
            task=args.task,
            provider=args.provider,
            minimum_tokens=args.minimum_tokens,
        )
        _json_print(result) if args.json else print(f"{result['prefix_fingerprint']}\t{result['reason']}")
        return 0
    text = args.text if args.text is not None else (sys.stdin.read() if not sys.stdin.isatty() else "")
    result = compress_context(text, mode=args.mode, max_chars=args.max_chars, external_command=args.command)
    if args.json:
        _json_print(result)
    else:
        print(result["compressed"], end="" if result["compressed"].endswith("\n") else "\n")
        if result.get("warning"):
            print(f"warning: {result['warning']}", file=sys.stderr)
    return 0


def _project_overrides(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SessionRecoveryError("--project must use SESSION_ID=PATH")
        session_id, path = value.split("=", 1)
        session_id = session_id.strip()
        path = path.strip()
        if not session_id or not path:
            raise SessionRecoveryError("--project must use SESSION_ID=PATH")
        if session_id in result and result[session_id] != str(Path(path).expanduser()):
            raise SessionRecoveryError(f"conflicting --project overrides for session {session_id}")
        result[session_id] = str(Path(path).expanduser())
    return result


def sessions_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent code sessions",
        description="Inventory native harness sessions and stage evidence-first continuation handoffs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    inventory = sub.add_parser("inventory", help="List recent local Claude sessions without copying message bodies.")
    inventory.add_argument(
        "--claude-data-root",
        default=os.environ.get("AGENT_BRIDGE_CLAUDE_DATA_ROOT", str(DEFAULT_CLAUDE_DATA_ROOT)),
    )
    inventory.add_argument(
        "--claude-projects-root",
        default=os.environ.get("AGENT_BRIDGE_CLAUDE_PROJECTS_ROOT", str(DEFAULT_CLAUDE_PROJECTS_ROOT)),
    )
    inventory.add_argument("--since-hours", type=float, default=168.0)
    inventory.add_argument("--all-time", action="store_true", help="Do not filter by last activity time.")
    inventory.add_argument("--include-archived", action="store_true")
    inventory.add_argument("--session-id", action="append", default=[])
    inventory.add_argument("--title", default="", help="Case-insensitive title substring.")
    inventory.add_argument("--limit", type=int, default=50)
    inventory.add_argument("--json", action="store_true")

    recover = sub.add_parser(
        "recover",
        help="Stage private handoffs for unfinished sessions and record completed sessions without duplicating them.",
    )
    recover.add_argument("--from", dest="source", choices=["claude"], default="claude")
    recover.add_argument("--to", dest="target", default="codex")
    recover.add_argument("--continue", dest="continue_ids", action="append", default=[])
    recover.add_argument(
        "--complete",
        dest="complete_ids",
        action="append",
        default=[],
        help="Operator-confirmed completed session; record evidence but do not create a continuation.",
    )
    recover.add_argument(
        "--selection",
        help="JSON file with sessions containing session_id, disposition continue|complete, and optional project_dir.",
    )
    recover.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="SESSION_ID=PATH",
        help="Override the exact project/worktree for one source session.",
    )
    recover.add_argument(
        "--claude-data-root",
        default=os.environ.get("AGENT_BRIDGE_CLAUDE_DATA_ROOT", str(DEFAULT_CLAUDE_DATA_ROOT)),
    )
    recover.add_argument(
        "--claude-projects-root",
        default=os.environ.get("AGENT_BRIDGE_CLAUDE_PROJECTS_ROOT", str(DEFAULT_CLAUDE_PROJECTS_ROOT)),
    )
    recover.add_argument("--verify-github", action="store_true", help="Read current PR state with the authenticated gh CLI.")
    recover.add_argument(
        "--enqueue",
        action="store_true",
        help="Create idempotent Agent Bridge task-ledger entries for continuation handoffs.",
    )
    recover.add_argument("--context-messages", type=int, default=8)
    recover.add_argument("--context-chars", type=int, default=12_000)
    recover.add_argument("--output-root", help="Private recovery artifact root. Defaults under Agent Bridge state.")
    recover.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    data_root = Path(args.claude_data_root).expanduser()
    projects_root = Path(args.claude_projects_root).expanduser()
    sessions = discover_claude_sessions(data_root=data_root, projects_root=projects_root)

    if args.cmd == "inventory":
        rows = filter_sessions(
            sessions,
            session_ids=args.session_id,
            since_hours=None if args.all_time else args.since_hours,
            include_archived=args.include_archived,
            title=args.title,
            limit=args.limit,
        )
        _json_print(rows) if args.json else print(format_session_inventory(rows), end="")
        return 0

    decisions: dict[str, str] = {}
    projects: dict[str, str] = {}
    if args.selection:
        selected_decisions, selected_projects = load_recovery_selection(Path(args.selection).expanduser())
        decisions.update(selected_decisions)
        projects.update(selected_projects)
    for disposition, session_ids in (("continue", args.continue_ids), ("complete", args.complete_ids)):
        for session_id in session_ids:
            session_id = session_id.strip()
            if not session_id:
                raise SessionRecoveryError("session ids must not be empty")
            existing = decisions.get(session_id)
            if existing and existing != disposition:
                raise SessionRecoveryError(f"conflicting dispositions for session {session_id}")
            decisions[session_id] = disposition
    projects.update(_project_overrides(args.project))
    unknown_projects = sorted(set(projects) - set(decisions))
    if unknown_projects:
        raise SessionRecoveryError(
            "project overrides require a matching continue/complete decision: " + ", ".join(unknown_projects)
        )
    selected = filter_sessions(
        sessions,
        session_ids=decisions,
        since_hours=None,
        include_archived=True,
        limit=None,
    )
    result = recover_sessions(
        selected,
        decisions=decisions,
        source=args.source,
        target=args.target,
        project_overrides=projects,
        verify_github=args.verify_github,
        enqueue=args.enqueue,
        context_messages=max(0, args.context_messages),
        context_chars=max(0, args.context_chars),
        output_root=Path(args.output_root).expanduser() if args.output_root else None,
    )
    _json_print(result) if args.json else print(format_recovery_result(result), end="")
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
    run.add_argument("--cache-mode", choices=["off", "exact", "semantic"], default=os.environ.get("AGENT_BRIDGE_WORKFLOW_CACHE_MODE", "off"))
    run.add_argument("--semantic-cache-threshold", type=float, default=float(os.environ.get("AGENT_BRIDGE_SEMANTIC_CACHE_THRESHOLD", "0.9")))
    run.add_argument("--compress", choices=["off", "trim", "summarize", "external"], default=os.environ.get("AGENT_BRIDGE_CONTEXT_COMPRESSOR", "off"))
    run.add_argument("--compress-max-chars", type=int, default=int(os.environ.get("AGENT_BRIDGE_COMPRESS_MAX_CHARS", "12000")))
    run.add_argument("--compress-command", default=os.environ.get("AGENT_BRIDGE_COMPRESS_COMMAND"))
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
    set_trace_context(meta)
    record_run_task("created", meta=meta, command="workflow", data={"workflow_id": args.workflow_id, "tier": args.tier})
    try:
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
            cache_mode=args.cache_mode,
            semantic_cache_threshold=args.semantic_cache_threshold,
            compression_mode=args.compress,
            compression_max_chars=args.compress_max_chars,
            compression_command=args.compress_command,
            meta=meta,
        )
    except Exception:
        record_run_task("failed", meta=meta, command="workflow")
        raise
    artifact_dir = result.get("artifact_dir")
    if artifact_dir:
        record_run_task("artifact_attached", meta=meta, command="workflow", artifact={"path": str(artifact_dir), "kind": "dir"})
    record_run_task("completed", meta=meta, command="workflow", data={"status": result.get("status")})
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
    parser.add_argument(
        "--route-policy",
        choices=["off", "no-route", "cache-first", "cheap-classifier", "standard", "premium"],
        default=os.environ.get("AGENT_BRIDGE_ROUTE_POLICY", "standard"),
        help="Optional token/spend route policy for loop phases.",
    )
    parser.add_argument("--prompt", help="Loop task prompt. If omitted in non-interactive mode, stdin is used.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned dispatches without invoking agents")
    parser.add_argument("--no-preflight", action="store_true", help="Skip the default authenticated work-readiness gate")
    parser.add_argument("--require-ready", action="store_true", help="Require fully ready reports for every dispatched phase")
    parser.add_argument("--refresh-readiness", action="store_true", help="Refresh readiness instead of using fresh caches")
    parser.add_argument("--preflight-timeout", type=int, default=20, help="Maximum seconds for each readiness probe")
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
    policy_rc = _enforce_dispatch_policy("loop", source=args.source, mode="code", meta=base_meta)
    if policy_rc is not None:
        return policy_rc
    set_trace_context(base_meta)
    record_run_task("created", meta=base_meta, command="loop", data={"spawn_policy": args.spawn_policy})
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
    if not args.dry_run:
        target_modes: dict[str, str] = {}
        phases_for_gate = (
            [(args.builder, "code"), (args.critic, "review"), (args.verifier, "review")]
            if decision.mode == "full_loop"
            else [(args.critic, "review")]
        )
        for target_id, target_mode in phases_for_gate:
            if target_mode == "code" or target_id not in target_modes:
                target_modes[target_id] = target_mode
        for target_id, target_mode in target_modes.items():
            if not _dispatch_readiness_gate(
                target_id,
                mode=target_mode,
                command="loop",
                meta=base_meta,
                no_preflight=args.no_preflight,
                require_ready=args.require_ready,
                refresh=args.refresh_readiness,
                timeout=args.preflight_timeout,
            ):
                emit_event("run.completed", run_id=base_meta.get("run_id"), meta=base_meta, data={"command": "loop", "return_code": 4, "dry_run": False})
                record_run_task("failed", meta=base_meta, command="loop", data={"return_code": 4, "reason": "readiness_refused"})
                return 4
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
                route_policy=args.route_policy,
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
    record_run_task("artifact_attached", meta=base_meta, command="loop", artifact={"path": str(events_path()), "kind": "trace"})
    record_run_task("completed" if rc == 0 else "failed", meta=base_meta, command="loop", data={"return_code": rc})
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
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "trace":
        return trace_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "findings":
        return findings_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "verdicts":
        return verdicts_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "gateway":
        return gateway_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "usage":
        return usage_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "cache":
        return cache_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "optimize":
        return optimize_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "update":
        return update_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "repos":
        return repos_cmd(argv[2:])
    if len(argv) >= 3 and argv[0] == "code" and argv[1] == "hook" and argv[2] == "session-start":
        return hook_session_start(argv[3:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "hooks":
        return hooks_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "harness":
        return harness_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "sessions":
        return sessions_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "preflight":
        return preflight_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "context":
        return context_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] == "doctor":
        return doctor_cmd(argv[2:])
    if len(argv) >= 2 and argv[0] == "code" and argv[1] in {"capabilities", "tasks", "transport", "policy", "eval", "daemon"}:
        return coord_cmd(argv[1], argv[2:])
    if len(argv) >= 1 and argv[0] == "bridge":
        return bridge(argv[1:])
    print("usage: agent code bridge [options]", file=sys.stderr)
    print("       agent code loop [options]", file=sys.stderr)
    print("       agent code repair [options]", file=sys.stderr)
    print("       agent code trace [options]", file=sys.stderr)
    print("       agent code findings <create|list|read> [options]", file=sys.stderr)
    print("       agent code verdicts <record|list> [options]", file=sys.stderr)
    print("       agent code gateway <status|chat> [options]", file=sys.stderr)
    print("       agent code usage --run-id <id> [options]", file=sys.stderr)
    print("       agent code cache <key|put|get> [options]", file=sys.stderr)
    print("       agent code optimize <route|cacheability|compress> [options]", file=sys.stderr)
    print("       agent code update <status|check|apply> [options]", file=sys.stderr)
    print("       agent code hook session-start [options]", file=sys.stderr)
    print("       agent code hooks <install|uninstall|status> [options]", file=sys.stderr)
    print("       agent code harness <install-skill|register|status> [options]", file=sys.stderr)
    print("       agent code sessions <inventory|recover> [options]", file=sys.stderr)
    print("       agent code preflight <session|work|status|publish|flush|aggregate|roots|configure> [options]", file=sys.stderr)
    print("       agent code context <install|check|status> --manifest PATH [options]", file=sys.stderr)
    print("       agent code doctor [options]", file=sys.stderr)
    print("       agent code capabilities [options]", file=sys.stderr)
    print("       agent code tasks <create|claim|update|request-input|cancel|resume|attach|inspect|list> [options]", file=sys.stderr)
    print("       agent code transport <send|receive|ack|status|smoke> [options]", file=sys.stderr)
    print("       agent code policy <check|show|sign|verify> [options]", file=sys.stderr)
    print("       agent code eval [options]", file=sys.stderr)
    print("       agent code daemon status", file=sys.stderr)
    print("       agent workflow <list|show|run|inspect> [options]", file=sys.stderr)
    print("       agent bridge [options]", file=sys.stderr)
    return 2


def main_entry() -> None:
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (BridgeError, SessionRecoveryError, WorkflowError, ValueError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (BridgeError, SessionRecoveryError, WorkflowError, ValueError) as exc:
        print(f"agent: {exc}", file=sys.stderr)
        raise SystemExit(2)
