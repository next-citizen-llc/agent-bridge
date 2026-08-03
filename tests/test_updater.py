from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_bridge import updater


# Git refuses to commit without an identity; no assertion depends on the value.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Agent Bridge Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Agent Bridge Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(cwd), *args],
        env=GIT_ENV,
        stderr=subprocess.STDOUT,
        text=True,
    ).strip()


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-b", "main", str(self.seed)], check=True, stdout=subprocess.DEVNULL)
        (self.seed / "bridge.txt").write_text("one\n", encoding="utf-8")
        git(self.seed, "add", "bridge.txt")
        git(self.seed, "commit", "-m", "Initial bridge")
        git(self.seed, "remote", "add", "origin", str(self.remote))
        git(self.seed, "push", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", str(self.remote), str(self.repo)], check=True, stdout=subprocess.DEVNULL)
        git(self.repo, "config", "user.name", "Agent Bridge Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        self.env = mock.patch.dict(
            os.environ,
            {"AGENT_BRIDGE_STATE_DIR": str(self.root / "state")},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def update(self, **kwargs: object) -> dict[str, object]:
        return updater.update_bridge(
            self.repo,
            expected_remote=str(self.remote),
            timeout=5,
            **kwargs,
        )

    def deploy_current(self) -> list[str]:
        refreshed: list[str] = []
        result = self.update(
            action="apply",
            force=True,
            refresh=lambda _repo, _timeout: (refreshed.append("refresh") is None, "test refresh"),
        )
        self.assertEqual(result["status"], "current")
        self.assertTrue(result["installed"])
        return refreshed

    def push_change(self, text: str = "two\n") -> str:
        (self.seed / "bridge.txt").write_text(text, encoding="utf-8")
        git(self.seed, "add", "bridge.txt")
        git(self.seed, "commit", "-m", "Update bridge")
        git(self.seed, "push", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def test_current_revision_installs_once_then_uses_cache(self) -> None:
        refreshed = self.deploy_current()
        cached = self.update(
            action="apply",
            refresh=lambda _repo, _timeout: (refreshed.append("unexpected") is None, "unexpected"),
        )
        self.assertEqual(refreshed, ["refresh"])
        self.assertEqual(cached["status"], "current_cached")
        self.assertFalse(cached["changed"])

    def test_remote_fast_forward_refreshes_and_marks_reexec(self) -> None:
        self.deploy_current()
        remote_revision = self.push_change()
        refreshed: list[str] = []
        result = self.update(
            action="apply",
            force=True,
            refresh=lambda _repo, _timeout: (refreshed.append("refresh") is None, "test refresh"),
        )
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["changed"])
        self.assertTrue(result["reexec_required"])
        self.assertEqual(refreshed, ["refresh"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), remote_revision)

    def test_check_reports_update_without_mutating_checkout(self) -> None:
        local_revision = git(self.repo, "rev-parse", "HEAD")
        self.push_change()
        result = self.update(action="check", force=True)
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), local_revision)

    def test_dirty_checkout_is_not_hidden_by_fresh_cache(self) -> None:
        self.deploy_current()
        (self.repo / "bridge.txt").write_text("local edit\n", encoding="utf-8")
        result = self.update(action="apply")
        self.assertEqual(result["status"], "blocked_dirty")
        self.assertEqual((self.repo / "bridge.txt").read_text(encoding="utf-8"), "local edit\n")

    def test_non_main_and_wrong_remote_are_blocked(self) -> None:
        git(self.repo, "checkout", "-b", "local-work")
        branch = self.update(action="apply", force=True)
        self.assertEqual(branch["status"], "blocked_branch")

        git(self.repo, "checkout", "main")
        wrong = self.root / "wrong.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(wrong)], check=True, stdout=subprocess.DEVNULL)
        git(self.repo, "remote", "set-url", "origin", str(wrong))
        remote = self.update(action="apply", force=True)
        self.assertEqual(remote["status"], "blocked_remote")

    def test_ahead_and_diverged_histories_are_never_rewritten(self) -> None:
        self.deploy_current()
        (self.repo / "local.txt").write_text("ahead\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "Local work")
        ahead_revision = git(self.repo, "rev-parse", "HEAD")
        ahead = self.update(action="apply", force=True)
        self.assertEqual(ahead["status"], "blocked_ahead")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), ahead_revision)

        self.push_change("remote divergence\n")
        diverged = self.update(action="apply", force=True)
        self.assertEqual(diverged["status"], "blocked_diverged")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), ahead_revision)

    def test_offline_start_uses_installed_revision_and_caches_failure(self) -> None:
        self.deploy_current()
        self.remote.rename(self.root / "remote-offline.git")
        first = self.update(action="apply", force=True)
        second = self.update(action="apply")
        self.assertEqual(first["status"], "offline")
        self.assertEqual(second["status"], "offline_cached")
        self.assertEqual(first["deployed_revision"], git(self.repo, "rev-parse", "HEAD"))

    def test_live_lock_and_failed_build_leave_checkout_safe(self) -> None:
        lock = self.root / "state" / "update" / "update.lock"
        lock.mkdir(parents=True)
        busy = self.update(action="apply", force=True)
        self.assertEqual(busy["status"], "busy")
        lock.rmdir()

        remote_revision = self.push_change()
        failed = self.update(
            action="apply",
            force=True,
            refresh=lambda _repo, _timeout: (False, "test build failed"),
        )
        self.assertEqual(failed["status"], "build_failed")
        self.assertTrue(failed["reexec_required"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), remote_revision)
        self.assertNotEqual(failed["deployed_revision"], remote_revision)


if __name__ == "__main__":
    unittest.main()
