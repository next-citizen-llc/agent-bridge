from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_bridge import managed_repos as mr


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


class ManagedRepoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "init", "-b", "main", str(self.seed)], check=True, stdout=subprocess.DEVNULL)
        (self.seed / "readme.md").write_text("one\n", encoding="utf-8")
        (self.seed / "canon").mkdir()
        (self.seed / "canon" / "fact.yaml").write_text("id: one\n", encoding="utf-8")
        git(self.seed, "add", ".")
        git(self.seed, "commit", "-m", "Initial")
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

    def entry(self, mode: str, *, path: Path | None = None, canonical: list[str] | None = None) -> dict[str, object]:
        return {
            "id": f"test-{mode}",
            "label": f"test {mode}",
            "path": str(path if path is not None else self.repo),
            "expected_remote": str(self.remote),
            "branch": "main",
            "mode": mode,
            "canonical_paths": ["canon"] if canonical is None else canonical,
        }

    def check(self, mode: str, **kwargs: object) -> dict[str, object]:
        return mr.check_managed_repo(self.entry(mode, **kwargs), timeout=5, interval_seconds=0, force=True)

    def push_change(self, text: str = "two\n") -> str:
        (self.seed / "readme.md").write_text(text, encoding="utf-8")
        git(self.seed, "add", "readme.md")
        git(self.seed, "commit", "-m", "Remote change")
        git(self.seed, "push", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def test_apply_mode_fast_forwards_a_clean_checkout(self) -> None:
        remote_revision = self.push_change()
        result = self.check("apply")
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["changed"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), remote_revision)

    def test_apply_mode_leaves_a_dirty_checkout_untouched(self) -> None:
        local_revision = git(self.repo, "rev-parse", "HEAD")
        self.push_change()
        (self.repo / "readme.md").write_text("local edit\n", encoding="utf-8")
        result = self.check("apply")
        self.assertEqual(result["status"], "blocked_dirty")
        # The staleness must survive the dirty block; a dirty tree that is also
        # behind is the exact state that stayed invisible before this check.
        self.assertEqual(result["behind"], 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), local_revision)
        self.assertEqual((self.repo / "readme.md").read_text(encoding="utf-8"), "local edit\n")
        self.assertIn("behind", mr.format_managed_repos([result]))

    def test_report_mode_never_mutates_even_when_behind(self) -> None:
        local_revision = git(self.repo, "rev-parse", "HEAD")
        self.push_change()
        result = self.check("report")
        self.assertEqual(result["status"], "behind")
        self.assertEqual(result["behind"], 1)
        self.assertFalse(result["changed"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), local_revision)

    def test_uncommitted_canonical_work_is_reported_when_otherwise_current(self) -> None:
        # The failure mode a sync check alone cannot see: canonical facts that
        # exist only locally and are on no remote.
        (self.repo / "canon" / "fact.yaml").write_text("id: one\nextra: local\n", encoding="utf-8")
        (self.repo / "canon" / "new.yaml").write_text("id: two\n", encoding="utf-8")
        result = self.check("report")
        self.assertEqual(result["status"], "uncommitted_canonical")
        self.assertEqual(len(result["uncommitted_canonical"]), 2)

    def test_dirty_outside_canonical_paths_does_not_raise_canonical_alarm(self) -> None:
        (self.repo / "scratch.txt").write_text("noise\n", encoding="utf-8")
        result = self.check("report")
        self.assertEqual(result["status"], "dirty")
        self.assertEqual(result["uncommitted_canonical"], [])

    def test_ahead_and_diverged_are_reported_without_rewriting(self) -> None:
        (self.repo / "local.txt").write_text("ahead\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "Local work")
        ahead_revision = git(self.repo, "rev-parse", "HEAD")
        ahead = self.check("apply")
        self.assertEqual(ahead["status"], "blocked_ahead")
        self.assertEqual(ahead["ahead"], 1)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), ahead_revision)

        self.push_change("divergence\n")
        diverged = self.check("apply")
        self.assertEqual(diverged["status"], "blocked_diverged")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), ahead_revision)

    def test_absent_checkout_is_not_an_error(self) -> None:
        result = self.check("report", path=self.root / "nope")
        self.assertEqual(result["status"], "absent")

    def test_non_git_path_is_flagged(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        result = self.check("report", path=plain)
        self.assertEqual(result["status"], "not_git")

    def test_wrong_remote_is_blocked_in_report_mode(self) -> None:
        wrong = self.root / "wrong.git"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(wrong)], check=True, stdout=subprocess.DEVNULL)
        git(self.repo, "remote", "set-url", "origin", str(wrong))
        result = self.check("report")
        self.assertEqual(result["status"], "blocked_remote")

    def test_warning_state_is_never_served_from_cache(self) -> None:
        self.push_change()
        first = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=True)
        self.assertEqual(first["status"], "behind")
        # A fresh cache must not hide it on the next session.
        second = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=False)
        self.assertEqual(second["status"], "behind")
        self.assertNotEqual(second.get("cached"), True)

    def test_current_state_is_served_from_cache(self) -> None:
        first = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=True)
        self.assertEqual(first["status"], "current")
        second = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=False)
        self.assertEqual(second["status"], "current")
        self.assertTrue(second.get("cached"))

    def test_offline_remote_degrades_without_error(self) -> None:
        self.remote.rename(self.root / "remote-offline.git")
        result = self.check("report")
        self.assertEqual(result["status"], "offline")

    def test_unverified_repo_never_reads_as_current(self) -> None:
        # An unchecked repo is not a clean repo. offline/busy/disabled must be
        # reported, or a transient failure silently passes as "all current".
        self.remote.rename(self.root / "remote-offline.git")
        result = self.check("report")
        text = mr.format_managed_repos([result])
        self.assertNotIn("all current", text)
        self.assertIn("ATTENTION", text)
        self.assertIn("unverified", text)

    def test_busy_lock_is_reported_rather_than_passing_as_clean(self) -> None:
        from agent_bridge import updater

        lock = updater._lock_dir("test-apply")
        lock.mkdir(parents=True)
        try:
            result = self.check("apply")
        finally:
            lock.rmdir()
        self.assertEqual(result["status"], "busy")
        text = mr.format_managed_repos([result])
        self.assertNotIn("all current", text)
        self.assertIn("unverified", text)

    def test_unverified_state_is_never_served_from_cache(self) -> None:
        self.remote.rename(self.root / "remote-offline.git")
        first = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=True)
        self.assertEqual(first["status"], "offline")
        second = mr.check_managed_repo(self.entry("report"), timeout=5, interval_seconds=3600, force=False)
        self.assertNotEqual(second.get("cached"), True)

    def test_format_surfaces_every_notable_fact_not_just_the_worst(self) -> None:
        # Ahead AND holding uncommitted canonical work: both must appear, or the
        # higher-severity status silently hides the other.
        (self.repo / "local.txt").write_text("ahead\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "Local work")
        (self.repo / "canon" / "fact.yaml").write_text("id: one\nextra: local\n", encoding="utf-8")
        result = self.check("report")
        text = mr.format_managed_repos([result])
        self.assertIn("AHEAD", text)
        self.assertIn("UNCOMMITTED canonical", text)
        self.assertIn("ATTENTION", text)

    def test_format_is_quiet_when_everything_is_current(self) -> None:
        result = self.check("report")
        self.assertEqual(result["status"], "current")
        text = mr.format_managed_repos([result])
        self.assertIn("all current", text)
        self.assertNotIn("ATTENTION", text)

    def test_absent_repos_are_omitted_from_the_startup_line(self) -> None:
        absent = self.check("report", path=self.root / "nope")
        self.assertEqual(mr.format_managed_repos([absent]), "")

    def test_disable_env_short_circuits_the_sweep(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_DISABLE_MANAGED_REPOS": "1"}, clear=False):
            self.assertEqual(mr.sync_managed_repos(force=True), [])

    def test_registry_ships_empty_so_the_tool_embeds_no_repo_identities(self) -> None:
        # Which repos a machine tracks is local operator configuration. Agent
        # Bridge must not carry any repo name, clone URL, or filesystem path.
        self.assertEqual(mr.DEFAULT_REGISTRY, [])
        self.assertEqual(mr.load_registry(), [])
        self.assertEqual(mr.sync_managed_repos(force=True), [])

    def test_registry_config_file_declares_repos(self) -> None:
        config = mr.config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"repos": [{"id": "only-one", "path": "/tmp/x", "mode": "report"}]}', encoding="utf-8")
        registry = mr.load_registry()
        self.assertEqual([entry["id"] for entry in registry], ["only-one"])
        self.assertEqual(registry[0]["branch"], "main")
        self.assertEqual(registry[0]["label"], "only-one")

    def test_bare_list_config_is_accepted(self) -> None:
        config = mr.config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[{"id": "bare", "path": "/tmp/y", "mode": "apply"}]', encoding="utf-8")
        self.assertEqual([entry["id"] for entry in mr.load_registry()], ["bare"])

    def test_env_path_override_wins_for_a_registry_entry(self) -> None:
        config = mr.config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"repos": [{"id": "example-docs", "path": "/tmp/x", "mode": "report"}]}', encoding="utf-8")
        with mock.patch.dict(os.environ, {"AGENT_BRIDGE_MANAGED_EXAMPLE_DOCS_PATH": "/tmp/elsewhere"}, clear=False):
            registry = {entry["id"]: entry for entry in mr.load_registry()}
        self.assertEqual(registry["example-docs"]["path"], "/tmp/elsewhere")

    def test_config_example_is_valid_registry_input(self) -> None:
        config = mr.config_path()
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(mr.CONFIG_EXAMPLE), encoding="utf-8")
        registry = mr.load_registry()
        self.assertEqual(len(registry), 1)
        self.assertIn(registry[0]["mode"], {"apply", "report"})

    def test_apply_and_report_repos_use_separate_state_files(self) -> None:
        self.check("apply")
        self.check("report")
        self.assertTrue(mr.managed_state_path("test-apply").exists())
        self.assertTrue(mr.managed_state_path("test-report").exists())
        self.assertNotEqual(mr.managed_state_path("test-apply"), mr.managed_state_path("test-report"))


if __name__ == "__main__":
    unittest.main()
