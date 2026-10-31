"""Settings controls through a headless Textual pilot."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, Static

from bandctl.rules import AutoRule
from bandctl.settings_screen import SettingsScreen
from test_group_screen import group_app


class SettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_preserves_leading_spaces_and_rejects_blank_path(self):
        app, shaper, host = group_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("x")
            path = app.screen.query_one("#settings-path", Input)
            with patch("bandctl.settings_screen.export_session") as export:
                path.value = "  settings.json"
                await pilot.click("#settings-export")
                self.assertEqual(export.call_args.args[0], "  settings.json")
                path.value = "   "
                app.screen.on_button_pressed(Button.Pressed(app.screen.query_one("#settings-export", Button)))
                export.assert_called_once()
                self.assertIn("Enter a file path", str(app.screen.query_one("#settings-status", Static).render()))

    async def test_settings_error_replaces_control_characters(self):
        app, _shaper, _host = group_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("x")
            app.screen.query_one("#settings-path", Input).value = "settings.json"
            with patch("bandctl.settings_screen.export_session", side_effect=ValueError("bad\npath\x1b\u0085")):
                await pilot.click("#settings-export")
            status = str(app.screen.query_one("#settings-status", Static).render())
            self.assertIn("bad?path??", status)
            self.assertNotIn("\x1b", status)

    async def test_import_and_export_preserve_spaces_in_file_path(self):
        app, shaper, host = group_app()
        app._rules = [AutoRule("work", 20, None)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json "
            other = Path(directory) / "settings.json"
            other.write_text("keep this file")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.press("x")
                app.screen.query_one("#settings-path", Input).value = str(path)
                await pilot.click("#settings-export")
                self.assertEqual(other.read_text(), "keep this file")
                self.assertTrue(path.is_file())
                await pilot.press("escape")
                app._rules = []
                await pilot.press("x")
                app.screen.query_one("#settings-path", Input).value = str(path)
                await pilot.click("#settings-import")
                await pilot.pause()
                self.assertEqual(app._rules, [AutoRule("work", 20, None)])

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


class StartupSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_startup_budget_is_rejected_without_changing_state(self):
        import json
        from bandctl.settings import Settings, dumps

        app, shaper, host = group_app()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            data = json.loads(dumps(Settings("eth0", None, None, (), ())))
            data["down_budget_bps"] = 10 ** 400
            path.write_text(json.dumps(data))
            app._startup_settings = str(path)
            async with app.run_test(size=(80, 24)):
                self.assertIsNone(app._down_budget)
                self.assertIn("budget", str(app.query_one("#status", Static).render()))

    async def test_explicit_startup_import_sets_rules_and_budgets(self):
        from bandctl.settings import Settings, write_settings
        app, shaper, host = group_app()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            write_settings(path, Settings("eth0", 80_000_000, None, (AutoRule("work", 20, None),), ()))
            app._startup_settings = str(path)
            async with app.run_test(size=(80, 24)):
                self.assertEqual(app._down_budget, 80_000_000)
                self.assertEqual(app._rules, [AutoRule("work", 20, None)])

    async def test_invalid_startup_import_does_not_change_state(self):
        app, shaper, host = group_app()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("bad")
            app._startup_settings = str(path)
            async with app.run_test(size=(80, 24)):
                self.assertEqual(app._rules, [])
                self.assertIsNone(app._down_budget)
                self.assertIn("settings import", str(app.query_one("#status", Static).render()))
