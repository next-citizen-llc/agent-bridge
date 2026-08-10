from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge.workflow import (
    ExternalEngineAdapter,
    FakeEngineAdapter,
    WorkflowError,
    _extract_json_object,
    _response_usage,
    format_report,
    inspect_workflow_run,
    list_workflows,
    load_workflow,
    plan_workflow_run,
    project_usage,
    resolve_engine,
    run_workflow,
    summarize_actual_usage,
    strict_json_schema,
    workflow_run_dir,
)


ROOT = Path(__file__).resolve().parents[1]
AGENT_CMD = [str(ROOT / "bin" / "agent")] if os.name != "nt" else [sys.executable, "-m", "agent_bridge.cli"]


class ConcurrencyTrackingAdapter(FakeEngineAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self._parallel_started = threading.Event()

    def call(self, call):
        parallel_phase = call.label.startswith("collector:") or call.label.startswith("worker:")
        if not parallel_phase:
            return super().call(call)
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active >= 2:
                self._parallel_started.set()
        try:
            self._parallel_started.wait(timeout=1)
            return super().call(call)
        finally:
            with self._lock:
                self._active -= 1


class DecliningAdapter(FakeEngineAdapter):
    def call(self, call):
        result = super().call(call)
        if call.label == "team-scope":
            result["teamFit"] = False
            result["fitReason"] = "The fixture is one serial decision."
        return result


class FailedVerifierAdapter(FakeEngineAdapter):
    def call(self, call):
        result = super().call(call)
        if call.label.startswith("verifier:"):
            result.update(
                {
                    "verdict": "fail",
                    "summary": "A central fixture claim contradicted its snapshot.",
                    "contradictions": [
                        {
                            "description": "Fixture contradiction.",
                            "packetIds": ["packet-a"],
                            "evidence": "Fixture evidence.",
                            "resolution": "Do not publish packet-a.",
                        }
                    ],
                    "gaps": ["Recheck packet-a."],
                    "rerunCommands": ["fixture read"],
                    "confidence": "high",
                }
            )
        return result


class CaveatedVerifierAdapter(FakeEngineAdapter):
    def call(self, call):
        result = super().call(call)
        if call.label.startswith("verifier:"):
            result["verdict"] = "pass_with_caveats"
            result["gaps"] = ["Fixture caveat."]
        return result


class InvalidShardAdapter(FakeEngineAdapter):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def call(self, call):
        result = super().call(call)
        if call.label != "team-shard":
            return result
        if self.mode == "too_few":
            result["workPackets"] = result["workPackets"][:1]
        elif self.mode == "dependent":
            result["workPackets"][1]["dependsOn"] = ["packet-a"]
        elif self.mode == "unknown_authority":
            result["workPackets"][0]["authorityIds"] = ["source-typo"]
        return result


class ExcessAuthorityAdapter(FakeEngineAdapter):
    def call(self, call):
        result = super().call(call)
        if call.label == "team-scope":
            result["authorities"].append(
                {
                    "id": "source-c",
                    "label": "Fixture source C",
                    "sourceKind": "files",
                    "task": "Collect fixture C.",
                    "authoritativeCommands": ["read fixture C"],
                    "outputContract": "Return fixture C facts.",
                }
            )
        return result


class BlockedCollectorAdapter(FakeEngineAdapter):
    def call(self, call):
        if call.label == "collector:source-a":
            raise WorkflowError("fixture collector timeout")
        return super().call(call)


class WorkflowTests(unittest.TestCase):
    def test_workflow_spec_loads_and_lists(self) -> None:
        spec = load_workflow("deep-research-lite")
        self.assertEqual(spec["id"], "deep-research-lite")
        self.assertIn("scope", spec["schemas"])
        self.assertIn("search", spec["prompts"])
        self.assertTrue(any(row["id"] == "deep-research-lite" for row in list_workflows()))

    def test_agent_team_spec_loads_and_lists(self) -> None:
        spec = load_workflow("agent-team")
        self.assertEqual(spec["runner"], "agent-team")
        self.assertEqual(spec["tiers"]["standard"]["workers"], 3)
        self.assertIn("collector", spec["schemas"])
        self.assertIn("integrate", spec["prompts"])
        self.assertTrue(any(row["id"] == "agent-team" for row in list_workflows()))

    def test_resolve_engine_precedence(self) -> None:
        with patch.dict(os.environ, {"AGENT_BRIDGE_CALLER": "claude"}, clear=False):
            self.assertEqual(resolve_engine("codex", "claude"), "codex")
            self.assertEqual(resolve_engine("auto", "codex"), "codex")
            self.assertEqual(resolve_engine("auto", "human"), "claude")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_engine("auto", "human"), "codex")

    def test_strict_json_schema_closes_nested_objects(self) -> None:
        schema = {
            "type": "object",
            "required": ["rows"],
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                }
            },
        }

        strict = strict_json_schema(schema)

        self.assertFalse(strict["additionalProperties"])
        self.assertFalse(strict["properties"]["rows"]["items"]["additionalProperties"])
        self.assertEqual(strict["required"], ["rows"])
        self.assertEqual(strict["properties"]["rows"]["items"]["required"], ["name"])
        self.assertNotIn("additionalProperties", schema)

    def test_strict_json_schema_makes_optional_properties_nullable(self) -> None:
        schema = {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {"type": "string"},
                "note": {"type": "string"},
            },
        }

        strict = strict_json_schema(schema)

        self.assertEqual(strict["required"], ["name", "note"])
        self.assertEqual(strict["properties"]["name"], {"type": "string"})
        self.assertEqual(strict["properties"]["note"], {"anyOf": [{"type": "string"}, {"type": "null"}]})

    def test_fake_workflow_writes_stable_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            with patch.dict(os.environ, env, clear=False), patch(
                "agent_bridge.workflow.fetch_source_excerpt",
                return_value={
                    "ok": True,
                    "url": "https://example.com/primary",
                    "excerpt": "Fixture quote.",
                    "path": str(Path(tmp) / "source.txt"),
                    "error": "",
                },
            ):
                result = run_workflow(
                    workflow_id="deep-research-lite",
                    question="fixture question",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-workflow-fixture"},
                    adapter=FakeEngineAdapter(),
                )
                run_dir = workflow_run_dir("run-workflow-fixture")
                inspected = inspect_workflow_run("run-workflow-fixture")
                self.assertEqual(result["workflow_id"], "deep-research-lite")
                self.assertEqual(result["run_id"], "run-workflow-fixture")
                self.assertEqual(result["engine"], "codex")
                self.assertEqual(result["tier"], "shallow")
                self.assertIn("Fixture summary", result["summary"])
                self.assertTrue((run_dir / "manifest.json").exists())
                self.assertTrue((run_dir / "report.md").exists())
                self.assertTrue((run_dir / "result.json").exists())
                self.assertEqual(inspected["result"]["run_id"], "run-workflow-fixture")

    def test_agent_team_runner_fans_out_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            adapter = ConcurrencyTrackingAdapter()
            with patch.dict(os.environ, env, clear=False):
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Audit two independent fixture authorities.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-agent-team-fixture"},
                    adapter=adapter,
                )
                run_dir = workflow_run_dir("run-agent-team-fixture")
                inspected = inspect_workflow_run("run-agent-team-fixture")

                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["workflow_id"], "agent-team")
                self.assertEqual(result["team"]["collectors"], 2)
                self.assertEqual(result["team"]["workers"], 2)
                self.assertEqual(result["team"]["verifiers"], 1)
                self.assertGreaterEqual(adapter.max_active, 2)
                self.assertEqual(len(list((run_dir / "snapshots").glob("*.json"))), 2)
                self.assertEqual(len(list((run_dir / "workers").glob("*.json"))), 2)
                self.assertTrue((run_dir / "verification.json").exists())
                self.assertEqual(inspected["result"]["quality"]["coverage"], "complete")
                self.assertIn("snapshots", inspected["manifest"]["artifacts"])
                snapshot = json.loads(next((run_dir / "snapshots").glob("*.json")).read_text(encoding="utf-8"))
                self.assertIn("snapshotPath", snapshot)
                report = (run_dir / "report.md").read_text(encoding="utf-8")
                self.assertIn("Status: complete", report)
                self.assertIn("## Independent Verification", report)

    def test_agent_team_declines_unshardable_task_without_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                adapter = DecliningAdapter()
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Make one serial fixture decision.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    meta={"run_id": "run-agent-team-declined"},
                    adapter=adapter,
                )

        self.assertEqual(result["status"], "declined")
        self.assertEqual(result["team"]["workers"], 0)
        self.assertFalse(any(call.label.startswith("collector:") for call in adapter.calls))
        self.assertIn("Status: declined", format_report(result))

    def test_agent_team_failed_verification_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Audit fixture claims.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-agent-team-verification-failed"},
                    adapter=FailedVerifierAdapter(),
                )

        report = format_report(result)
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("Status: failed_verification", report)
        self.assertIn("Verifier 1: fail", report)
        self.assertIn("Fixture contradiction", report)

    def test_agent_team_pass_with_caveats_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Audit fixture claims with caveats.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-agent-team-verification-caveat"},
                    adapter=CaveatedVerifierAdapter(),
                )

        self.assertEqual(result["status"], "complete_with_caveats")
        self.assertIn("Status: complete_with_caveats", format_report(result))

    def test_agent_team_invalid_shards_stop_before_workers_and_persist(self) -> None:
        for mode in ("too_few", "dependent", "unknown_authority"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                run_id = "run-agent-team-invalid-" + mode
                adapter = InvalidShardAdapter(mode)
                with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                    result = run_workflow(
                        workflow_id="agent-team",
                        question="Audit invalid fixture shards.",
                        tier="shallow",
                        engine="codex",
                        source="codex",
                        project_dir=ROOT,
                        concurrency=2,
                        meta={"run_id": run_id},
                        adapter=adapter,
                    )
                    run_dir = workflow_run_dir(run_id)
                    self.assertEqual(result["status"], "failed_validation")
                    self.assertFalse(any(call.label.startswith("worker:") for call in adapter.calls))
                    self.assertTrue((run_dir / "result.json").exists())
                    self.assertTrue((run_dir / "snapshots").exists())

    def test_agent_team_refuses_to_silently_truncate_authorities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            adapter = ExcessAuthorityAdapter()
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Audit three fixture authorities.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    meta={"run_id": "run-agent-team-excess-authorities"},
                    adapter=adapter,
                )

        self.assertEqual(result["status"], "failed_validation")
        self.assertIn("refusing to silently drop evidence", result["caveats"])
        self.assertFalse(any(call.label.startswith("collector:") for call in adapter.calls))

    def test_agent_team_preserves_blocked_collector_as_failed_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}, clear=False):
                result = run_workflow(
                    workflow_id="agent-team",
                    question="Audit fixture authorities with one timeout.",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-agent-team-blocked-collector"},
                    adapter=BlockedCollectorAdapter(),
                )
                run_dir = workflow_run_dir("run-agent-team-blocked-collector")
                result_exists = (run_dir / "result.json").exists()

        self.assertEqual(result["status"], "failed_execution")
        self.assertEqual(result["snapshots"][0]["sourceStatus"], "blocked")
        self.assertIn("unknown, not empty", result["snapshots"][0]["limitations"][0])
        self.assertTrue(result_exists)

    def test_agent_team_projection_has_context_aware_ceiling(self) -> None:
        projection = project_usage(
            load_workflow("agent-team"),
            question="Audit fixture authorities.",
            tier="deep",
            engine="codex",
            budget_usd="2.00",
            pricing={"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0},
        )

        self.assertEqual(projection["calls_by_phase"]["execute"], 5)
        self.assertEqual(projection["calls_by_phase"]["verify"], 2)
        self.assertGreater(projection["max_input_tokens"], projection["input_tokens"])
        self.assertGreater(projection["max_cost_usd"], projection["cost_usd"])
        self.assertEqual(projection["budget_scope"], "per_call_engine_limit")

        claude_projection = project_usage(
            load_workflow("agent-team"),
            question="Audit fixture authorities.",
            tier="shallow",
            engine="claude",
            budget_usd="1.00",
            pricing={"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0},
        )
        self.assertGreater(claude_projection["total_tokens"], 650_000)

    def test_agent_team_allows_claude_read_only_plan(self) -> None:
        plan = plan_workflow_run(
            workflow_id="agent-team",
            question="Audit local fixture authorities.",
            tier="shallow",
            engine="claude",
            source="claude",
            meta={"run_id": "run-agent-team-claude-plan"},
        )

        self.assertEqual(plan["engine"], "claude")

    def test_claude_agent_team_tool_boundary_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            response_path = Path(tmp) / "response.json"
            schema_path.write_text("{}", encoding="utf-8")
            adapter = ExternalEngineAdapter(
                engine="claude",
                command="claude",
                project_dir=ROOT,
                run_dir=Path(tmp),
                allowed_tools="Read,Grep,Glob,WebSearch,WebFetch",
                timeout_seconds=300,
            )
            large_prompt = "fixture" * 10_000
            command = adapter._command(large_prompt, schema_path, response_path)

        allowed = command[command.index("--allowedTools") + 1]
        self.assertEqual(allowed, "Read,Grep,Glob,WebSearch,WebFetch")
        self.assertNotIn("Write", allowed)
        self.assertNotIn("Edit", allowed)
        self.assertNotIn("Bash", allowed)
        self.assertEqual(adapter.timeout_seconds, 300)
        self.assertNotIn(large_prompt, command)
        self.assertIn("--input-format", command)

    def test_claude_wrapper_usage_is_preserved(self) -> None:
        parsed = _extract_json_object(
            json.dumps(
                {
                    "total_cost_usd": 0.12,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 30,
                        "cache_read_input_tokens": 20,
                        "output_tokens": 4,
                    },
                    "structured_output": {"summary": "fixture"},
                }
            )
        )
        normalized = _response_usage(parsed)
        actual = summarize_actual_usage(
            [{"input_tokens": normalized["input_tokens"], "output_tokens": normalized["output_tokens"], "cost_usd": normalized["cost_usd"]}],
            {"input_usd_per_mtok": 99.0, "output_usd_per_mtok": 99.0},
        )

        self.assertEqual(parsed["_usage"]["input_tokens"], 10)
        self.assertEqual(parsed["_usage"]["cost_usd"], 0.12)
        self.assertEqual(normalized["input_tokens"], 60)
        self.assertEqual(normalized["output_tokens"], 4)
        self.assertEqual(actual["total_tokens"], 64)
        self.assertEqual(actual["cost_usd"], 0.12)
        self.assertEqual(actual["cost_source"], "provider")

    def test_cli_workflow_list_show_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            list_proc = subprocess.run(
                [*AGENT_CMD, "workflow", "list"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            show_proc = subprocess.run(
                [*AGENT_CMD, "workflow", "show", "deep-research-lite"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            show_team_proc = subprocess.run(
                [*AGENT_CMD, "workflow", "show", "agent-team"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            run_proc = subprocess.run(
                [
                    *AGENT_CMD,
                    "workflow",
                    "run",
                    "deep-research-lite",
                    "--question",
                    "fixture question",
                    "--tier",
                    "shallow",
                    "--engine",
                    "codex",
                    "--run-id",
                    "run-cli-dry",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            team_run_proc = subprocess.run(
                [
                    *AGENT_CMD,
                    "workflow",
                    "run",
                    "agent-team",
                    "--question",
                    "Audit two independent fixture authorities.",
                    "--tier",
                    "shallow",
                    "--engine",
                    "codex",
                    "--run-id",
                    "run-cli-team-dry",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertIn("deep-research-lite", list_proc.stdout)
        self.assertIn("agent-team", list_proc.stdout)
        self.assertEqual(show_proc.returncode, 0, show_proc.stderr)
        self.assertIn("Phases:", show_proc.stdout)
        self.assertEqual(show_team_proc.returncode, 0, show_team_proc.stderr)
        self.assertIn("2 collectors, 2 workers, 1 verifier", show_team_proc.stdout)
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        self.assertIn("Workflow: deep-research-lite", run_proc.stdout)
        self.assertIn("Engine: codex", run_proc.stdout)
        self.assertIn("Dry run: yes", run_proc.stdout)
        self.assertEqual(team_run_proc.returncode, 0, team_run_proc.stderr)
        self.assertIn("Workflow: agent-team", team_run_proc.stdout)

    def test_cli_dry_run_engine_defaults_to_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"), "AGENT_BRIDGE_CALLER": "claude"}
            proc = subprocess.run(
                [
                    *AGENT_CMD,
                    "workflow",
                    "run",
                    "deep-research-lite",
                    "--question",
                    "fixture question",
                    "--dry-run",
                    "--format",
                    "json",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["engine"], "claude")


if __name__ == "__main__":
    unittest.main()
