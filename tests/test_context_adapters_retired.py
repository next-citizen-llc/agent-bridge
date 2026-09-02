from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge.context_adapters import context_status


class RetiredContextManifestTest(unittest.TestCase):
    def _manifest(self, directory: Path, *, retired_on: str | None, adapters: list) -> Path:
        module = directory / "module.md"
        module.write_text("## Retired notice\n\nbody\n", encoding="utf-8")
        data = {
            "schema_version": "1.0",
            "modules": [{"id": "notice", "path": str(module)}],
            "adapters": adapters,
        }
        if retired_on:
            data["retired_on"] = retired_on
        manifest = directory / "context-manifest.json"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        return manifest

    def test_retired_manifest_with_no_adapters_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = self._manifest(Path(raw), retired_on="2026-09-02", adapters=[])
            status = context_status(manifest)
        self.assertTrue(status["ok"])
        self.assertEqual(status["retired_on"], "2026-09-02")
        self.assertEqual(status["adapters"], [])

    def test_unretired_manifest_with_no_adapters_still_needs_attention(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manifest = self._manifest(Path(raw), retired_on=None, adapters=[])
            status = context_status(manifest)
        self.assertFalse(status["ok"])
        self.assertEqual(status["retired_on"], "")


if __name__ == "__main__":
    unittest.main()
