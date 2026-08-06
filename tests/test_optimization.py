from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_bridge.optimization import (
    cache_key,
    cache_lookup,
    cache_store,
    cacheability_report,
    choose_route,
    compress_context,
)
from agent_bridge.workflow import FakeEngineAdapter, run_workflow, workflow_run_dir


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        payload = {
            "choices": [{"message": {"content": "GATEWAY_OK"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class OptimizationTests(unittest.TestCase):
    def test_cacheability_and_route_reports_are_deterministic(self) -> None:
        report = cacheability_report(prefix="stable " * 600, task="change one file", provider="test", minimum_tokens=100)
        route = choose_route(policy="cheap-classifier", prompt="classify this small request")

        self.assertTrue(report["likely_provider_cacheable"])
        self.assertEqual(len(report["prefix_fingerprint"]), 16)
        self.assertEqual(route["route"], "cheap")

    def test_exact_and_semantic_cache_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AGENT_BRIDGE_STATE_DIR": tmp}):
            key = cache_key(cache_class="review", provider="fake", model="small", prefix="p", task="same question")
            cache_store(key, {"answer": "cached"}, cache_class="review", semantic_text="same question about routing")

            exact = cache_lookup(key)
            miss = cache_lookup("missing", semantic_query="same question routing", semantic_enabled=False)
            semantic = cache_lookup("missing", semantic_query="same question routing", semantic_enabled=True, semantic_threshold=0.4)

        self.assertEqual(exact["status"], "hit")
        self.assertEqual(miss["status"], "miss")
        self.assertEqual(semantic["status"], "semantic_hit")

    def test_compression_modes_preserve_audit_metadata(self) -> None:
        text = "First sentence. Second sentence has useful detail. Third sentence is extra."
        trimmed = compress_context(text, mode="trim", max_chars=20)
        summarized = compress_context(text, mode="summarize", max_chars=32)

        self.assertLess(trimmed["compressed_tokens"], trimmed["original_tokens"])
        self.assertEqual(trimmed["quality_risk"], "medium")
        self.assertTrue(summarized["warning"])

    def test_gateway_status_and_chat_use_fake_openai_compatible_server(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _GatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "agents.json"
                config.write_text(
                    json.dumps(
                        {
                            "agents": [
                                {
                                    "id": "gateway",
                                    "label": "Gateway",
                                    "adapter": "openai_gateway",
                                    "gateway": {
                                        "base_url": f"http://127.0.0.1:{server.server_port}",
                                        "provider": "fake",
                                        "model_alias": "cheap-model",
                                        "budget_tag": "test",
                                    },
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                status = subprocess.run(
                    [str(AGENT), "code", "gateway", "status", "--config", str(config), "--json"],
                    cwd=str(ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                chat = subprocess.run(
                    [str(AGENT), "code", "gateway", "chat", "--config", str(config), "--to", "gateway", "--prompt", "hello"],
                    cwd=str(ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)[0]["route"], "gateway")
        self.assertEqual(chat.returncode, 0, chat.stderr)
        self.assertIn("GATEWAY_OK", chat.stdout)

    def test_workflow_cache_compression_and_usage_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state")}
            fetch = {
                "ok": True,
                "url": "https://example.com/primary",
                "excerpt": "Fixture quote. " * 200,
                "path": str(Path(tmp) / "source.txt"),
                "error": "",
            }
            with patch.dict(os.environ, env, clear=False), patch("agent_bridge.workflow.fetch_source_excerpt", return_value=fetch):
                first = run_workflow(
                    workflow_id="deep-research-lite",
                    question="fixture question",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-optimization-first"},
                    adapter=FakeEngineAdapter(),
                    cache_mode="exact",
                    compression_mode="trim",
                    compression_max_chars=80,
                )
                run_workflow(
                    workflow_id="deep-research-lite",
                    question="fixture question",
                    tier="shallow",
                    engine="codex",
                    source="codex",
                    project_dir=ROOT,
                    concurrency=2,
                    meta={"run_id": "run-optimization-second"},
                    adapter=FakeEngineAdapter(),
                    cache_mode="exact",
                    compression_mode="trim",
                    compression_max_chars=80,
                )
                usage_path = workflow_run_dir("run-optimization-second") / "usage.json"
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
                report_text = (workflow_run_dir("run-optimization-second") / "report.md").read_text(encoding="utf-8")
                usage_exists = usage_path.exists()

        self.assertTrue(first["optimization"]["compression"])
        self.assertTrue(any(row.get("cache_status") == "hit" for row in usage["actual"]["records"]))
        self.assertTrue(usage_exists)
        self.assertIn("Usage", report_text)


if __name__ == "__main__":
    unittest.main()
