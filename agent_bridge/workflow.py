"""Portable workflow runner for Agent Bridge.

The first bundled workflow is a harness-neutral port of Claude's
deep-research-lite workflow. The runner keeps model execution behind small
engine adapters so Codex, Claude, and future agents can share one command and
one result shape.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
from string import Template
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .agent_team import run_agent_team
from .correlation import ensure_run_meta, format_meta, safe_fragment
from .optimization import (
    build_scorecard,
    cache_key,
    cache_lookup,
    cache_store,
    compress_context,
    estimate_tokens,
    parse_usage_metadata,
    write_usage,
)
from .trace import emit_event, events_path, state_dir


WORKFLOW_DIR = Path(__file__).resolve().parent / "workflows"
DEFAULT_WORKFLOW_ID = "deep-research-lite"
SUPPORTED_WORKFLOW_RUNNERS = {"deep-research-lite", "agent-team"}
ENGINE_IDS = {"codex", "claude"}
DEFAULT_PRICING_USD_PER_MTOK = {
    "codex": {
        "input": 1.25,
        "output": 10.0,
        "source": "built-in Codex planning default; override with CLI flags or AGENT_WORKFLOW_*_USD_PER_MTOK",
    },
    "claude": {
        "input": 15.0,
        "output": 75.0,
        "source": "built-in Claude planning default; override with CLI flags or AGENT_WORKFLOW_*_USD_PER_MTOK",
    },
}


class WorkflowError(RuntimeError):
    """Raised when workflow loading or execution fails."""


@dataclass(frozen=True)
class ModelCall:
    prompt: str
    schema: dict[str, Any]
    label: str
    phase: str


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            text = data.strip()
            if text:
                self._parts.append(text)

    def text(self) -> str:
        return " ".join(self._parts)


def workflow_state_dir() -> Path:
    return state_dir() / "workflows"


def workflow_run_dir(run_id: str) -> Path:
    return workflow_state_dir() / safe_fragment(run_id)


def list_workflows() -> list[dict[str, Any]]:
    specs = []
    for path in sorted(WORKFLOW_DIR.glob("*.workflow.json")):
        spec = load_workflow(path.stem.replace(".workflow", ""))
        specs.append({"id": spec["id"], "name": spec["name"], "description": spec.get("description", "")})
    return specs


def load_workflow(workflow_id: str) -> dict[str, Any]:
    path = WORKFLOW_DIR / f"{workflow_id}.workflow.json"
    if not path.exists():
        raise WorkflowError(f"unknown workflow {workflow_id!r}")
    with path.open(encoding="utf-8") as handle:
        spec = json.load(handle)
    validate_workflow_spec(spec, path)
    return spec


def validate_workflow_spec(spec: dict[str, Any], path: Path | None = None) -> None:
    source = f"{path}: " if path else ""
    required = ["id", "name", "description", "phases", "tiers", "schemas", "prompts"]
    for key in required:
        if key not in spec:
            raise WorkflowError(f"{source}workflow spec is missing {key!r}")
    if not isinstance(spec["phases"], list) or not spec["phases"]:
        raise WorkflowError(f"{source}workflow spec must define phases")
    for tier in ("shallow", "standard", "deep"):
        if tier not in spec["tiers"]:
            raise WorkflowError(f"{source}workflow spec is missing tier {tier!r}")
    runner = str(spec.get("runner") or spec.get("id") or "")
    required_schemas = {
        "deep-research-lite": ("scope", "search", "extract", "dedup", "verdict", "report", "critic"),
        "agent-team": ("scope", "collector", "shard", "worker", "verifier", "integrate"),
    }.get(runner, ())
    required_prompts = {
        "deep-research-lite": ("scope", "search", "fetch", "dedup", "verify", "synthesize", "critic"),
        "agent-team": ("scope", "collect", "shard", "execute", "verify", "integrate"),
    }.get(runner, ())
    for schema in required_schemas:
        if schema not in spec["schemas"]:
            raise WorkflowError(f"{source}workflow spec is missing schema {schema!r}")
    for prompt in required_prompts:
        if prompt not in spec["prompts"]:
            raise WorkflowError(f"{source}workflow spec is missing prompt {prompt!r}")


def resolve_engine(engine: str, source: str | None = None) -> str:
    if engine and engine != "auto":
        if engine not in ENGINE_IDS:
            raise WorkflowError(f"unsupported engine {engine!r}")
        return engine
    if source in ENGINE_IDS:
        return str(source)
    caller = os.environ.get("AGENT_BRIDGE_CALLER", "").strip().lower()
    if caller in ENGINE_IDS:
        return caller
    return "codex"


def resolve_pricing(
    engine: str,
    *,
    input_usd_per_mtok: float | None = None,
    output_usd_per_mtok: float | None = None,
) -> dict[str, Any]:
    defaults = DEFAULT_PRICING_USD_PER_MTOK.get(engine, DEFAULT_PRICING_USD_PER_MTOK["codex"])
    input_rate = _first_float(
        input_usd_per_mtok,
        os.environ.get(f"AGENT_WORKFLOW_{engine.upper()}_INPUT_USD_PER_MTOK"),
        os.environ.get("AGENT_WORKFLOW_INPUT_USD_PER_MTOK"),
        defaults["input"],
    )
    output_rate = _first_float(
        output_usd_per_mtok,
        os.environ.get(f"AGENT_WORKFLOW_{engine.upper()}_OUTPUT_USD_PER_MTOK"),
        os.environ.get("AGENT_WORKFLOW_OUTPUT_USD_PER_MTOK"),
        defaults["output"],
    )
    source = defaults["source"]
    if input_usd_per_mtok is not None or output_usd_per_mtok is not None:
        source = "CLI override"
    elif (
        os.environ.get(f"AGENT_WORKFLOW_{engine.upper()}_INPUT_USD_PER_MTOK")
        or os.environ.get(f"AGENT_WORKFLOW_{engine.upper()}_OUTPUT_USD_PER_MTOK")
        or os.environ.get("AGENT_WORKFLOW_INPUT_USD_PER_MTOK")
        or os.environ.get("AGENT_WORKFLOW_OUTPUT_USD_PER_MTOK")
    ):
        source = "environment override"
    return {"input_usd_per_mtok": input_rate, "output_usd_per_mtok": output_rate, "source": source}


def _first_float(*values: Any) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _cost_usd(input_tokens: int | None, output_tokens: int | None, pricing: dict[str, Any]) -> float | None:
    if input_tokens is None or output_tokens is None:
        return None
    return (
        (input_tokens / 1_000_000.0) * float(pricing["input_usd_per_mtok"])
        + (output_tokens / 1_000_000.0) * float(pricing["output_usd_per_mtok"])
    )


def _display_money(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _display_tokens(value: int | None) -> str:
    if value is None:
        return "unavailable"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _projected_tier(raw_question: str, tier: str) -> str:
    if tier != "auto":
        return tier
    tag = re.match(r"^\[(shallow|standard|deep)\]\s*", raw_question, flags=re.IGNORECASE)
    if tag:
        return tag.group(1).lower()
    return "standard"


def project_usage(spec: dict[str, Any], *, question: str, tier: str, engine: str, budget_usd: str, pricing: dict[str, Any]) -> dict[str, Any]:
    projection_tier = _projected_tier(question, tier)
    cfg = spec["tiers"][projection_tier]
    estimates = spec.get("token_estimates", {})
    runner = str(spec.get("runner") or spec.get("id"))
    if runner == "agent-team":
        calls_by_phase = {
            "scope": 1,
            "collect": int(cfg["collectors"]),
            "shard": 1,
            "execute": int(cfg["workers"]),
            "verify": int(cfg["verifiers"]),
            "integrate": 1,
        }
        input_tokens, output_tokens = _project_tokens(estimates, calls_by_phase)
        engine_overhead = (spec.get("engine_token_overhead") or {}).get(engine, {})
        call_count = sum(calls_by_phase.values())
        input_tokens += int(engine_overhead.get("input_per_call", 0)) * call_count
        maximum_estimates = {
            phase: {
                "input": estimate.get("max_input", estimate.get("input", 0)),
                "output": estimate.get("max_output", estimate.get("output", 0)),
            }
            for phase, estimate in estimates.items()
        }
        max_input_tokens, max_output_tokens = _project_tokens(maximum_estimates, calls_by_phase)
        max_input_tokens += int(engine_overhead.get("max_input_per_call", engine_overhead.get("input_per_call", 0))) * call_count
        return {
            "tier_basis": projection_tier,
            "calls": sum(calls_by_phase.values()),
            "max_calls": sum(calls_by_phase.values()),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "max_total_tokens": max_input_tokens + max_output_tokens,
            "cost_usd": _cost_usd(input_tokens, output_tokens, pricing),
            "max_cost_usd": _cost_usd(max_input_tokens, max_output_tokens, pricing),
            "calls_by_phase": calls_by_phase,
            "max_calls_by_phase": dict(calls_by_phase),
            "budget_usd": _first_float(budget_usd),
            "budget_scope": "per_call_engine_limit",
        }
    claims = int(cfg["claims"])
    angles = int(cfg["angles"])
    fetches = int(cfg["fetch"])
    escalation_rate = float(spec.get("projected_escalation_rate", 0.25))
    total_votes = int(spec.get("escalate_total_votes", 3))
    expected_escalations = int(math.ceil(claims * escalation_rate))
    expected_verify_calls = claims + expected_escalations * max(0, total_votes - 1)
    max_verify_calls = claims * total_votes
    calls_by_phase = {
        "scope": 1,
        "search": angles,
        "fetch": fetches,
        "dedup": 1,
        "verify": expected_verify_calls,
        "synthesize": 1,
        "critic": 1,
    }
    max_calls_by_phase = {**calls_by_phase, "verify": max_verify_calls}
    input_tokens, output_tokens = _project_tokens(estimates, calls_by_phase)
    max_input_tokens, max_output_tokens = _project_tokens(estimates, max_calls_by_phase)
    return {
        "tier_basis": projection_tier,
        "calls": sum(calls_by_phase.values()),
        "max_calls": sum(max_calls_by_phase.values()),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "max_input_tokens": max_input_tokens,
        "max_output_tokens": max_output_tokens,
        "max_total_tokens": max_input_tokens + max_output_tokens,
        "cost_usd": _cost_usd(input_tokens, output_tokens, pricing),
        "max_cost_usd": _cost_usd(max_input_tokens, max_output_tokens, pricing),
        "calls_by_phase": calls_by_phase,
        "max_calls_by_phase": max_calls_by_phase,
        "budget_usd": _first_float(budget_usd),
    }


def _project_tokens(estimates: dict[str, Any], calls_by_phase: dict[str, int]) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for phase, calls in calls_by_phase.items():
        estimate = estimates.get(phase, {})
        input_tokens += int(estimate.get("input", 0)) * calls
        output_tokens += int(estimate.get("output", 0)) * calls
    return input_tokens, output_tokens


def summarize_actual_usage(records: list[dict[str, Any]], pricing: dict[str, Any]) -> dict[str, Any]:
    known = [record for record in records if record.get("input_tokens") is not None or record.get("output_tokens") is not None]
    if not known:
        return {
            "calls": len(records),
            "available": False,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "records": records,
        }
    input_tokens = sum(int(record.get("input_tokens") or 0) for record in known)
    output_tokens = sum(int(record.get("output_tokens") or 0) for record in known)
    provider_costs = [record.get("cost_usd") for record in known]
    provider_cost_available = bool(known) and all(value is not None for value in provider_costs)
    return {
        "calls": len(records),
        "metered_calls": len(known),
        "available": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(sum(float(value) for value in provider_costs), 6) if provider_cost_available else _cost_usd(input_tokens, output_tokens, pricing),
        "cost_source": "provider" if provider_cost_available else "pricing_estimate",
        "records": records,
    }


def pending_actual_usage() -> dict[str, Any]:
    return {
        "calls": 0,
        "available": False,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "records": [],
    }


def build_usage(
    spec: dict[str, Any],
    *,
    question: str,
    tier: str,
    engine: str,
    budget_usd: str,
    pricing: dict[str, Any],
    actual: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "pricing": pricing,
        "projected": project_usage(spec, question=question, tier=tier, engine=engine, budget_usd=budget_usd, pricing=pricing),
        "actual": actual or pending_actual_usage(),
    }


def format_usage_block(usage: dict[str, Any]) -> str:
    projected = usage.get("projected", {})
    actual = usage.get("actual", {})
    pricing = usage.get("pricing", {})
    lines = [
        "## Usage",
        "",
        "Projected usage:",
        f"- Calls: ~{projected.get('calls', 0)} (max {projected.get('max_calls', 0)})",
        "- Tokens: "
        f"~{_display_tokens(projected.get('input_tokens'))} input / "
        f"~{_display_tokens(projected.get('output_tokens'))} output "
        f"(max {_display_tokens(projected.get('max_input_tokens'))} / {_display_tokens(projected.get('max_output_tokens'))})",
        f"- Cost: ~{_display_money(projected.get('cost_usd'))} (max {_display_money(projected.get('max_cost_usd'))})",
        (
            f"- Per-call engine budget limit: {_display_money(projected.get('budget_usd'))} (total run cost can exceed this)"
            if projected.get("budget_scope") == "per_call_engine_limit"
            else f"- Budget cap: {_display_money(projected.get('budget_usd'))}"
        ),
        "",
        "Actual usage:",
    ]
    if actual.get("available"):
        delta = None
        if actual.get("cost_usd") is not None and projected.get("cost_usd") is not None:
            delta = float(actual["cost_usd"]) - float(projected["cost_usd"])
        lines.extend(
            [
                f"- Calls: {actual.get('calls', 0)}",
                f"- Tokens: {_display_tokens(actual.get('input_tokens'))} input/cache / {_display_tokens(actual.get('output_tokens'))} output",
                f"- Cost: {_display_money(actual.get('cost_usd'))}",
                f"- Cost source: {actual.get('cost_source', 'pricing_estimate')}",
                f"- Delta vs projected: {_display_money(delta)}" if delta is not None else "- Delta vs projected: unavailable",
            ]
        )
    else:
        call_text = "pending" if actual.get("calls", 0) == 0 else f"{actual.get('calls')} calls, token metadata unavailable"
        lines.extend(["- Calls: " + call_text, "- Tokens: pending/unavailable", "- Cost: pending/unavailable"])
    lines.extend(
        [
            "",
            "Pricing:",
            f"- Input: ${float(pricing.get('input_usd_per_mtok', 0.0)):.4f}/M tokens",
            f"- Output: ${float(pricing.get('output_usd_per_mtok', 0.0)):.4f}/M tokens",
            f"- Source: {pricing.get('source', 'unknown')}",
        ]
    )
    return "\n".join(lines)


def render_template(template: str, values: dict[str, Any]) -> str:
    safe_values = {key: _stringify_template_value(value) for key, value in values.items()}
    return Template(template).safe_substitute(safe_values)


def _stringify_template_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(_stringify_template_value(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an engine-compatible copy of a workflow JSON schema.

    Newer structured-output engines require every object schema to explicitly
    close unknown keys. Keep the workflow spec itself unchanged so local
    validation remains intentionally lightweight.
    """

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        normalized = {key: visit(item) for key, item in value.items()}
        if normalized.get("type") == "object" or "properties" in normalized:
            properties = normalized.get("properties")
            if isinstance(properties, dict):
                original_required = set(value.get("required") or [])
                for property_name, property_schema in list(properties.items()):
                    if property_name not in original_required:
                        properties[property_name] = {"anyOf": [property_schema, {"type": "null"}]}
                normalized["required"] = list(properties.keys())
            normalized.setdefault("additionalProperties", False)
        return normalized

    return visit(schema)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise WorkflowError("empty model response")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return _unwrap_json_response(parsed)
    if isinstance(parsed, list):
        raise WorkflowError("model returned a JSON array; expected object")

    start = text.find("{")
    while start >= 0:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            ch = text[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return _unwrap_json_response(json.loads(candidate))
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise WorkflowError("could not parse JSON object from model response")


def _unwrap_json_response(parsed: dict[str, Any]) -> dict[str, Any]:
    wrapper_usage = dict(parsed["usage"]) if isinstance(parsed.get("usage"), dict) else None
    if wrapper_usage is not None and parsed.get("total_cost_usd") is not None:
        wrapper_usage["cost_usd"] = parsed["total_cost_usd"]
    for key in ("structured_output", "result", "response", "message", "content"):
        value = parsed.get(key)
        if isinstance(value, str):
            try:
                nested = json.loads(value)
                if isinstance(nested, dict):
                    if wrapper_usage:
                        nested["_usage"] = wrapper_usage
                    return nested
            except json.JSONDecodeError:
                try:
                    return _extract_json_object(value)
                except WorkflowError:
                    pass
        if isinstance(value, dict):
            nested = dict(value)
            if wrapper_usage:
                nested["_usage"] = wrapper_usage
            return nested
    return parsed


def validate_model_output(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    required = schema.get("required") or []
    missing = [key for key in required if key not in value]
    if missing:
        raise WorkflowError(f"{label} response missing required field(s): {', '.join(missing)}")


class EngineAdapter:
    def call(self, call: ModelCall) -> dict[str, Any]:
        raise NotImplementedError


class ExternalEngineAdapter(EngineAdapter):
    def __init__(
        self,
        *,
        engine: str,
        command: str,
        project_dir: Path,
        run_dir: Path,
        model: str | None = None,
        budget_usd: str = "0.50",
        allowed_tools: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.engine = engine
        self.command = command
        self.project_dir = project_dir
        self.run_dir = run_dir
        self.model = model
        self.budget_usd = budget_usd
        self.allowed_tools = allowed_tools
        self.timeout_seconds = int(_first_float(os.environ.get("AGENT_WORKFLOW_CALL_TIMEOUT_SECONDS"), timeout_seconds, 180))
        self._lock = threading.Lock()
        self._index = 0

    def call(self, call: ModelCall) -> dict[str, Any]:
        call_dir = self._next_call_dir(call)
        schema_path = call_dir / "schema.json"
        prompt_path = call_dir / "prompt.md"
        stdout_path = call_dir / "stdout.txt"
        response_path = call_dir / "response.json"
        write_json(schema_path, strict_json_schema(call.schema))
        prompt_path.write_text(call.prompt, encoding="utf-8")
        cmd = self._command(call.prompt, schema_path, response_path)
        (call_dir / "command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
        run_kwargs: dict[str, Any] = {
            "cwd": str(self.project_dir),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "check": False,
            "timeout": self.timeout_seconds,
        }
        if self.engine == "claude":
            run_kwargs["input"] = call.prompt
        else:
            run_kwargs["stdin"] = subprocess.DEVNULL
        try:
            proc = subprocess.run(cmd, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            stdout_path.write_text(stdout, encoding="utf-8")
            raise WorkflowError(f"{self.engine} call {call.label!r} timed out after {self.timeout_seconds}s") from exc
        stdout_path.write_text(proc.stdout or "", encoding="utf-8")
        if proc.returncode != 0:
            tail = (proc.stdout or "").strip()[-1000:]
            raise WorkflowError(f"{self.engine} call {call.label!r} failed with {proc.returncode}: {tail}")
        raw = response_path.read_text(encoding="utf-8") if response_path.exists() else (proc.stdout or "")
        parsed = _extract_json_object(raw)
        validate_model_output(parsed, call.schema, call.label)
        write_json(response_path, parsed)
        return parsed

    def _next_call_dir(self, call: ModelCall) -> Path:
        with self._lock:
            self._index += 1
            index = self._index
        fragment = safe_fragment(f"{index:03d}_{call.phase}_{call.label}")
        path = self.run_dir / "calls" / fragment
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _command(self, prompt: str, schema_path: Path, response_path: Path) -> list[str]:
        if self.engine == "codex":
            cmd = [
                self.command,
                "--search",
                "exec",
                "-C",
                str(self.project_dir),
                "-s",
                "read-only",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "-o",
                str(response_path),
            ]
            if self.model:
                cmd.extend(["-m", self.model])
            cmd.append(prompt)
            return cmd
        if self.engine == "claude":
            cmd = [
                self.command,
                "-p",
                "--input-format",
                "text",
                "--add-dir",
                str(self.project_dir),
                "--permission-mode",
                "auto",
                "--allowedTools",
                self.allowed_tools or "WebSearch,WebFetch",
                "--json-schema",
                schema_path.read_text(encoding="utf-8"),
                "--output-format",
                "json",
                "--max-budget-usd",
                self.budget_usd,
            ]
            if self.model:
                cmd.extend(["--model", self.model])
            return cmd
        raise WorkflowError(f"unsupported engine {self.engine!r}")


class FakeEngineAdapter(EngineAdapter):
    """Deterministic adapter for tests and dry development."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []

    def call(self, call: ModelCall) -> dict[str, Any]:
        self.calls.append(call)
        label = call.label
        if label == "team-scope":
            return {
                "objective": "fixture team objective",
                "profile": "shallow",
                "teamFit": True,
                "fitReason": "Two independent fixture authorities and packets are available.",
                "mode": "audit",
                "authorities": [
                    {
                        "id": "source-a",
                        "label": "Fixture source A",
                        "sourceKind": "files",
                        "task": "Collect fixture A.",
                        "authoritativeCommands": ["read fixture A"],
                        "outputContract": "Return fixture A facts.",
                    },
                    {
                        "id": "source-b",
                        "label": "Fixture source B",
                        "sourceKind": "git",
                        "task": "Collect fixture B.",
                        "authoritativeCommands": ["git status --short"],
                        "outputContract": "Return fixture B facts.",
                    },
                ],
                "suggestedShards": ["Analyze A", "Analyze B"],
                "successCriteria": ["Both sources are covered."],
                "singleAgentEstimate": {"wallTimeSeconds": 120, "totalTokens": 12000, "basis": "Fixture estimate."},
            }
        if label.startswith("collector:"):
            authority_id = label.split(":", 1)[1]
            return {
                "authorityId": authority_id,
                "sourceStatus": "verified",
                "authoritative": True,
                "capturedAt": "2026-01-01T00:00:00Z",
                "summary": f"Collected {authority_id}.",
                "facts": [
                    {
                        "statement": f"{authority_id} fixture fact.",
                        "evidence": "Fixture evidence.",
                        "locator": authority_id,
                        "confidence": "high",
                    }
                ],
                "commands": [{"command": "fixture read", "outcome": "success"}],
                "artifacts": [],
                "limitations": [],
            }
        if label == "team-shard":
            return {
                "workPackets": [
                    {
                        "id": "packet-a",
                        "title": "Analyze A",
                        "objective": "Analyze source A.",
                        "authorityIds": ["source-a"],
                        "scopeBoundary": "Source A only.",
                        "deliverable": "A finding.",
                        "verification": "Check source A snapshot.",
                        "dependsOn": [],
                    },
                    {
                        "id": "packet-b",
                        "title": "Analyze B",
                        "objective": "Analyze source B.",
                        "authorityIds": ["source-b"],
                        "scopeBoundary": "Source B only.",
                        "deliverable": "B finding.",
                        "verification": "Check source B snapshot.",
                        "dependsOn": [],
                    },
                ],
                "integrationRisks": ["Fixture mismatch risk."],
            }
        if label.startswith("worker:"):
            packet_id = label.split(":", 1)[1]
            return {
                "packetId": packet_id,
                "status": "complete",
                "summary": f"Completed {packet_id}.",
                "findings": [
                    {
                        "statement": f"{packet_id} fixture finding.",
                        "evidence": "Fixture snapshot.",
                        "sourceRefs": [packet_id.replace("packet", "source")],
                        "confidence": "high",
                    }
                ],
                "checks": ["Fixture check passed."],
                "artifacts": [],
                "uncertainties": [],
                "efficiencyNote": "Packet stayed within scope.",
            }
        if label.startswith("verifier:"):
            return {
                "verdict": "pass",
                "summary": "Fixture packets agree with the snapshots.",
                "validatedPacketIds": ["packet-a", "packet-b"],
                "contradictions": [],
                "gaps": [],
                "rerunCommands": [],
                "confidence": "high",
            }
        if label == "team-integrate":
            return {
                "summary": "Fixture team summary.",
                "findings": [
                    {
                        "claim": "Both fixture packets completed.",
                        "confidence": "high",
                        "sources": ["source-a", "source-b"],
                        "evidence": "The verifier passed both packets.",
                    }
                ],
                "caveats": "Fixture caveat.",
                "open_questions": [],
                "recommended_actions": ["Use the integrated fixture result."],
                "quality": {
                    "coverage": "complete",
                    "accuracy": "high",
                    "consistency": "high",
                    "verificationSummary": "All fixture packets verified.",
                },
            }
        if label == "scope":
            return {
                "question": "fixture question",
                "summary": "Fixture decomposition.",
                "depth": "shallow",
                "angles": [
                    {"label": "primary", "query": "fixture primary", "rationale": "Find source of record."},
                    {"label": "recent", "query": "fixture recent", "rationale": "Find current context."},
                    {"label": "skeptical", "query": "fixture skeptical", "rationale": "Find counterpoints."},
                ],
            }
        if label.startswith("search:"):
            slug = safe_fragment(label.split(":", 1)[1])
            return {
                "results": [
                    {
                        "url": f"https://example.com/{slug}",
                        "title": f"{slug} source",
                        "snippet": "Fixture search result.",
                        "relevance": "high",
                    }
                ]
            }
        if label.startswith("fetch:"):
            return {
                "sourceQuality": "primary",
                "publishDate": "2026-01-01",
                "claims": [
                    {
                        "claim": "Fixture claim is supported by the source.",
                        "quote": "Fixture quote.",
                        "importance": "central",
                    }
                ],
            }
        if label == "dedup":
            return {
                "claims": [
                    {
                        "claim": "Fixture claim is supported by the source.",
                        "quote": "Fixture quote.",
                        "importance": "central",
                        "sourceQuality": "primary",
                        "sourceUrls": ["https://example.com/primary"],
                    }
                ]
            }
        if label.startswith("v"):
            return {
                "refuted": False,
                "evidence": "Fixture quote supports the claim.",
                "confidence": "high",
                "searched": False,
                "counterSource": "",
            }
        if label == "synthesize":
            return {
                "summary": "Fixture summary.",
                "findings": [
                    {
                        "claim": "Fixture claim is supported by the source.",
                        "confidence": "high",
                        "sources": ["https://example.com/primary"],
                        "evidence": "Fixture quote supports the claim.",
                        "vote": "1-0",
                    }
                ],
                "caveats": "Fixture caveat.",
                "open_questions": ["Fixture open question?"],
            }
        if label == "gap-check":
            return {"coverage": "complete", "underCovered": [], "suggestedFollowUps": []}
        raise WorkflowError(f"fake adapter has no response for {label!r}")


class OptimizingEngineAdapter(EngineAdapter):
    """Optional cache and usage recorder around a workflow engine adapter."""

    def __init__(
        self,
        inner: EngineAdapter,
        *,
        run_id: str,
        engine: str,
        project_dir: Path,
        cache_mode: str = "off",
        semantic_threshold: float = 0.9,
    ) -> None:
        self.inner = inner
        self.run_id = run_id
        self.engine = engine
        self.project_dir = project_dir
        self.cache_mode = cache_mode
        self.semantic_threshold = semantic_threshold
        self.records: list[dict[str, Any]] = []

    def call(self, call: ModelCall) -> dict[str, Any]:
        key = cache_key(
            cache_class="workflow",
            model=self.engine,
            provider=self.engine,
            prefix=call.phase,
            task=call.prompt,
            project_dir=self.project_dir,
            tool="workflow-model-call",
            tool_args={"label": call.label, "schema": call.schema},
        )
        if self.cache_mode in {"exact", "semantic"}:
            hit = cache_lookup(
                key,
                semantic_query=call.prompt,
                semantic_enabled=self.cache_mode == "semantic",
                semantic_threshold=self.semantic_threshold,
            )
            if hit["status"] in {"hit", "semantic_hit"}:
                entry = hit["entry"]
                self.records.append(self._record(call, cache_status=hit["status"], cached=True, usage={}))
                return dict(entry.get("value") or {})

        response = self.inner.call(call)
        usage = _response_usage(response)
        self.records.append(self._record(call, cache_status="miss", cached=False, usage=usage))
        if self.cache_mode in {"exact", "semantic"}:
            cache_store(
                key,
                response,
                cache_class="workflow",
                semantic_text=call.prompt,
                metadata={"run_id": self.run_id, "engine": self.engine, "phase": call.phase, "label": call.label},
            )
        return response

    def _record(self, call: ModelCall, *, cache_status: str, cached: bool, usage: dict[str, Any]) -> dict[str, Any]:
        estimated_input = estimate_tokens(call.prompt)
        estimated_output = 0 if cached else None
        return {
            "phase": call.phase,
            "label": call.label,
            "cache_status": cache_status,
            "cached": cached,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cached_tokens": usage.get("cached_tokens") if usage else (estimated_input if cached else None),
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "model": self.engine,
            "provider": self.engine,
            "route": "cache" if cached else "standard",
            "cost_usd": usage.get("cost_usd"),
        }


def _response_usage(response: dict[str, Any]) -> dict[str, Any]:
    for key in ("_usage", "usage"):
        value = response.get(key)
        if isinstance(value, dict):
            parsed = parse_usage_metadata({"usage": value}) or dict(value)
            cache_creation = int(value.get("cache_creation_input_tokens") or 0)
            cache_read = int(value.get("cache_read_input_tokens") or 0)
            if cache_creation or cache_read:
                parsed["input_tokens"] = int(value.get("input_tokens") or 0) + cache_creation + cache_read
                parsed["cache_creation_tokens"] = cache_creation
                parsed["cached_tokens"] = cache_read
            if value.get("cost_usd") is not None:
                parsed["cost_usd"] = float(value["cost_usd"])
            return parsed
    return {}


def create_engine_adapter(
    *,
    engine: str,
    agents: dict[str, dict[str, Any]],
    project_dir: Path,
    run_dir: Path,
    model: str | None = None,
    budget_usd: str = "0.50",
    allowed_tools: str | None = None,
    timeout_seconds: int | None = None,
) -> EngineAdapter:
    if engine not in agents:
        raise WorkflowError(f"engine {engine!r} is not configured in agents.json")
    agent = agents[engine]
    command = _resolve_agent_command(agent)
    return ExternalEngineAdapter(
        engine=engine,
        command=command,
        project_dir=project_dir,
        run_dir=run_dir,
        model=model,
        budget_usd=budget_usd,
        allowed_tools=allowed_tools,
        timeout_seconds=timeout_seconds,
    )


def _resolve_agent_command(agent: dict[str, Any]) -> str:
    env_name = agent.get("env_command")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    command = agent.get("command")
    if not command:
        raise WorkflowError(f"agent {agent.get('id', '<unknown>')} has no command")
    return shutil.which(str(command)) or str(command)


def fetch_source_excerpt(url: str, run_dir: Path, index: int, *, limit: int = 12000) -> dict[str, Any]:
    sources_dir = run_dir / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    path = sources_dir / f"{index:03d}_{safe_fragment(url)}.txt"
    try:
        req = Request(url, headers={"User-Agent": "agent-bridge-workflow/0.1"})
        with urlopen(req, timeout=12) as response:
            raw = response.read(750_000)
            content_type = response.headers.get("content-type", "")
        text = raw.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or "<html" in text[:500].lower():
            parser = _HTMLTextExtractor()
            parser.feed(text)
            text = parser.text()
        excerpt = re.sub(r"\s+", " ", text).strip()[:limit]
        path.write_text(excerpt, encoding="utf-8")
        return {"ok": True, "url": url, "excerpt": excerpt, "path": str(path), "error": ""}
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        path.write_text(message, encoding="utf-8")
        return {"ok": False, "url": url, "excerpt": "", "path": str(path), "error": message}


def parallel_map(items: Iterable[Any], fn: Callable[[Any], Any], concurrency: int) -> list[Any]:
    values = list(items)
    if not values:
        return []
    max_workers = max(1, min(concurrency, len(values)))
    results: list[Any] = [None] * len(values)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(fn, item): index for index, item in enumerate(values)}
        for future in as_completed(future_map):
            results[future_map[future]] = future.result()
    return results


def plan_workflow_run(
    *,
    workflow_id: str,
    question: str,
    tier: str,
    engine: str,
    source: str | None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = load_workflow(workflow_id)
    run_meta = ensure_run_meta(meta)
    resolved_engine = resolve_engine(engine, source)
    return {
        "workflow_id": spec["id"],
        "name": spec["name"],
        "run_id": run_meta["run_id"],
        "engine": resolved_engine,
        "question": question,
        "tier": tier,
        "phases": [phase["title"] for phase in spec["phases"]],
        "artifact_dir": str(workflow_run_dir(str(run_meta["run_id"]))),
        "dry_run": True,
    }


def run_workflow(
    *,
    workflow_id: str,
    question: str,
    tier: str = "auto",
    engine: str = "auto",
    source: str | None = None,
    agents: dict[str, dict[str, Any]] | None = None,
    project_dir: Path | None = None,
    concurrency: int = 4,
    fmt: str = "both",
    model: str | None = None,
    budget_usd: str = "0.50",
    cache_mode: str = "off",
    semantic_cache_threshold: float = 0.9,
    compression_mode: str = "off",
    compression_max_chars: int = 12000,
    compression_command: str | None = None,
    meta: dict[str, Any] | None = None,
    adapter: EngineAdapter | None = None,
) -> dict[str, Any]:
    spec = load_workflow(workflow_id)
    runner = str(spec.get("runner") or spec["id"])
    if runner not in SUPPORTED_WORKFLOW_RUNNERS:
        raise WorkflowError(f"workflow {workflow_id!r} is registered but has no runner yet")
    if tier not in {"auto", "shallow", "standard", "deep"}:
        raise WorkflowError("--tier must be auto, shallow, standard, or deep")
    if fmt not in {"both", "text", "json"}:
        raise WorkflowError("--format must be both, text, or json")
    question = question.strip()
    if not question:
        raise WorkflowError("a workflow task is required")

    project_dir = (project_dir or Path.cwd()).resolve()
    run_meta = ensure_run_meta(meta)
    run_id = str(run_meta["run_id"])
    resolved_engine = resolve_engine(engine, source)
    run_dir = workflow_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    pricing = resolve_pricing(resolved_engine)
    usage = build_usage(spec, question=question, tier=tier, engine=resolved_engine, budget_usd=budget_usd, pricing=pricing)
    manifest = {
        "workflow_id": spec["id"],
        "runner": runner,
        "name": spec["name"],
        "run_id": run_id,
        "engine": resolved_engine,
        "question": question,
        "tier_requested": tier,
        "project_dir": str(project_dir),
        "correlation": format_meta(run_meta),
        "events": str(events_path()),
        "artifact_dir": str(run_dir),
        "optimization": {
            "cache_mode": cache_mode,
            "semantic_cache_threshold": semantic_cache_threshold,
            "compression_mode": compression_mode,
            "compression_max_chars": compression_max_chars,
        },
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "usage.json", usage)
    write_usage(
        run_id,
        build_scorecard(
            run_id=run_id,
            command="workflow",
            projected=usage["projected"],
            records=[],
            budget_usd=float(_first_float(budget_usd)),
        ),
    )
    emit_event("workflow.created", run_id=run_id, meta=run_meta, data=manifest)

    if adapter is None:
        if agents is None:
            raise WorkflowError("agents config is required when no test adapter is provided")
        adapter = create_engine_adapter(
            engine=resolved_engine,
            agents=agents,
            project_dir=project_dir,
            run_dir=run_dir,
            model=model,
            budget_usd=budget_usd,
            allowed_tools="Read,Grep,Glob,WebSearch,WebFetch" if runner == "agent-team" and resolved_engine == "claude" else None,
            timeout_seconds=300 if runner == "agent-team" else None,
        )
    optimizing_adapter = OptimizingEngineAdapter(
        adapter,
        run_id=run_id,
        engine=resolved_engine,
        project_dir=project_dir,
        cache_mode=cache_mode,
        semantic_threshold=semantic_cache_threshold,
    )

    try:
        if runner == "agent-team":
            result = run_agent_team(
                spec=spec,
                raw_task=question,
                tier_arg=tier,
                adapter=optimizing_adapter,
                run_dir=run_dir,
                project_dir=project_dir,
                concurrency=concurrency,
                run_meta=run_meta,
                call_model=_call,
                parallel_map=parallel_map,
                emit_phase=_phase,
            )
        else:
            result = _run_deep_research_lite(
                spec,
                question,
                tier,
                optimizing_adapter,
                run_dir,
                concurrency,
                run_meta,
                compression_mode=compression_mode,
                compression_max_chars=compression_max_chars,
                compression_command=compression_command,
            )
    except Exception as exc:
        emit_event("workflow.failed", run_id=run_id, meta=run_meta, data={"error": str(exc)})
        raise

    result.update(
        {
            "workflow_id": spec["id"],
            "workflow_name": spec["name"],
            "run_id": run_id,
            "engine": resolved_engine,
            "artifact_dir": str(run_dir),
        }
    )
    compression_usage_records = [
        {
            "phase": "Fetch",
            "label": "context-compression",
            "cache_status": "n/a",
            "route": "compress",
            "compression_saved_tokens": row.get("saved_tokens", 0),
            "input_tokens": None,
            "output_tokens": None,
            "provider": resolved_engine,
            "model": resolved_engine,
        }
        for row in result.get("optimization", {}).get("compression", [])
    ]
    usage_records = [*optimizing_adapter.records, *compression_usage_records]
    actual_usage = summarize_actual_usage(usage_records, pricing)
    usage = build_usage(spec, question=question, tier=result.get("tier", tier), engine=resolved_engine, budget_usd=budget_usd, pricing=pricing, actual=actual_usage)
    result["usage"] = usage
    if result.get("team"):
        team = result["team"]
        projected_tokens = int(usage.get("projected", {}).get("total_tokens") or 0)
        actual_tokens = usage.get("actual", {}).get("total_tokens")
        baseline_tokens = int((team.get("singleAgentEstimate") or {}).get("totalTokens") or 0)
        team["projectedTokens"] = projected_tokens
        team["actualTokens"] = actual_tokens
        team["projectedTokenMultiple"] = round(projected_tokens / baseline_tokens, 2) if baseline_tokens else None
        team["baselineBasis"] = str((team.get("singleAgentEstimate") or {}).get("basis") or "Agent-generated estimate; no direct single-agent control run was measured.")
    report = format_report(result)
    report_path = run_dir / "report.md"
    result_path = run_dir / "result.json"
    report_path.write_text(report, encoding="utf-8")
    write_json(result_path, result)
    write_json(run_dir / "usage.json", usage)
    write_usage(
        run_id,
        build_scorecard(
            run_id=run_id,
            command="workflow",
            projected=usage["projected"],
            records=usage_records,
            budget_usd=float(_first_float(budget_usd)),
            warnings=[] if actual_usage.get("available") else ["provider token metadata unavailable; estimates only"],
        ),
    )
    manifest["tier"] = result.get("tier")
    manifest["artifacts"] = {
        "manifest": str(run_dir / "manifest.json"),
        "report": str(report_path),
        "result": str(result_path),
        "usage": str(run_dir / "usage.json"),
        "events": str(events_path()),
        **(result.get("artifacts") or {}),
    }
    write_json(run_dir / "manifest.json", manifest)
    emit_event("workflow.completed", run_id=run_id, meta=run_meta, data={"result": str(result_path), "report": str(report_path)})
    return result


def _phase(name: str, run_meta: dict[str, Any]) -> None:
    emit_event("workflow.phase", run_id=str(run_meta["run_id"]), meta=run_meta, data={"phase": name})


def _call(adapter: EngineAdapter, spec: dict[str, Any], prompt_name: str, schema_name: str, label: str, phase: str, values: dict[str, Any]) -> dict[str, Any]:
    prompt = render_template(spec["prompts"][prompt_name], values)
    schema = spec["schemas"][schema_name]
    response = adapter.call(ModelCall(prompt=prompt, schema=schema, label=label, phase=phase))
    validate_model_output(response, schema, label)
    return response


def _run_deep_research_lite(
    spec: dict[str, Any],
    raw_question: str,
    tier_arg: str,
    adapter: EngineAdapter,
    run_dir: Path,
    concurrency: int,
    run_meta: dict[str, Any],
    *,
    compression_mode: str = "off",
    compression_max_chars: int = 12000,
    compression_command: str | None = None,
) -> dict[str, Any]:
    tier_tag = re.match(r"^\[(shallow|standard|deep)\]\s*", raw_question, flags=re.IGNORECASE)
    forced_tier = None
    question = raw_question
    if tier_tag:
        forced_tier = tier_tag.group(1).lower()
        question = raw_question[tier_tag.end() :].strip()
    if tier_arg != "auto":
        forced_tier = tier_arg

    _phase("Scope", run_meta)
    scope = _call(adapter, spec, "scope", "scope", "scope", "Scope", {"question": question})
    resolved_tier = forced_tier or (scope.get("depth") if scope.get("depth") in spec["tiers"] else "standard")
    cfg = spec["tiers"][resolved_tier]
    angles = (scope.get("angles") or [])[: int(cfg["angles"])]
    if not angles:
        raise WorkflowError("scope phase returned no search angles")

    _phase("Search", run_meta)
    search_results = parallel_map(
        angles,
        lambda angle: {
            "angle": angle.get("label", "angle"),
            "results": _call(
                adapter,
                spec,
                "search",
                "search",
                "search:" + str(angle.get("label", "angle")),
                "Search",
                {
                    "question": question,
                    "label": angle.get("label", "angle"),
                    "rationale": angle.get("rationale", ""),
                    "query": angle.get("query", question),
                },
            ).get("results", []),
        },
        concurrency,
    )

    fetch_jobs = _select_fetch_jobs(search_results, int(spec.get("sources_per_angle", 3)), int(cfg["fetch"]))

    _phase("Fetch", run_meta)
    compression_records: list[dict[str, Any]] = []

    def do_fetch(job: dict[str, Any]) -> dict[str, Any]:
        source = job["source"]
        fetched = fetch_source_excerpt(str(source.get("url", "")), run_dir, int(job["index"]))
        original_context = (
            "Runner-fetched source excerpt:\n" + fetched["excerpt"]
            if fetched["ok"] and fetched["excerpt"]
            else "Runner fetch failed: " + fetched["error"] + "\nUse native web tools if available."
        )
        source_context = original_context
        compression_record: dict[str, Any] | None = None
        if compression_mode != "off":
            compression_record = compress_context(
                original_context,
                mode=compression_mode,
                max_chars=compression_max_chars,
                external_command=compression_command,
            )
            source_context = compression_record["compressed"]
            compression_path = run_dir / "compressed" / f"{int(job['index']):03d}_{safe_fragment(str(source.get('url', 'source')))}.txt"
            compression_path.parent.mkdir(parents=True, exist_ok=True)
            compression_path.write_text(source_context, encoding="utf-8")
            compression_records.append(
                {
                    "url": source.get("url", ""),
                    "path": str(compression_path),
                    "mode": compression_record["mode"],
                    "original_tokens": compression_record["original_tokens"],
                    "compressed_tokens": compression_record["compressed_tokens"],
                    "saved_tokens": max(0, int(compression_record["original_tokens"]) - int(compression_record["compressed_tokens"])),
                    "ratio": compression_record["ratio"],
                    "quality_risk": compression_record["quality_risk"],
                    "warning": compression_record["warning"],
                    "fallback": compression_record["fallback"],
                    "original_path": fetched.get("path", ""),
                }
            )
        host = "unknown"
        try:
            from urllib.parse import urlparse

            host = urlparse(str(source.get("url", ""))).netloc.replace("www.", "") or "unknown"
        except Exception:
            pass
        response = _call(
            adapter,
            spec,
            "fetch",
            "extract",
            "fetch:" + host,
            "Fetch",
            {
                "question": question,
                "url": source.get("url", ""),
                "title": source.get("title", ""),
                "angle": job["angle"],
                "source_context": source_context,
            },
        )
        claims = response.get("claims") or []
        return {
            "url": source.get("url", ""),
            "title": source.get("title", ""),
            "angle": job["angle"],
            "sourceQuality": response.get("sourceQuality", "unreliable"),
            "publishDate": response.get("publishDate", ""),
            "claims": [
                {**claim, "sourceUrl": source.get("url", ""), "sourceQuality": response.get("sourceQuality", "unreliable")}
                for claim in claims
            ],
        }

    all_sources = [source for source in parallel_map(fetch_jobs["jobs"], do_fetch, concurrency) if source]
    raw_claims = [claim for source in all_sources for claim in source.get("claims", [])]
    if not raw_claims:
        return _empty_result(question, resolved_tier, all_sources, fetch_jobs, compression_records=compression_records, cache_mode=getattr(adapter, "cache_mode", "off"))

    _phase("Dedup", run_meta)
    claims_block = "\n".join(
        f"[{index}] ({claim.get('importance')}, {claim.get('sourceQuality')}) {claim.get('claim')}\n"
        f"    quote: \"{claim.get('quote', '')}\"\n"
        f"    source: {claim.get('sourceUrl')}"
        for index, claim in enumerate(raw_claims)
    )
    dedup = _call(adapter, spec, "dedup", "dedup", "dedup", "Dedup", {"question": question, "claims_block": claims_block})
    unique_claims = dedup.get("claims") or [
        {
            "claim": claim.get("claim", ""),
            "quote": claim.get("quote", ""),
            "importance": claim.get("importance", "supporting"),
            "sourceQuality": claim.get("sourceQuality", "unreliable"),
            "sourceUrls": [claim.get("sourceUrl", "")],
        }
        for claim in raw_claims
    ]
    ranked_claims = _rank_claims(unique_claims)[: int(cfg["claims"])]

    _phase("Verify", run_meta)
    round1 = parallel_map(
        [{**claim, "_id": index} for index, claim in enumerate(ranked_claims)],
        lambda claim: {
            "id": claim["_id"],
            "claim": claim,
            "votes": [
                _call(
                    adapter,
                    spec,
                    "verify",
                    "verdict",
                    "v1:" + str(claim.get("claim", ""))[:32],
                    "Verify",
                    _verify_values(question, claim, 1, 1),
                )
            ],
        },
        concurrency,
    )
    escalations = [
        row
        for row in round1
        if row["claim"].get("importance") == "central"
        and (not row["votes"] or row["votes"][0].get("refuted") or row["votes"][0].get("confidence") == "low")
    ]
    if escalations:
        total_votes = int(spec.get("escalate_total_votes", 3))

        def add_votes(row: dict[str, Any]) -> dict[str, Any]:
            extras = parallel_map(
                list(range(2, total_votes + 1)),
                lambda vote_number: _call(
                    adapter,
                    spec,
                    "verify",
                    "verdict",
                    f"v{vote_number}:" + str(row["claim"].get("claim", ""))[:28],
                    "Verify",
                    _verify_values(question, row["claim"], vote_number, total_votes),
                ),
                concurrency,
            )
            row["votes"].extend(extras)
            return row

        round1 = [add_votes(row) if row in escalations else row for row in round1]

    voted = []
    for row in round1:
        decision = _decide(row["votes"], int(spec.get("refutes_to_kill", 2)))
        voted.append({**row["claim"], "votes": row["votes"], "refutes": decision["refutes"], "validVotes": decision["valid"], "survives": decision["survives"]})
    confirmed = [claim for claim in voted if claim["survives"]]
    killed = [claim for claim in voted if not claim["survives"]]
    if not confirmed:
        return {
            "question": question,
            "tier": resolved_tier,
            "summary": f"All {len(voted)} verified claims were refuted or unverified.",
            "findings": [],
            "caveats": "No claims survived verification.",
            "open_questions": [],
            "refuted": _refuted(killed),
            "sources": _source_summary(all_sources),
            "stats": _stats(resolved_tier, angles, all_sources, raw_claims, unique_claims, ranked_claims, round1, confirmed, killed, fetch_jobs),
            "optimization": {"compression": compression_records, "cache_mode": getattr(adapter, "cache_mode", "off")},
            "completeness": {},
        }

    _phase("Synthesize", run_meta)
    confirmed_block = _confirmed_block(confirmed)
    killed_block = "\n".join(
        f"- \"{claim.get('claim')}\" ({', '.join(claim.get('sourceUrls', []))}, vote {_vote(claim)})" for claim in killed
    )
    report = _call(
        adapter,
        spec,
        "synthesize",
        "report",
        "synthesize",
        "Synthesize",
        {
            "question": question,
            "confirmed_count": len(confirmed),
            "confirmed_block": confirmed_block,
            "killed_block": killed_block,
        },
    )

    _phase("Gap check", run_meta)
    answered_angles = sorted({source.get("angle", "") for source in all_sources if any(source.get("url") in c.get("sourceUrls", []) for c in confirmed)})
    critic = _call(
        adapter,
        spec,
        "critic",
        "critic",
        "gap-check",
        "Gap check",
        {
            "question": question,
            "angles": ", ".join(str(angle.get("label", "")) for angle in angles),
            "answered_angles": ", ".join(answered_angles) or "none",
            "summary": report.get("summary", ""),
            "findings": " | ".join(finding.get("claim", "") for finding in report.get("findings", [])),
        },
    )

    return {
        "question": question,
        "tier": resolved_tier,
        "summary": report.get("summary", ""),
        "findings": report.get("findings", []),
        "caveats": report.get("caveats", ""),
        "open_questions": report.get("open_questions", []),
        "refuted": _refuted(killed),
        "sources": _source_summary(all_sources),
        "stats": _stats(resolved_tier, angles, all_sources, raw_claims, unique_claims, ranked_claims, round1, confirmed, killed, fetch_jobs),
        "optimization": {"compression": compression_records, "cache_mode": getattr(adapter, "cache_mode", "off")},
        "completeness": {
            "coverage": critic.get("coverage"),
            "underCovered": critic.get("underCovered", []),
            "suggestedFollowUps": critic.get("suggestedFollowUps", []),
        },
    }


def _select_fetch_jobs(search_results: list[dict[str, Any]], sources_per_angle: int, fetch_budget: int) -> dict[str, Any]:
    rank = {"high": 0, "medium": 1, "low": 2}
    seen: dict[str, dict[str, Any]] = {}
    dupes: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    remaining = fetch_budget
    index = 0
    for result in search_results:
        taken = 0
        for source in sorted(result.get("results", []), key=lambda item: rank.get(item.get("relevance", "low"), 9)):
            if taken >= sources_per_angle:
                continue
            key = _norm_url(str(source.get("url", "")))
            if key in seen:
                dupes.append({**source, "angle": result["angle"], "dupOf": seen[key]})
                continue
            if remaining <= 0:
                dropped.append({**source, "angle": result["angle"]})
                continue
            seen[key] = {"angle": result["angle"], "title": source.get("title", "")}
            remaining -= 1
            taken += 1
            index += 1
            jobs.append({"index": index, "angle": result["angle"], "source": source})
    return {"jobs": jobs, "dupes": dupes, "budgetDropped": dropped}


def _norm_url(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return (parsed.netloc.replace("www.", "") + parsed.path.rstrip("/")).lower()
    except Exception:
        return url.lower()


def _rank_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    imp = {"central": 0, "supporting": 1, "tangential": 2}
    qual = {"primary": 0, "secondary": 1, "blog": 2, "forum": 3, "unreliable": 4}
    return sorted(
        claims,
        key=lambda claim: (
            imp.get(claim.get("importance", "supporting"), 9),
            -len(claim.get("sourceUrls", [])),
            qual.get(claim.get("sourceQuality", "unreliable"), 9),
        ),
    )


def _verify_values(question: str, claim: dict[str, Any], vote: int, total: int) -> dict[str, Any]:
    return {
        "question": question,
        "vote": vote,
        "total": total,
        "claim": claim.get("claim", ""),
        "importance": claim.get("importance", ""),
        "source_count": len(claim.get("sourceUrls", [])),
        "source_urls": ", ".join(claim.get("sourceUrls", [])),
        "source_quality": claim.get("sourceQuality", ""),
        "quote": claim.get("quote", ""),
    }


def _decide(votes: list[dict[str, Any]], refutes_to_kill: int) -> dict[str, Any]:
    valid = [vote for vote in votes if vote]
    if not valid:
        return {"survives": False, "refutes": 0, "valid": 0}
    refutes = len([vote for vote in valid if vote.get("refuted")])
    if len(valid) == 1:
        return {"survives": not valid[0].get("refuted"), "refutes": refutes, "valid": 1}
    return {"survives": refutes < refutes_to_kill, "refutes": refutes, "valid": len(valid)}


def _vote(claim: dict[str, Any]) -> str:
    return f"{claim.get('validVotes', 0) - claim.get('refutes', 0)}-{claim.get('refutes', 0)}"


def _refuted(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"claim": claim.get("claim", ""), "vote": _vote(claim), "sources": claim.get("sourceUrls", [])} for claim in claims]


def _source_summary(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "url": source.get("url", ""),
            "title": source.get("title", ""),
            "quality": source.get("sourceQuality", ""),
            "angle": source.get("angle", ""),
            "claimCount": len(source.get("claims", [])),
        }
        for source in sources
    ]


def _stats(
    tier: str,
    angles: list[dict[str, Any]],
    all_sources: list[dict[str, Any]],
    raw_claims: list[dict[str, Any]],
    unique_claims: list[dict[str, Any]],
    ranked_claims: list[dict[str, Any]],
    round1: list[dict[str, Any]],
    confirmed: list[dict[str, Any]],
    killed: list[dict[str, Any]],
    fetch_jobs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tier": tier,
        "angles": len(angles),
        "sourcesFetched": len(all_sources),
        "rawClaims": len(raw_claims),
        "uniqueClaims": len(unique_claims),
        "claimsVerified": len(ranked_claims),
        "escalated": len([row for row in round1 if len(row.get("votes", [])) > 1]),
        "confirmed": len(confirmed),
        "killed": len(killed),
        "urlDupes": len(fetch_jobs.get("dupes", [])),
        "budgetDropped": len(fetch_jobs.get("budgetDropped", [])),
    }


def _confirmed_block(confirmed: list[dict[str, Any]]) -> str:
    rows = []
    rank = {"high": 0, "medium": 1, "low": 2}
    for index, claim in enumerate(sorted(confirmed, key=lambda item: -len(item.get("sourceUrls", [])))):
        best_votes = [vote for vote in claim.get("votes", []) if vote and not vote.get("refuted")]
        best_votes.sort(key=lambda vote: rank.get(vote.get("confidence", "low"), 9))
        best = best_votes[0] if best_votes else {}
        rows.append(
            f"### [{index}] {claim.get('claim')}\n"
            f"Vote: {_vote(claim)}; Corroboration: {len(claim.get('sourceUrls', []))} source(s); "
            f"Best quality: {claim.get('sourceQuality')}\n"
            f"Sources: {', '.join(claim.get('sourceUrls', []))}\n"
            f"Quote: \"{claim.get('quote', '')}\"\n"
            f"Verifier evidence ({best.get('confidence', 'unknown')}): {best.get('evidence', '')}\n"
        )
    return "\n".join(rows)


def _empty_result(
    question: str,
    tier: str,
    all_sources: list[dict[str, Any]],
    fetch_jobs: dict[str, Any],
    *,
    compression_records: list[dict[str, Any]] | None = None,
    cache_mode: str = "off",
) -> dict[str, Any]:
    return {
        "question": question,
        "tier": tier,
        "summary": f"No claims extracted. {len(all_sources)} sources fetched, all empty or failed.",
        "findings": [],
        "caveats": "No source produced a verifiable claim.",
        "open_questions": [],
        "refuted": [],
        "sources": _source_summary(all_sources),
        "stats": {
            "tier": tier,
            "angles": 0,
            "sourcesFetched": len(all_sources),
            "rawClaims": 0,
            "uniqueClaims": 0,
            "claimsVerified": 0,
            "confirmed": 0,
            "killed": 0,
            "urlDupes": len(fetch_jobs.get("dupes", [])),
            "budgetDropped": len(fetch_jobs.get("budgetDropped", [])),
        },
        "optimization": {"compression": compression_records or [], "cache_mode": cache_mode},
        "completeness": {},
    }


def format_report(result: dict[str, Any]) -> str:
    title = result.get("workflow_name")
    if not title:
        title = "Deep Research Lite" if result.get("workflow_id") == "deep-research-lite" else result.get("workflow_id") or "Workflow Report"
    lines = [
        f"# {title}",
        "",
        f"Question: {result.get('question', '')}",
        f"Engine: {result.get('engine', '')}",
        f"Tier: {result.get('tier', '')}",
        f"Status: {result.get('status', 'unknown')}",
        f"Run: {result.get('run_id', '')}",
        "",
        "## Summary",
        result.get("summary", ""),
        "",
        "## Findings",
    ]
    findings = result.get("findings") or []
    if findings:
        for index, finding in enumerate(findings, start=1):
            sources = ", ".join(finding.get("sources", []))
            lines.extend(
                [
                    f"{index}. [{finding.get('confidence', 'unknown')}] {finding.get('claim', '')}",
                    f"   Evidence: {finding.get('evidence', '')}",
                    f"   Sources: {sources}",
                ]
            )
            if finding.get("vote"):
                lines.append(f"   Vote: {finding.get('vote')}")
    else:
        lines.append("No confirmed findings.")
    lines.extend(["", "## Caveats", result.get("caveats", "") or "None recorded."])
    open_questions = result.get("open_questions") or []
    if open_questions:
        lines.extend(["", "## Open Questions"])
        lines.extend(f"- {question}" for question in open_questions)
    recommended_actions = result.get("recommended_actions") or []
    if recommended_actions:
        lines.extend(["", "## Recommended Actions"])
        lines.extend(f"- {action}" for action in recommended_actions)
    verification = result.get("verification") or []
    if verification:
        lines.extend(["", "## Independent Verification"])
        for index, verifier in enumerate(verification, start=1):
            lines.append(f"- Verifier {index}: {verifier.get('verdict', 'unknown')} ({verifier.get('confidence', 'unknown')} confidence) — {verifier.get('summary', '')}")
            for contradiction in verifier.get("contradictions") or []:
                lines.append(f"  - Contradiction: {contradiction.get('description', '')} Resolution: {contradiction.get('resolution', '')}")
            for gap in verifier.get("gaps") or []:
                lines.append(f"  - Gap: {gap}")
            for command in verifier.get("rerunCommands") or []:
                lines.append(f"  - Suggested recheck: {command}")
    team = result.get("team") or {}
    if team:
        timings = team.get("timingsSeconds") or {}
        lines.extend(
            [
                "",
                "## Team Performance",
                f"- Protocol: {team.get('protocol', 'Snapshot-Shard-Verify')}",
                f"- Team: {team.get('collectors', 0)} collector(s), {team.get('workers', 0)} worker(s), {team.get('verifiers', 0)} verifier(s)",
                f"- Concurrency: {team.get('concurrency', 0)}",
                f"- Wall time: {timings.get('total', 'unavailable')} seconds",
                f"- Actual tokens: {team.get('actualTokens') if team.get('actualTokens') is not None else 'provider metadata unavailable'}",
                f"- Agent-estimated single-agent baseline: {(team.get('singleAgentEstimate') or {}).get('wallTimeSeconds', 'unavailable')} seconds / {(team.get('singleAgentEstimate') or {}).get('totalTokens', 'unavailable')} tokens",
                f"- Baseline basis: {team.get('baselineBasis', 'Agent-generated estimate; no direct control run was measured.')}",
                f"- Derived speedup vs agent estimate (not measured): {team.get('estimatedSpeedup') if team.get('estimatedSpeedup') is not None else 'unavailable'}x",
                f"- Projected token multiple vs agent estimate: {team.get('projectedTokenMultiple') if team.get('projectedTokenMultiple') is not None else 'unavailable'}x",
            ]
        )
        quality = result.get("quality") or {}
        if quality:
            lines.extend(
                [
                    f"- Coverage: {quality.get('coverage', 'unknown')}",
                    f"- Accuracy: {quality.get('accuracy', 'unknown')}",
                    f"- Consistency: {quality.get('consistency', 'unknown')}",
                    f"- Verification: {quality.get('verificationSummary', '')}",
                ]
            )
    optimization = result.get("optimization") or {}
    compression = optimization.get("compression") or []
    lines.extend(
        [
            "",
            "## Optimization",
            f"- Cache mode: {optimization.get('cache_mode', 'off')}",
            f"- Compressed contexts: {len(compression)}",
        ]
    )
    if compression:
        saved = sum(int(row.get("saved_tokens") or 0) for row in compression)
        lossy = len([row for row in compression if row.get("quality_risk") not in {"", "none"}])
        lines.append(f"- Estimated compression savings: {saved} tokens")
        lines.append(f"- Lossy compression records: {lossy}")
    if result.get("usage"):
        lines.extend(["", format_usage_block(result["usage"])])
    lines.extend(
        [
            "",
            "## Artifacts",
            f"- report.md: {workflow_run_dir(str(result.get('run_id', ''))) / 'report.md'}",
            f"- result.json: {workflow_run_dir(str(result.get('run_id', ''))) / 'result.json'}",
            f"- usage.json: {workflow_run_dir(str(result.get('run_id', ''))) / 'usage.json'}",
            f"- events: {events_path()}",
        ]
    )
    for name, path in (result.get("artifacts") or {}).items():
        lines.append(f"- {name}: {path}")
    return "\n".join(lines).rstrip() + "\n"


def inspect_workflow_run(run_id: str) -> dict[str, Any]:
    run_dir = workflow_run_dir(run_id)
    manifest_path = run_dir / "manifest.json"
    result_path = run_dir / "result.json"
    if not manifest_path.exists():
        raise WorkflowError(f"no workflow run found for {run_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else None
    return {"manifest": manifest, "result": result, "run_dir": str(run_dir)}


def format_inspection(data: dict[str, Any]) -> str:
    result = data.get("result")
    if result:
        return format_report(result)
    manifest = data["manifest"]
    return (
        f"# Workflow Run {manifest.get('run_id')}\n\n"
        f"Workflow: {manifest.get('workflow_id')}\n"
        f"Engine: {manifest.get('engine')}\n"
        f"Question: {manifest.get('question')}\n"
        f"Artifacts: {data.get('run_dir')}\n"
    )
