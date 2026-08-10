"""Snapshot-Shard-Verify runner for the portable agent-team workflow."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any, Callable


CallModel = Callable[..., dict[str, Any]]
ParallelMap = Callable[[list[Any], Callable[[Any], Any], int], list[Any]]
EmitPhase = Callable[[str, dict[str, Any]], None]


def run_agent_team(
    *,
    spec: dict[str, Any],
    raw_task: str,
    tier_arg: str,
    adapter: Any,
    run_dir: Path,
    project_dir: Path,
    concurrency: int,
    run_meta: dict[str, Any],
    call_model: CallModel,
    parallel_map: ParallelMap,
    emit_phase: EmitPhase,
) -> dict[str, Any]:
    """Run one read-only Snapshot-Shard-Verify team."""

    started = time.perf_counter()
    timings: dict[str, float] = {}
    forced_tier, task = _forced_tier(raw_task, tier_arg)
    engine = str(getattr(adapter, "engine", "unknown"))
    engine_capabilities = (
        "Claude may use Read, Grep, Glob, WebSearch, and WebFetch. It has no shell, Git, gh, or CI command access; mark those authorities blocked and do not invent substitutes."
        if engine == "claude"
        else "Codex may use read-only shell and web tools when its sandbox readiness allows. If shell or authenticated access fails, mark the authority blocked after one retry."
    )

    scope_started = time.perf_counter()
    emit_phase("Scope", run_meta)
    scope = call_model(
        adapter,
        spec,
        "scope",
        "scope",
        "team-scope",
        "Scope",
        {
            "task": task,
            "project_dir": str(project_dir),
            "tier": forced_tier or "auto",
            "engine_capabilities": engine_capabilities,
            "team_limits": _prompt_json(
                {
                    tier_name: {
                        "collectors": tier_cfg["collectors"],
                        "workers": tier_cfg["workers"],
                        "verifiers": tier_cfg["verifiers"],
                    }
                    for tier_name, tier_cfg in spec["tiers"].items()
                }
            ),
        },
    )
    timings["scope"] = _elapsed(scope_started)
    resolved_tier = forced_tier or _valid_tier(scope.get("profile"), spec) or "standard"
    cfg = spec["tiers"][resolved_tier]
    _write_json(run_dir / "scope.json", scope)

    if not scope.get("teamFit"):
        return _declined_result(task, resolved_tier, cfg, scope, timings, started, run_dir, adapter)

    authorities = _unique_by_id(scope.get("authorities") or [])
    collector_limit = int(cfg["collectors"])
    if len(authorities) > collector_limit:
        return _failed_result(
            task=task,
            tier=resolved_tier,
            cfg=cfg,
            scope=scope,
            timings=timings,
            started=started,
            run_dir=run_dir,
            adapter=adapter,
            reason=f"The {resolved_tier} profile allows {collector_limit} collectors, but scope returned {len(authorities)} authorities; refusing to silently drop evidence.",
        )
    if not authorities:
        return _failed_result(
            task=task,
            tier=resolved_tier,
            cfg=cfg,
            scope=scope,
            timings=timings,
            started=started,
            run_dir=run_dir,
            adapter=adapter,
            reason="The coordinator declared teamFit=true but supplied no authoritative sources.",
        )

    collect_started = time.perf_counter()
    emit_phase("Collect", run_meta)
    snapshots_dir = run_dir / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    def collect(job: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, authority = job
        try:
            result = call_model(
                adapter,
                spec,
                "collect",
                "collector",
                "collector:" + str(authority["id"]),
                "Collect",
                {
                    "task": task,
                    "project_dir": str(project_dir),
                    "authority": _prompt_json(authority),
                    "engine_capabilities": engine_capabilities,
                },
            )
        except Exception as exc:
            result = _blocked_collector(authority, exc)
        result["authorityId"] = str(authority["id"])
        path = snapshots_dir / f"{index:02d}_{_safe_name(str(authority['id']))}.json"
        result["snapshotPath"] = str(path)
        _write_json(path, result)
        return result

    snapshots = parallel_map(list(enumerate(authorities, start=1)), collect, concurrency)
    timings["collect"] = _elapsed(collect_started)

    shard_started = time.perf_counter()
    emit_phase("Shard", run_meta)
    snapshot_block = _prompt_json(snapshots)
    try:
        shard_plan = call_model(
            adapter,
            spec,
            "shard",
            "shard",
            "team-shard",
            "Shard",
            {
                "task": task,
                "profile_limits": _prompt_json(cfg),
                "suggested_shards": _prompt_json(scope.get("suggestedShards") or []),
                "snapshots": snapshot_block,
            },
        )
    except Exception as exc:
        timings["shard"] = _elapsed(shard_started)
        return _failed_result(
            task=task,
            tier=resolved_tier,
            cfg=cfg,
            scope=scope,
            timings=timings,
            started=started,
            run_dir=run_dir,
            adapter=adapter,
            reason="The shard coordinator call failed: " + _error_text(exc),
            snapshots=snapshots,
        )
    timings["shard"] = _elapsed(shard_started)
    _write_json(run_dir / "shard-plan.json", shard_plan)
    raw_packets = shard_plan.get("workPackets")
    packets = _unique_by_id(raw_packets if isinstance(raw_packets, list) else [])[: int(cfg["workers"])]
    validation_errors: list[str] = []
    if len(packets) < 2:
        validation_errors.append("The shard plan did not contain at least two independent work packets.")
    dependent = [str(packet.get("id")) for packet in packets if packet.get("dependsOn")]
    if dependent:
        validation_errors.append("Packet dependencies are not allowed: " + ", ".join(dependent) + ".")

    snapshot_by_id = {str(row.get("authorityId")): row for row in snapshots}
    unknown_authorities = {
        str(authority_id)
        for packet in packets
        for authority_id in (packet.get("authorityIds") or [])
        if str(authority_id) not in snapshot_by_id
    }
    empty_authorities = [str(packet.get("id")) for packet in packets if not packet.get("authorityIds")]
    if unknown_authorities:
        validation_errors.append("Packets referenced unknown authorities: " + ", ".join(sorted(unknown_authorities)) + ".")
    if empty_authorities:
        validation_errors.append("Packets omitted authorityIds: " + ", ".join(empty_authorities) + ".")
    if validation_errors:
        return _failed_result(
            task=task,
            tier=resolved_tier,
            cfg=cfg,
            scope=scope,
            timings=timings,
            started=started,
            run_dir=run_dir,
            adapter=adapter,
            reason=" ".join(validation_errors),
            snapshots=snapshots,
            packets=packets,
            shard_plan=shard_plan,
        )

    execute_started = time.perf_counter()
    emit_phase("Execute", run_meta)

    def execute(packet: dict[str, Any]) -> dict[str, Any]:
        authority_ids = [str(value) for value in packet.get("authorityIds") or []]
        relevant = [snapshot_by_id[value] for value in authority_ids]
        try:
            result = call_model(
                adapter,
                spec,
                "execute",
                "worker",
                "worker:" + str(packet["id"]),
                "Execute",
                {
                    "task": task,
                    "packet": _prompt_json(packet),
                    "snapshots": _prompt_json(relevant),
                },
            )
        except Exception as exc:
            result = _blocked_worker(packet, exc)
        result["packetId"] = str(packet["id"])
        return result

    worker_results = parallel_map(packets, execute, concurrency)
    timings["execute"] = _elapsed(execute_started)
    workers_dir = run_dir / "workers"
    workers_dir.mkdir(parents=True, exist_ok=True)
    for index, worker in enumerate(worker_results, start=1):
        _write_json(workers_dir / f"{index:02d}_{_safe_name(str(worker.get('packetId', 'packet')))}.json", worker)

    verify_started = time.perf_counter()
    emit_phase("Verify", run_meta)
    verifier_jobs = list(range(1, int(cfg["verifiers"]) + 1))
    verifier_lenses = [
        "source fidelity, claim accuracy, and contradictions",
        "coverage, packet boundaries, and integration risks",
    ]

    def verify(index: int) -> dict[str, Any]:
        try:
            return call_model(
                adapter,
                spec,
                "verify",
                "verifier",
                f"verifier:{index}",
                "Verify",
                {
                    "verifier_index": index,
                    "verifier_lens": verifier_lenses[(index - 1) % len(verifier_lenses)],
                    "task": task,
                    "snapshots": snapshot_block,
                    "worker_results": _prompt_json(worker_results),
                    "integration_risks": _prompt_json(shard_plan.get("integrationRisks") or []),
                },
            )
        except Exception as exc:
            return {
                "verdict": "fail",
                "summary": "The verifier call failed before completing its checks.",
                "validatedPacketIds": [],
                "contradictions": [],
                "gaps": [_error_text(exc)],
                "rerunCommands": [],
                "confidence": "low",
            }

    verifier_results = parallel_map(verifier_jobs, verify, concurrency)
    timings["verify"] = _elapsed(verify_started)
    _write_json(run_dir / "verification.json", verifier_results)

    integrate_started = time.perf_counter()
    emit_phase("Integrate", run_meta)
    integration_error = ""
    try:
        integrated = call_model(
            adapter,
            spec,
            "integrate",
            "integrate",
            "team-integrate",
            "Integrate",
            {
                "task": task,
                "single_agent_estimate": _prompt_json(scope.get("singleAgentEstimate") or {}),
                "snapshots": snapshot_block,
                "worker_results": _prompt_json(worker_results),
                "verifier_results": _prompt_json(verifier_results),
            },
        )
    except Exception as exc:
        integration_error = _error_text(exc)
        integrated = {
            "summary": "Agent Team completed its packets but the integration call failed.",
            "findings": [],
            "caveats": integration_error,
            "open_questions": ["Rerun integration against the preserved snapshots, worker results, and verifier results."],
            "recommended_actions": ["Inspect the preserved artifacts before retrying integration."],
            "quality": {
                "coverage": "partial",
                "accuracy": "low",
                "consistency": "low",
                "verificationSummary": "Integration failed; no final verified synthesis is available.",
            },
        }
    timings["integrate"] = _elapsed(integrate_started)
    timings["total"] = _elapsed(started)

    failed_verification = any(row.get("verdict") == "fail" for row in verifier_results)
    verification_caveats = any(row.get("verdict") == "pass_with_caveats" for row in verifier_results)
    failed_execution = any(row.get("sourceStatus") == "blocked" for row in snapshots) or any(
        row.get("status") == "blocked" for row in worker_results
    )
    baseline = scope.get("singleAgentEstimate") or {}
    baseline_seconds = max(0, int(baseline.get("wallTimeSeconds") or 0))
    speedup = round(baseline_seconds / timings["total"], 2) if baseline_seconds and timings["total"] else None

    quality = dict(integrated.get("quality") or {})
    if failed_verification:
        quality["accuracy"] = "low"
        quality["consistency"] = "low"
        quality["verificationSummary"] = "Independent verification failed; inspect verifier contradictions and gaps."
    elif failed_execution:
        quality["coverage"] = "partial"
        quality["accuracy"] = "low"
        quality["verificationSummary"] = "At least one collector or worker was blocked; inaccessible evidence remains unknown."

    return {
        "status": "failed_integration" if integration_error else "failed_verification" if failed_verification else "failed_execution" if failed_execution else "complete_with_caveats" if verification_caveats else "complete",
        "question": task,
        "task": task,
        "tier": resolved_tier,
        "profile": cfg.get("profile", resolved_tier),
        "summary": integrated.get("summary", ""),
        "findings": integrated.get("findings", []),
        "caveats": integrated.get("caveats", ""),
        "open_questions": integrated.get("open_questions", []),
        "recommended_actions": integrated.get("recommended_actions", []),
        "quality": quality,
        "scope": scope,
        "snapshots": snapshots,
        "packets": packets,
        "worker_results": worker_results,
        "verification": verifier_results,
        "artifacts": _artifact_paths(run_dir),
        "team": {
            "protocol": "Snapshot-Shard-Verify",
            "collectors": len(snapshots),
            "workers": len(worker_results),
            "verifiers": len(verifier_results),
            "concurrency": max(1, concurrency),
            "timingsSeconds": timings,
            "singleAgentEstimate": baseline,
            "baselineKind": "agent_estimate",
            "estimatedSpeedup": speedup,
        },
        "optimization": {"compression": [], "cache_mode": getattr(adapter, "cache_mode", "off")},
    }


def _declined_result(
    task: str,
    tier: str,
    cfg: dict[str, Any],
    scope: dict[str, Any],
    timings: dict[str, float],
    started: float,
    run_dir: Path,
    adapter: Any,
) -> dict[str, Any]:
    timings["total"] = _elapsed(started)
    reason = str(scope.get("fitReason") or "The objective is not safely shardable.")
    return {
        "status": "declined",
        "question": task,
        "task": task,
        "tier": tier,
        "profile": cfg.get("profile", tier),
        "summary": "Agent Team declined fanout: " + reason,
        "findings": [],
        "caveats": reason,
        "open_questions": [],
        "recommended_actions": ["Run this objective as one bounded agent or redesign it into independent packets."],
        "quality": {"coverage": "thin", "accuracy": "high", "consistency": "high", "verificationSummary": "No team was dispatched."},
        "scope": scope,
        "snapshots": [],
        "packets": [],
        "worker_results": [],
        "verification": [],
        "artifacts": _artifact_paths(run_dir),
        "team": {
            "protocol": "Snapshot-Shard-Verify",
            "collectors": 0,
            "workers": 0,
            "verifiers": 0,
            "concurrency": 0,
            "timingsSeconds": timings,
            "singleAgentEstimate": scope.get("singleAgentEstimate") or {},
            "baselineKind": "agent_estimate",
            "estimatedSpeedup": None,
        },
        "optimization": {"compression": [], "cache_mode": getattr(adapter, "cache_mode", "off")},
    }


def _failed_result(
    *,
    task: str,
    tier: str,
    cfg: dict[str, Any],
    scope: dict[str, Any],
    timings: dict[str, float],
    started: float,
    run_dir: Path,
    adapter: Any,
    reason: str,
    snapshots: list[dict[str, Any]] | None = None,
    packets: list[dict[str, Any]] | None = None,
    shard_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshots = snapshots or []
    packets = packets or []
    timings["total"] = _elapsed(started)
    return {
        "status": "failed_validation",
        "question": task,
        "task": task,
        "tier": tier,
        "profile": cfg.get("profile", tier),
        "summary": "Agent Team stopped before execution because its coordination plan was invalid.",
        "findings": [],
        "caveats": reason,
        "open_questions": [],
        "recommended_actions": ["Correct the authority or packet plan and rerun the workflow."],
        "quality": {
            "coverage": "thin",
            "accuracy": "low",
            "consistency": "low",
            "verificationSummary": "Execution was not started because coordination validation failed.",
        },
        "scope": scope,
        "snapshots": snapshots,
        "packets": packets,
        "shard_plan": shard_plan or {},
        "worker_results": [],
        "verification": [],
        "artifacts": _artifact_paths(run_dir),
        "team": {
            "protocol": "Snapshot-Shard-Verify",
            "collectors": len(snapshots),
            "workers": 0,
            "verifiers": 0,
            "concurrency": 0,
            "timingsSeconds": timings,
            "singleAgentEstimate": scope.get("singleAgentEstimate") or {},
            "baselineKind": "agent_estimate",
            "estimatedSpeedup": None,
        },
        "optimization": {"compression": [], "cache_mode": getattr(adapter, "cache_mode", "off")},
    }


def _forced_tier(raw_task: str, tier_arg: str) -> tuple[str | None, str]:
    match = re.match(r"^\[(shallow|standard|deep)\]\s*", raw_task, flags=re.IGNORECASE)
    tagged = match.group(1).lower() if match else None
    task = raw_task[match.end() :].strip() if match else raw_task.strip()
    return (tier_arg if tier_arg != "auto" else tagged), task


def _valid_tier(value: Any, spec: dict[str, Any]) -> str | None:
    text = str(value or "")
    return text if text in spec.get("tiers", {}) else None


def _unique_by_id(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("id") or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)
        result.append(row)
    return result


def _prompt_json(value: Any, *, max_chars: int = 60_000) -> str:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    fragment_chars = max(100, max_chars // 3)
    while True:
        bounded = json.dumps(
            {
                "truncated": True,
                "originalChars": len(text),
                "notice": "Evidence context was bounded by the runner; treat omitted content as unknown.",
                "head": text[:fragment_chars],
                "tail": text[-fragment_chars:],
            },
            indent=2,
            ensure_ascii=False,
        )
        if len(bounded) <= max_chars or fragment_chars <= 100:
            return bounded
        fragment_chars = max(100, int(fragment_chars * 0.8))


def _artifact_paths(run_dir: Path) -> dict[str, str]:
    candidates = {
        "scope": run_dir / "scope.json",
        "shard_plan": run_dir / "shard-plan.json",
        "snapshots": run_dir / "snapshots",
        "workers": run_dir / "workers",
        "verification": run_dir / "verification.json",
    }
    return {name: str(path) for name, path in candidates.items() if path.exists()}


def _blocked_collector(authority: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "authorityId": str(authority.get("id") or "unknown"),
        "sourceStatus": "blocked",
        "authoritative": False,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "summary": "The collector call failed before authoritative evidence could be captured.",
        "facts": [],
        "commands": [{"command": "agent collector call", "outcome": _error_text(exc)}],
        "artifacts": [],
        "limitations": ["This authority is unknown, not empty. " + _error_text(exc)],
    }


def _blocked_worker(packet: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "packetId": str(packet.get("id") or "unknown"),
        "status": "blocked",
        "summary": "The worker call failed before completing its packet.",
        "findings": [],
        "checks": [],
        "artifacts": [],
        "uncertainties": [_error_text(exc)],
        "efficiencyNote": "The failed call is preserved as a blocked packet rather than discarded.",
    }


def _error_text(exc: Exception) -> str:
    return (f"{type(exc).__name__}: {exc}").replace("\r", " ").replace("\n", " ")[:1000]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned[:80] or "item"


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 3)
