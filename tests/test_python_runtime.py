from __future__ import annotations

import contextlib
import io
import unittest

from agent_bridge.python_runtime import require_supported_python


class PythonRuntimeTests(unittest.TestCase):
    def test_accepts_minimum_and_future_versions(self) -> None:
        require_supported_python("fixture", version_info=(3, 11, 0))
        require_supported_python("fixture", version_info=(3, 99, 0))

    def test_rejects_old_version_with_actionable_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            require_supported_python("mailbox MCP", version_info=(3, 10, 14))

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("mailbox MCP: Python 3.11 or newer is required (found 3.10.14)", stderr.getvalue())
        self.assertIn("AGENT_BRIDGE_PYTHON", stderr.getvalue())
