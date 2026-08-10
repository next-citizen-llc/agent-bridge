import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_bridge import cli
from agent_bridge.skill_retirement import purge_retired_skills


class SkillRetirementTests(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        path = root / "retired.json"
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "retirements": [{
                    "id": "anthropic-skills:jobapp",
                    "root": "codex_plugin_cache",
                    "path": ["claude-cowork", "anthropic-skills", "*", "skills", "jobapp"],
                    "skill_name": "jobapp",
                }],
            }),
            encoding="utf-8",
        )
        return path

    def test_purges_only_matching_manifest_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            jobapp = home / "plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/jobapp"
            other = home / "plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/pdf"
            jobapp.mkdir(parents=True)
            other.mkdir(parents=True)
            (jobapp / "SKILL.md").write_text("---\nname: jobapp\n---\n", encoding="utf-8")
            (other / "SKILL.md").write_text("---\nname: pdf\n---\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                report = purge_retired_skills(manifest_path=self._manifest(Path(tmp)))
            self.assertEqual(report["status"], "ok")
            self.assertFalse(jobapp.exists())
            self.assertTrue(other.exists())

    def test_name_mismatch_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            jobapp = home / "plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/jobapp"
            jobapp.mkdir(parents=True)
            (jobapp / "SKILL.md").write_text("---\nname: not-jobapp\n---\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                report = purge_retired_skills(manifest_path=self._manifest(Path(tmp)))
            self.assertEqual(report["status"], "degraded")
            self.assertTrue(jobapp.exists())

    def test_empty_retired_skill_shell_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            jobapp = home / "plugins/cache/claude-cowork/anthropic-skills/1.0.0/skills/jobapp"
            (jobapp / "references").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False):
                report = purge_retired_skills(manifest_path=self._manifest(Path(tmp)))
            self.assertEqual(report["status"], "ok")
            self.assertFalse(jobapp.exists())

    def test_disable_override_leaves_skill_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"AGENT_BRIDGE_DISABLE_SKILL_PURGE": "1"}, clear=False):
                report = purge_retired_skills(manifest_path=self._manifest(Path(tmp)))
            self.assertEqual(report["status"], "disabled")

    def test_session_start_runs_retirement_before_emitting_context(self) -> None:
        purge = {"status": "ok", "purged": [{"id": "old", "path": "/tmp/old"}], "errors": []}
        with (
            mock.patch.object(cli, "purge_retired_skills", return_value=purge) as retire,
            mock.patch.object(cli, "maybe_register_harness", return_value=None),
            mock.patch.object(cli, "run_preflight", return_value=None),
            mock.patch.object(cli, "load_update_state", return_value={}),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = cli.hook_session_start(
                [
                    "--client",
                    "codex",
                    "--surface",
                    "cli",
                    "--skip-update",
                    "--skip-managed-repos",
                    "--skip-pointer-sync",
                    "--plain",
                ]
            )
        self.assertEqual(result, 0)
        retire.assert_called_once_with()
        self.assertIn("Deprecated-skill purge removed 1 installation(s).", output.getvalue())


if __name__ == "__main__":
    unittest.main()
