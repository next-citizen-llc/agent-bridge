from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"


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


if __name__ == "__main__":
    unittest.main()
