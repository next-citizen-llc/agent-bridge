from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_bridge import cli, optimization
from agent_bridge.cli import AgentRunResult
from agent_bridge.optimization import SAFE_CACHE_CLASSES


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "bin" / "agent"


class BridgeReviewCacheTests(unittest.TestCase):
    """An identical review may be served from cache; a code dispatch never may.

    A cached `code` dispatch would print a prior run's output and report success
    without touching the worktree, so the boundary is enforced in two places: the
    wrapper only caches `review`, and `SAFE_CACHE_CLASSES` has no `code` member.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_path = Path(tmp.name) / "exact.json"
        self.agent = {
            "id": "claude",
            "label": "Claude Code",
            "adapter": "claude_code",
            "command": "claude",
        }
        self.dispatched: list[str] = []

    def _fake_dispatch(self, agent: dict, **kwargs) -> AgentRunResult:
        self.dispatched.append(kwargs["mode"])
        return AgentRunResult(return_code=0, output=f"body for {kwargs['prompt']}")

    def _invoke(self, *, mode: str, prompt: str, cache_mode: str = "exact") -> AgentRunResult:
        with mock.patch.object(cli, "_invoke_target_dispatch", self._fake_dispatch), mock.patch.object(
            optimization, "exact_cache_path", return_value=self.cache_path
        ):
            return cli._invoke_target_once(
                self.agent,
                source="human",
                mode=mode,
                prompt=prompt,
                budget_usd="0.50",
                dry_run=False,
                meta={"run_id": "run_test"},
                cache_mode=cache_mode,
            )

    def test_identical_review_is_served_from_cache(self) -> None:
        first = self._invoke(mode="review", prompt="review the readiness layer")
        second = self._invoke(mode="review", prompt="review the readiness layer")

        self.assertEqual(self.dispatched, ["review"], "second identical review should not re-dispatch")
        self.assertEqual(second.output, first.output)
        self.assertEqual(second.return_code, 0)

    def test_different_prompt_misses(self) -> None:
        self._invoke(mode="review", prompt="review the readiness layer")
        self._invoke(mode="review", prompt="review the mailbox layer")

        self.assertEqual(self.dispatched, ["review", "review"])

    def test_code_dispatch_is_never_cached(self) -> None:
        self._invoke(mode="code", prompt="apply the rename")
        self._invoke(mode="code", prompt="apply the rename")

        self.assertEqual(self.dispatched, ["code", "code"], "a code dispatch must always run")

    def test_cache_mode_off_never_caches(self) -> None:
        self._invoke(mode="review", prompt="same prompt", cache_mode="off")
        self._invoke(mode="review", prompt="same prompt", cache_mode="off")

        self.assertEqual(self.dispatched, ["review", "review"])

    def test_dry_run_is_not_cached(self) -> None:
        with mock.patch.object(cli, "_invoke_target_dispatch", self._fake_dispatch), mock.patch.object(
            optimization, "exact_cache_path", return_value=self.cache_path
        ):
            for _ in range(2):
                cli._invoke_target_once(
                    self.agent,
                    source="human",
                    mode="review",
                    prompt="same prompt",
                    budget_usd="0.50",
                    dry_run=True,
                    meta={"run_id": "run_test"},
                    cache_mode="exact",
                )

        self.assertEqual(self.dispatched, ["review", "review"])

    def test_code_is_not_a_safe_cache_class(self) -> None:
        self.assertIn("review", SAFE_CACHE_CLASSES)
        self.assertNotIn("code", SAFE_CACHE_CLASSES)

    def test_cli_rejects_exact_cache_for_code_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "AGENT_BRIDGE_STATE_DIR": str(Path(tmp) / "state"), "CLAUDE_BIN": sys.executable}
            proc = subprocess.run(
                [
                    str(AGENT), "code", "bridge",
                    "--from", "human",
                    "--to", "claude",
                    "--mode", "code",
                    "--prompt", "apply the rename",
                    "--cache-mode", "exact",
                    "--dry-run",
                    "--no-preflight",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("--mode review only", proc.stdout)


if __name__ == "__main__":
    unittest.main()
