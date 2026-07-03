"""Token, gateway, cache, compression, and routing helpers.

The helpers in this module are intentionally stdlib-only. They give bridge,
loop, and workflow commands a shared place to make token-saving behavior
observable without requiring a live gateway, vector database, or local
compression model.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .correlation import iso_now, safe_fragment
from .trace import emit_event, state_dir


CACHE_POLICY_VERSION = "2026-07-03"
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60
SAFE_CACHE_CLASSES = {"workflow", "tool-result", "review", "dry-run", "classification"}
_CACHE_LOCK = threading.Lock()


def estimate_tokens(text: str) -> int:
    """Cheap deterministic token estimate used for reports and thresholds."""

    if not text:
        return 0
    return max(1, int(len(text) / 4))


def fingerprint(value: Any, *, length: int = 16) -> str:
    raw = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def repo_fingerprint(project_dir: Path) -> dict[str, str]:
    def git(args: list[str]) -> str:
        try:
            return subprocess.check_output(
                ["git", "-C", str(project_dir), *args],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    head = git(["rev-parse", "HEAD"])
    status = git(["status", "--short"])
    return {
        "head": head,
        "status_fingerprint": fingerprint(status),
        "dirty": "true" if status else "false",
    }


def gateway_profile(agent: dict[str, Any]) -> dict[str, Any] | None:
    raw = agent.get("gateway")
    if raw is None:
        keys = {"base_url", "api_key_env", "provider", "model_alias", "budget_tag", "gateway_headers"}
        raw = {key: agent.get(key) for key in keys if agent.get(key) is not None}
    if not isinstance(raw, dict) or not raw.get("base_url"):
        return None
    return {
        "base_url": str(raw["base_url"]).rstrip("/"),
        "api_key_env": str(raw.get("api_key_env") or ""),
        "provider": str(raw.get("provider") or agent.get("id") or "gateway"),
        "model_alias": str(raw.get("model_alias") or agent.get("model") or agent.get("id") or ""),
        "budget_tag": str(raw.get("budget_tag") or ""),
        "gateway_headers": raw.get("gateway_headers") if isinstance(raw.get("gateway_headers"), dict) else {},
    }


def gateway_status_rows(agents: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for agent_id, agent in sorted(agents.items()):
        profile = gateway_profile(agent)
        if not profile:
            rows.append({"agent": agent_id, "route": "direct", "gateway": None})
            continue
        api_env = profile.get("api_key_env", "")
        rows.append(
            {
                "agent": agent_id,
                "route": "gateway",
                "gateway": profile["base_url"],
                "provider": profile["provider"],
                "model_alias": profile["model_alias"],
                "budget_tag": profile["budget_tag"],
                "api_key_env": api_env,
                "api_key_configured": bool(api_env and os.environ.get(api_env)),
            }
        )
    return rows


def format_gateway_status(rows: Iterable[dict[str, Any]]) -> str:
    lines = ["agent\troute\tgateway\tmodel\tbudget_tag\tapi_key"]
    for row in rows:
        if row.get("route") == "gateway":
            api_key = "set" if row.get("api_key_configured") else ("missing" if row.get("api_key_env") else "not required")
            lines.append(
                "\t".join(
                    [
                        str(row.get("agent", "")),
                        "gateway",
                        str(row.get("gateway", "")),
                        str(row.get("model_alias", "")),
                        str(row.get("budget_tag", "")),
                        api_key,
                    ]
                )
            )
        else:
            lines.append("\t".join([str(row.get("agent", "")), "direct", "", "", "", ""]))
    return "\n".join(lines) + "\n"


def call_openai_gateway(
    *,
    profile: dict[str, Any],
    prompt: str,
    system_prompt: str,
    timeout: int = 60,
) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    api_env = profile.get("api_key_env")
    if api_env and os.environ.get(api_env):
        headers["authorization"] = f"Bearer {os.environ[api_env]}"
    for key, value in profile.get("gateway_headers", {}).items():
        headers[str(key)] = str(value)
    body = {
        "model": profile.get("model_alias") or "default",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "metadata": {
            "agent_bridge_provider": profile.get("provider", ""),
            "agent_bridge_budget_tag": profile.get("budget_tag", ""),
        },
    }
    request = Request(
        profile["base_url"] + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"gateway call failed: {exc}") from exc
    output = ""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            output = str(message.get("content") or "")
    return {"output": output, "raw": payload, "usage": parse_usage_metadata(payload)}


def parse_usage_metadata(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    input_tokens = _first_int(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _first_int(usage.get("output_tokens"), usage.get("completion_tokens"))
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached_tokens = _first_int(
        usage.get("cached_tokens"),
        prompt_details.get("cached_tokens"),
        input_details.get("cached_tokens"),
        usage.get("cache_read_input_tokens"),
    )
    cache_creation_tokens = _first_int(usage.get("cache_creation_input_tokens"), usage.get("cache_creation_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _sum_optional(input_tokens, output_tokens),
        "cached_tokens": cached_tokens,
        "cache_creation_tokens": cache_creation_tokens,
    }


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _sum_optional(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return int(left or 0) + int(right or 0)


def cacheability_report(
    *,
    prefix: str,
    task: str,
    provider: str = "",
    minimum_tokens: int = 1024,
    cache_hint: str = "auto",
) -> dict[str, Any]:
    prefix_tokens = estimate_tokens(prefix)
    task_tokens = estimate_tokens(task)
    return {
        "prefix_fingerprint": fingerprint(prefix),
        "task_fingerprint": fingerprint(task),
        "prefix_tokens_estimate": prefix_tokens,
        "task_tokens_estimate": task_tokens,
        "minimum_tokens": minimum_tokens,
        "provider": provider,
        "cache_hint": cache_hint,
        "likely_provider_cacheable": prefix_tokens >= minimum_tokens,
        "reason": "prefix meets provider cache threshold" if prefix_tokens >= minimum_tokens else "prefix below provider cache threshold",
    }


def exact_cache_path() -> Path:
    return state_dir() / "cache" / "exact.json"


def tool_cache_path() -> Path:
    return state_dir() / "cache" / "tool-results.json"


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "entries": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "1.0", "entries": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), dict):
        return {"schema_version": "1.0", "entries": {}}
    return payload


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def cache_key(
    *,
    cache_class: str,
    model: str = "",
    provider: str = "",
    prefix: str = "",
    task: str = "",
    project_dir: Path | None = None,
    tool: str = "",
    tool_args: Any = None,
    policy_version: str = CACHE_POLICY_VERSION,
) -> str:
    repo = repo_fingerprint(project_dir) if project_dir else {}
    material = {
        "class": cache_class,
        "model": model,
        "provider": provider,
        "prefix": fingerprint(prefix),
        "task": fingerprint(task),
        "repo": repo,
        "tool": tool,
        "tool_args": tool_args,
        "policy_version": policy_version,
    }
    return fingerprint(material, length=32)


def cache_lookup(
    key: str,
    *,
    semantic_query: str | None = None,
    semantic_threshold: float = 0.9,
    semantic_enabled: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    path = path or exact_cache_path()
    payload = _load_cache(path)
    now = dt.datetime.now(dt.timezone.utc)
    entries = payload["entries"]
    entry = entries.get(key)
    if isinstance(entry, dict) and not _expired(entry, now):
        return {"status": "hit", "key": key, "entry": entry}
    if entry:
        return {"status": "expired", "key": key, "reason": "ttl expired"}
    if not semantic_enabled or not semantic_query:
        return {"status": "miss", "key": key, "reason": "no exact match"}
    best: tuple[str, dict[str, Any], float] | None = None
    for candidate_key, candidate in entries.items():
        if not isinstance(candidate, dict) or _expired(candidate, now):
            continue
        score = _jaccard(semantic_query, str(candidate.get("semantic_text", "")))
        if score >= semantic_threshold and (best is None or score > best[2]):
            best = (candidate_key, candidate, score)
    if best:
        return {"status": "semantic_hit", "key": key, "source_key": best[0], "similarity": best[2], "entry": best[1]}
    return {"status": "miss", "key": key, "reason": "semantic threshold not met", "semantic_threshold": semantic_threshold}


def cache_store(
    key: str,
    value: Any,
    *,
    cache_class: str,
    ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    semantic_text: str = "",
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    if cache_class not in SAFE_CACHE_CLASSES:
        raise ValueError(f"unsafe cache class {cache_class!r}")
    path = path or exact_cache_path()
    with _CACHE_LOCK:
        payload = _load_cache(path)
        expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_seconds)
        entry = {
            "cache_class": cache_class,
            "created_at": iso_now(),
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "semantic_text": semantic_text,
            "metadata": metadata or {},
            "value": value,
        }
        payload["entries"][key] = entry
        _write_cache(path, payload)
    return entry


def _expired(entry: dict[str, Any], now: dt.datetime) -> bool:
    raw = entry.get("expires_at")
    if not isinstance(raw, str):
        return False
    try:
        expiry = dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    return expiry <= now


def _jaccard(left: str, right: str) -> float:
    left_words = set(re.findall(r"[a-z0-9_]+", left.lower()))
    right_words = set(re.findall(r"[a-z0-9_]+", right.lower()))
    if not left_words or not right_words:
        return 0.0
    return len(left_words & right_words) / len(left_words | right_words)


def compress_context(
    text: str,
    *,
    mode: str = "off",
    max_chars: int = 8000,
    external_command: str | None = None,
) -> dict[str, Any]:
    original = text or ""
    if mode == "off":
        compressed = original
        warning = ""
        quality_risk = "none"
    elif mode == "trim":
        compressed = original[:max_chars]
        warning = "trimmed tail content" if len(original) > len(compressed) else ""
        quality_risk = "medium" if warning else "none"
    elif mode == "summarize":
        compressed = _summarize_text(original, max_chars=max_chars)
        warning = "deterministic summary is lossy" if compressed != original else ""
        quality_risk = "medium" if warning else "none"
    elif mode == "external":
        if not external_command:
            return _compression_fallback(original, mode, "external compressor command is required")
        try:
            proc = subprocess.run(
                shlex.split(external_command),
                input=original,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _compression_fallback(original, mode, str(exc))
        if proc.returncode != 0:
            return _compression_fallback(original, mode, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())
        compressed = proc.stdout[:max_chars]
        warning = "external compressor output truncated" if len(proc.stdout) > len(compressed) else ""
        quality_risk = "external"
    else:
        raise ValueError("--compress must be off, trim, summarize, or external")
    return _compression_record(original, compressed, mode=mode, warning=warning, quality_risk=quality_risk)


def _compression_fallback(text: str, mode: str, warning: str) -> dict[str, Any]:
    record = _compression_record(text, text, mode=mode, warning=f"compression failed; using original: {warning}", quality_risk="none")
    record["fallback"] = True
    return record


def _compression_record(text: str, compressed: str, *, mode: str, warning: str, quality_risk: str) -> dict[str, Any]:
    original_tokens = estimate_tokens(text)
    compressed_tokens = estimate_tokens(compressed)
    return {
        "mode": mode,
        "original": text,
        "compressed": compressed,
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "ratio": (compressed_tokens / original_tokens) if original_tokens else 1.0,
        "warning": warning,
        "quality_risk": quality_risk,
        "fallback": False,
    }


def _summarize_text(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    total = 0
    for sentence in sentences:
        if not sentence:
            continue
        if total + len(sentence) + 1 > max_chars:
            break
        kept.append(sentence)
        total += len(sentence) + 1
    if kept:
        return " ".join(kept)
    return text[:max_chars]


def choose_route(
    *,
    policy: str,
    prompt: str,
    cache_status: str = "miss",
    premium_terms: Iterable[str] | None = None,
) -> dict[str, Any]:
    policy = (policy or "standard").lower()
    if policy in {"off", "no-route", "none"}:
        return {"route": "standard", "reason": "routing disabled", "policy": policy}
    if policy == "cache-first" and cache_status in {"hit", "semantic_hit"}:
        return {"route": "cache", "reason": f"cache {cache_status}", "policy": policy}
    words = re.findall(r"[a-z0-9_]+", prompt.lower())
    word_set = set(words)
    premium = set(premium_terms or {"security", "production", "credential", "merge", "release", "schema", "migration"})
    if policy in {"cheap-classifier", "cache-first"} and len(words) <= 40 and not (word_set & premium):
        return {"route": "cheap", "reason": "short low-risk classification", "policy": policy}
    if word_set & premium:
        return {"route": "premium", "reason": "risk/complexity terms present", "policy": policy}
    return {"route": "standard", "reason": "default route", "policy": policy}


def usage_path(run_id: str) -> Path:
    return state_dir() / "runs" / safe_fragment(run_id) / "usage.json"


def write_usage(run_id: str, payload: dict[str, Any]) -> Path:
    path = usage_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_usage(run_id: str) -> dict[str, Any]:
    path = usage_path(run_id)
    if not path.exists():
        raise FileNotFoundError(f"no usage record for {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def build_scorecard(
    *,
    run_id: str,
    command: str,
    projected: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
    budget_usd: float | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    rows = records or []
    actual_input = sum(int(row.get("input_tokens") or 0) for row in rows)
    actual_output = sum(int(row.get("output_tokens") or 0) for row in rows)
    cached_tokens = sum(int(row.get("cached_tokens") or 0) for row in rows)
    cache_hits = len([row for row in rows if row.get("cache_status") in {"hit", "semantic_hit"}])
    compression_saved = sum(int(row.get("compression_saved_tokens") or 0) for row in rows)
    projected_total = (projected or {}).get("total_tokens")
    actual_total = actual_input + actual_output if rows else None
    scorecard = {
        "schema_version": "1.0",
        "run_id": run_id,
        "command": command,
        "updated_at": iso_now(),
        "projected": projected or {},
        "actual": {
            "available": bool(rows),
            "records": rows,
            "input_tokens": actual_input if rows else None,
            "output_tokens": actual_output if rows else None,
            "total_tokens": actual_total,
            "cached_tokens": cached_tokens,
            "cache_hits": cache_hits,
            "cache_misses": len([row for row in rows if row.get("cache_status") == "miss"]),
            "compression_saved_tokens": compression_saved,
        },
        "budget": {"budget_usd": budget_usd, "status": "unknown"},
        "warnings": warnings or [],
    }
    if budget_usd is not None and projected and projected.get("cost_usd") is not None:
        scorecard["budget"]["status"] = "projected_over" if float(projected["cost_usd"]) > budget_usd else "projected_ok"
    if projected_total and actual_total and actual_total > float(projected_total) * 1.5:
        scorecard["warnings"].append("actual tokens exceeded projected tokens by more than 50%")
    return scorecard


def format_scorecard(payload: dict[str, Any]) -> str:
    actual = payload.get("actual", {})
    projected = payload.get("projected", {})
    lines = [
        f"Usage scorecard: {payload.get('run_id')}",
        f"Command: {payload.get('command')}",
        f"Projected tokens: {projected.get('total_tokens', 'unknown')}",
        f"Actual tokens: {actual.get('total_tokens', 'unknown')}",
        f"Cached tokens: {actual.get('cached_tokens', 0)}",
        f"Cache hits/misses: {actual.get('cache_hits', 0)}/{actual.get('cache_misses', 0)}",
        f"Compression saved tokens: {actual.get('compression_saved_tokens', 0)}",
        f"Budget status: {payload.get('budget', {}).get('status', 'unknown')}",
    ]
    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def sign_message(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def trace_optimization(run_id: str | None, event_type: str, data: dict[str, Any]) -> None:
    try:
        emit_event(event_type, run_id=run_id, data=data)
    except Exception:
        print(f"agent-bridge warning: could not emit {event_type}", file=sys.stderr)
