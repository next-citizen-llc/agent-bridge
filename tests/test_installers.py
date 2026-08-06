from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
AGENT = ROOT / "bin" / "agent"
INSTALL_PS1 = ROOT / "scripts" / "install.ps1"
UNINSTALL_PS1 = ROOT / "scripts" / "uninstall.ps1"
WINDOWS_LAUNCHER_TEMPLATE = ROOT / "scripts" / "agent.cmd.template"


class InstallerTests(unittest.TestCase):
    def _run(self, script: Path, *args: str, home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=str(ROOT),
            env={**os.environ, "HOME": str(home)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_unix_install_is_launcher_only_idempotent_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / "custom-bin"
            home.mkdir()

            first = self._run(INSTALL, "--bin-dir", str(bin_dir), home=home)
            second = self._run(INSTALL, "--bin-dir", str(bin_dir), home=home)
            launcher = bin_dir / "agent"
            target = ROOT / "bin" / "agent"

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(os.readlink(launcher), str(target))
            self.assertIn("Startup hooks: not installed", first.stdout)
            self.assertIn("already installed", second.stdout)
            self.assertFalse((home / ".codex" / "hooks.json").exists())
            self.assertFalse((home / ".grok" / "hooks" / "agent-bridge.json").exists())

            removed = self._run(UNINSTALL, "--bin-dir", str(bin_dir), home=home)
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertFalse(launcher.exists())
            self.assertTrue((home / ".local" / "state" / "agent-bridge").exists())

    def test_unix_install_refuses_launcher_collision_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / "custom-bin"
            bin_dir.mkdir(parents=True)
            launcher = bin_dir / "agent"
            launcher.write_text("existing launcher\n", encoding="utf-8")

            refused = self._run(INSTALL, "--bin-dir", str(bin_dir), home=home)
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(launcher.read_text(encoding="utf-8"), "existing launcher\n")
            self.assertIn("refusing to replace", refused.stderr)

            forced = self._run(
                INSTALL,
                "--bin-dir",
                str(bin_dir),
                "--force",
                home=home,
            )
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertTrue(launcher.is_symlink())
            self.assertIn("explicit --force", forced.stdout)

    def test_unix_hook_opt_in_is_removed_with_custom_bin_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / "custom-bin"
            home.mkdir()

            installed = self._run(
                INSTALL,
                "--bin-dir",
                str(bin_dir),
                "--install-hooks",
                home=home,
            )
            hooks_path = home / ".codex" / "hooks.json"
            wrapper = home / ".local" / "bin" / "grok-gui-bridge"
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(hooks_path.exists())
            self.assertIn(str(bin_dir / "agent"), hooks_path.read_text(encoding="utf-8"))
            self.assertTrue(wrapper.exists())

            removed = self._run(
                UNINSTALL,
                "--bin-dir",
                str(bin_dir),
                "--remove-hooks",
                home=home,
            )
            self.assertEqual(removed.returncode, 0, removed.stderr)
            hooks = hooks_path.read_text(encoding="utf-8")
            self.assertNotIn(str(bin_dir / "agent"), hooks)
            self.assertFalse(wrapper.exists())
            self.assertFalse((bin_dir / "agent").exists())

    def test_unix_uninstall_preserves_nonmatching_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            bin_dir = home / "custom-bin"
            bin_dir.mkdir(parents=True)
            other = home / "other-agent"
            other.write_text("other\n", encoding="utf-8")
            launcher = bin_dir / "agent"
            launcher.symlink_to(other)

            result = self._run(UNINSTALL, "--bin-dir", str(bin_dir), home=home)
            self.assertEqual(result.returncode, 2)
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(os.readlink(launcher), str(other))
            self.assertIn("preserved non-matching launcher", result.stderr)

    def test_unix_launcher_scans_all_python3_matches_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_bin = root / "old-bin"
            supported_bin = root / "supported python bin"
            old_bin.mkdir()
            supported_bin.mkdir()
            log = root / "launcher.log"
            (old_bin / "python3").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
                "echo 'Python 3.9.18'\n",
                encoding="utf-8",
            )
            (supported_bin / "python3").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
                "printf '%s\\n' \"$@\" > \"$LAUNCHER_LOG\"\n",
                encoding="utf-8",
            )
            for interpreter in (*old_bin.iterdir(), *supported_bin.iterdir()):
                interpreter.chmod(0o755)

            result = subprocess.run(
                [str(AGENT), "code", "bridge", "--list"],
                cwd=str(ROOT),
                env={"PATH": f"{old_bin}:{supported_bin}:/usr/bin:/bin", "LAUNCHER_LOG": str(log)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines()[:3], ["-m", "agent_bridge.cli", "code"])

    def test_unix_launcher_accepts_future_versioned_python_in_path_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "future python bin"
            fake_bin.mkdir()
            log = root / "launcher.log"
            (fake_bin / "python3").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            (fake_bin / "python3.99").write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
                "printf '%s\\n' \"$@\" > \"$LAUNCHER_LOG\"\n",
                encoding="utf-8",
            )
            for interpreter in fake_bin.iterdir():
                interpreter.chmod(0o755)

            result = subprocess.run(
                [str(AGENT), "code", "bridge", "--list"],
                cwd=str(ROOT),
                env={"PATH": f"{fake_bin}:/usr/bin:/bin", "LAUNCHER_LOG": str(log)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").splitlines()[:2], ["-m", "agent_bridge.cli"])

    def test_unix_launcher_rejects_old_python_when_no_supported_version_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            old_python = fake_bin / "python3"
            old_python.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"-c\" ]; then exit 1; fi\n"
                "echo 'Python 3.9.18'\n",
                encoding="utf-8",
            )
            old_python.chmod(0o755)

            # Keep the negative fixture hermetic. GitHub's Ubuntu images expose
            # supported versioned interpreters under /usr/bin, which the
            # launcher correctly scans after the fake old python. Only the
            # shell utilities needed before interpreter selection belong on
            # this fixture's PATH.
            for utility in ("bash", "dirname"):
                target = shutil.which(utility)
                self.assertIsNotNone(target)
                (fake_bin / utility).symlink_to(target)

            result = subprocess.run(
                [str(AGENT), "code", "bridge", "--list"],
                cwd=str(ROOT),
                env={"PATH": str(fake_bin)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Python 3.11 or newer is required", result.stderr)
            self.assertIn("AGENT_BRIDGE_PYTHON", result.stderr)

    def test_windows_install_and_uninstall_share_one_launcher_template(self) -> None:
        install = INSTALL_PS1.read_text(encoding="utf-8")
        uninstall = UNINSTALL_PS1.read_text(encoding="utf-8")
        template = WINDOWS_LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
        shared_loader = '$LauncherTemplate = Join-Path $PSScriptRoot "agent.cmd.template"'
        shared_renderer = '.Replace("__AGENT_BRIDGE_PROJECT_DIR__", $ProjectDir)'

        self.assertIn(shared_loader, install)
        self.assertIn(shared_loader, uninstall)
        self.assertIn(shared_renderer, install)
        self.assertIn(shared_renderer, uninstall)
        self.assertNotIn('$cmd = @"', install)
        self.assertNotIn('$expected = @"', uninstall)
        self.assertEqual(template.count("__AGENT_BRIDGE_PROJECT_DIR__"), 1)

    def test_windows_launcher_uses_unbounded_validated_python_selection(self) -> None:
        template = WINDOWS_LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
        uninstall = UNINSTALL_PS1.read_text(encoding="utf-8")

        self.assertIn("py -3 -c", template)
        self.assertIn("py -3 -m agent_bridge.cli", template)
        self.assertNotRegex(template, r"py -3\.\d+")
        self.assertIn('"%AGENT_BRIDGE_PYTHON%" -c', template)
        self.assertIn("python -c", template)
        self.assertIn("if ($launcherPresent)", uninstall)
        self.assertIn("& $AgentCmd code hooks uninstall --client all", uninstall)
        self.assertIn("& py -3 -c", uninstall)
        self.assertIn("& python -c", uninstall)

    def test_windows_mcp_registration_validates_python_floor(self) -> None:
        install = INSTALL_PS1.read_text(encoding="utf-8")

        self.assertIn("$env:AGENT_BRIDGE_PYTHON", install)
        self.assertIn("sys.version_info >= (3, 11)", install)
        self.assertIn("Python 3.11 or newer is required for MCP registration", install)
        validation = install.index("sys.version_info >= (3, 11)")
        self.assertLess(validation, install.index("& claude mcp add"))
        self.assertLess(validation, install.index("& codex mcp add"))


if __name__ == "__main__":
    unittest.main()
