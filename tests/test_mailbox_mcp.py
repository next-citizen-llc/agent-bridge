from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "agent_bridge" / "mailbox_mcp.py"


class MailboxMcpTests(unittest.TestCase):
    def test_initialize_and_tools_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": tmp}
            payload = "\n".join(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    "",
                ]
            )
            proc = subprocess.run(
                ["python3", str(MCP)],
                input=payload,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(rows[0]["result"]["serverInfo"]["name"], "agent-mailbox")
        tool_names = {tool["name"] for tool in rows[1]["result"]["tools"]}
        self.assertTrue(
            {
                "mailbox_send",
                "mailbox_inbox",
                "mailbox_read",
                "trace_events",
                "finding_emit",
                "findings_list",
                "finding_read",
                "verdict_record",
                "verdicts_list",
            }.issubset(tool_names)
        )

    def test_mailbox_send_and_inbox_filter_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": tmp}
            payload = "\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "mailbox_send",
                                "arguments": {
                                    "from": "codex",
                                    "to": "claude",
                                    "subject": "smoke",
                                    "body": "hello",
                                    "run_id": "run-mcp",
                                    "loop_id": "loop-mcp",
                                    "role": "builder",
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "mailbox_inbox",
                                "arguments": {"to": "claude", "run_id": "run-mcp", "role": "builder"},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "mailbox_read",
                                "arguments": {"id": "m0001", "run_id": "run-mcp", "role": "critic"},
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 4,
                            "method": "tools/call",
                            "params": {
                                "name": "mailbox_read",
                                "arguments": {"id": "m0001", "run_id": "run-mcp", "role": "builder"},
                            },
                        }
                    ),
                    "",
                ]
            )
            proc = subprocess.run(
                ["python3", str(MCP)],
                input=payload,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertIn("sent m0001", rows[0]["result"]["content"][0]["text"])
        self.assertIn("run=run-mcp", rows[1]["result"]["content"][0]["text"])
        self.assertIn("no message m0001", rows[2]["result"]["content"][0]["text"])
        self.assertIn('"role": "builder"', rows[3]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
