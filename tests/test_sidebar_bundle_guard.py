from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SidebarBundlePlatformGuardTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "PowerShell guard runs on Windows")
    def test_windows_import_refuses_macos_bundle_before_writing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundle = base / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "kind": "codex_sidebar_state_bundle",
                        "platform": "macos",
                        "source_codex_home": "/Users/tts/.codex",
                    }
                ),
                encoding="utf-8",
            )
            target = base / "target"
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-File",
                    str(ROOT / "scripts" / "codex-sidebar-sync.ps1"),
                    "import",
                    "-From",
                    str(bundle),
                    "-CodexHome",
                    str(target),
                    "-Yes",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing macos bundle import into Windows", result.stdout + result.stderr)
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "POSIX guard runs in Linux/macOS CI")
    def test_posix_import_refuses_windows_bundle_before_tool_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bundle = base / "bundle"
            bundle.mkdir()
            (bundle / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "kind": "codex_sidebar_state_bundle",
                        "platform": "windows",
                        "source_codex_home": r"C:\\Users\\thist\\.codex",
                    }
                ),
                encoding="utf-8",
            )
            target = base / "target"
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "codex-sidebar-sync.sh"),
                    "import",
                    "--from",
                    str(bundle),
                    "--codex-home",
                    str(target),
                    "--yes",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing windows bundle import", result.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
