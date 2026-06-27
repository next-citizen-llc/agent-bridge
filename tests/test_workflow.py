from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge.workflow import (
    FakeEngineAdapter,
    inspect_workflow_run,
    list_workflows,
    load_workflow,
    resolve_engine,
    run_workflow,
    strict_json_schema,
    workflow_run_dir,
)


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class WorkflowTests(unittest.TestCase):
    def test_workflow_spec_loads_and_lists(self) -> None:
        spec = load_workflow("deep-research-lite")
        self.assertEqual(spec["id"], "deep-research-lite")
        self.assertIn("scope", spec["schemas"])
        self.assertIn("search", spec["prompts"])
        self.assertTrue(any(row["id"] == "deep-research-lite" for row in list_workflows()))

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

    def test_cli_workflow_list_show_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            list_proc = subprocess.run(
                [str(AGENT), "workflow", "list"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            show_proc = subprocess.run(
                [str(AGENT), "workflow", "show", "deep-research-lite"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            run_proc = subprocess.run(
                [
                    str(AGENT),
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

        self.assertEqual(list_proc.returncode, 0, list_proc.stderr)
        self.assertIn("deep-research-lite", list_proc.stdout)
        self.assertEqual(show_proc.returncode, 0, show_proc.stderr)
        self.assertIn("Phases:", show_proc.stdout)
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        self.assertIn("Workflow: deep-research-lite", run_proc.stdout)
        self.assertIn("Engine: codex", run_proc.stdout)
        self.assertIn("Dry run: yes", run_proc.stdout)

    def test_cli_dry_run_engine_defaults_to_caller(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"), "AGENT_BRIDGE_CALLER": "claude"}
            proc = subprocess.run(
                [
                    str(AGENT),
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
