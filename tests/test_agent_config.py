from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class AgentConfigTests(unittest.TestCase):
    def test_default_config_includes_local_coding_targets(self) -> None:
        config = json.loads((ROOT / "agent_bridge" / "agents.json").read_text(encoding="utf-8"))
        agents = {agent["id"]: agent for agent in config["agents"]}

        self.assertIn("grok", agents)
        self.assertEqual(agents["grok"]["adapter"], "argv")
        self.assertEqual(agents["grok"]["env_command"], "GROK_BIN")
        self.assertIn("review_args", agents["grok"])
        self.assertIn("code_args", agents["grok"])

        self.assertIn("agy", agents)
        self.assertEqual(agents["agy"]["adapter"], "argv")
        self.assertEqual(agents["agy"]["env_command"], "AGY_BIN")
        self.assertIn("review_args", agents["agy"])
        self.assertIn("code_args", agents["agy"])

    def test_list_shows_local_coding_targets(self) -> None:
        proc = subprocess.run(
            [str(AGENT), "code", "bridge", "--list"],
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("grok (Grok Build)", proc.stdout)
        self.assertIn("agy (Anti-Gravity)", proc.stdout)

    def test_grok_dry_run_renders_headless_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"),
                "GROK_BIN": sys.executable,
            }
            proc = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "grok",
                    "--mode",
                    "review",
                    "--prompt",
                    "review smoke",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[dry-run] grok:", proc.stdout)
        self.assertIn("--cwd", proc.stdout)
        self.assertIn("--permission-mode auto", proc.stdout)
        self.assertIn("--output-format plain", proc.stdout)
        self.assertIn("--disable-web-search", proc.stdout)
        self.assertIn("--max-turns 16", proc.stdout)

    def test_agy_dry_run_renders_review_and_code_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"),
                "AGY_BIN": sys.executable,
            }
            review = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "agy",
                    "--mode",
                    "review",
                    "--prompt",
                    "review smoke",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            code = subprocess.run(
                [
                    str(AGENT),
                    "code",
                    "bridge",
                    "--from",
                    "human",
                    "--to",
                    "agy",
                    "--mode",
                    "code",
                    "--prompt",
                    "code smoke",
                    "--dry-run",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertIn("[dry-run] agy:", review.stdout)
        self.assertIn("--print", review.stdout)
        self.assertIn("--add-dir", review.stdout)
        self.assertIn("--sandbox", review.stdout)

        self.assertEqual(code.returncode, 0, code.stderr)
        self.assertIn("[dry-run] agy:", code.stdout)
        self.assertIn("--dangerously-skip-permissions", code.stdout)
        self.assertIn("--print-timeout 20m", code.stdout)


if __name__ == "__main__":
    unittest.main()
