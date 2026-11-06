"""Verify additive email MCP overlay without touching a live UniSync installation."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE_FILE = REPO / "scripts" / "install_email_mcp_overlay.py"
spec = importlib.util.spec_from_file_location("overlay_installer", MODULE_FILE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestEmailMcpOverlay(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name)
        self.server = self.target / "backend/unisync/mcp/server.py"
        self.server.parent.mkdir(parents=True)
        self.server.write_text(
            'from pathlib import Path\nimport json\n\n'
            'class Stub:\n    def tool(self): return lambda f: f\n'
            'mcp = Stub()\n\n'
            '@mcp.tool()\n'
            'def unisync_get_overview(): return "{}"\n\n'
            '@mcp.tool()\n'
            'def unisync_search_course_materials(query): return "{}"\n')
        (self.target/"backend/unisync/services/email_monitor").mkdir(parents=True)

    def test_overlay_preserves_existing_tools_and_is_idempotent(self):
        original = self.server.read_text()
        plan = module.prepare(REPO, self.target)
        self.assertEqual(plan["prior_function_count"], 2)
        self.assertTrue(set(module.TOOL_NAMES).issubset(module.tool_names(plan["merged_server"])))
        self.assertEqual(self.server.read_text(), original)  # dry-run did not mutate
        result = module.install(plan)
        self.assertTrue(result["installed"])
        self.assertTrue(Path(result["server_backup"]).is_file())
        installed = self.server.read_text()
        self.assertIn("def unisync_get_overview(", installed)
        self.assertIn("def unisync_get_email(", installed)
        again = module.prepare(REPO, self.target)
        self.assertTrue(again["already_installed"])
        module.install(again)
        self.assertEqual(self.server.read_text(), installed)

    def test_reject_missing_server_without_touching_archive(self):
        self.server.unlink()
        with self.assertRaises(FileNotFoundError):
            module.prepare(REPO, self.target)
        self.assertFalse((self.target/"backend/unisync/services/email_monitor/archive_search.py").exists())


if __name__ == "__main__":
    unittest.main()
