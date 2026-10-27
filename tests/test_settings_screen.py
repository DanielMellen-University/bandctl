"""Settings controls through a headless Textual pilot."""
import tempfile
import unittest
from pathlib import Path

from textual.widgets import Input, Static

from bandctl.rules import AutoRule
from bandctl.settings_screen import SettingsScreen
from test_group_screen import group_app


class SettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_then_import_rules_and_budget(self):
        app, shaper, host = group_app()
        app._rules = [AutoRule("work", 20, None)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            async with app.run_test(size=(80, 24)) as pilot:
                app._down_budget = 90_000_000
                app._write_budget_inputs()
                await pilot.press("x")
                self.assertIsInstance(app.screen, SettingsScreen)
                app.screen.query_one("#settings-path", Input).value = str(path)
                await pilot.click("#settings-export")
                self.assertTrue(path.is_file())
                await pilot.press("escape")
                app._rules = []
                app._down_budget = 40_000_000
                app._write_budget_inputs()
                await pilot.press("x")
                app.screen.query_one("#settings-path", Input).value = str(path)
                await pilot.click("#settings-import")
                await pilot.pause()
                self.assertEqual(app._rules, [AutoRule("work", 20, None)])
                self.assertEqual(app._down_budget, 90_000_000)

    async def test_invalid_import_leaves_session_unchanged(self):
        app, shaper, host = group_app()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"version": 1}')
            async with app.run_test(size=(60, 20)) as pilot:
                app._rules = [AutoRule("work", 20, None)]
                await pilot.press("x")
                app.screen.query_one("#settings-path", Input).value = str(path)
                await pilot.click("#settings-import")
                self.assertIsInstance(app.screen, SettingsScreen)
                self.assertIn("invalid", str(app.screen.query_one("#settings-status", Static).render()))
                self.assertEqual(app._rules, [AutoRule("work", 20, None)])
