"""Group editor through a fake host and the Textual pilot."""
import unittest

from textual.widgets import DataTable, Input, Static

from bandctl.app import BandctlApp
from bandctl.group_screen import GroupsScreen
from bandctl.procscan import ProcessInfo
from test_shaper import _shaper


def group_app():
    shaper, host = _shaper()
    for pid in (42, 43):
        host.files[f"/proc/{pid}/stat"] = f"{pid} (work) S " + "0 " * 18 + "100"
    processes = [ProcessInfo(42, "work", "work", "user", 1, 100),
                 ProcessInfo(43, "game", "game", "user", 1, 100)]
    app = BandctlApp(iface="eth0", shaper=shaper, processes=lambda: processes,
                     link_bps=100_000_000, is_root=True, rates=lambda: {})
    return app, shaper, host


class GroupScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_update_and_add_member(self):
        app, shaper, host = group_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("g")
            self.assertIsInstance(app.screen, GroupsScreen)
            app.screen.query_one("#group-name", Input).value = "work"
            app.screen.query_one("#group-pids", Input).value = "42"
            app.screen.query_one("#group-down", Input).value = "30"
            await pilot.click("#group-create")
            self.assertEqual(shaper.shares[42].group, "work")
            app.screen.query_one("#group-name", Input).value = ""
            app.screen.query_one("#group-table", DataTable).focus()
            await pilot.press("enter")
            self.assertEqual(app.screen.query_one("#group-name", Input).value, "work")
            app.screen.query_one("#group-pids", Input).value = "43"
            await pilot.click("#group-add")
            self.assertEqual(shaper.shares[42].minor, shaper.shares[43].minor)
            app.screen.query_one("#group-down", Input).value = "25"
            await pilot.click("#group-update")
            self.assertEqual([shaper.shares[pid].down_pct for pid in (42, 43)], [25, 25])
            self.assertEqual(app.screen.query_one("#group-table", DataTable).row_count, 1)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, GroupsScreen)
            self.assertIn("group work", str(app.query_one("#detail-title", Static).render()))

    async def test_invalid_pid_does_not_create_group(self):
        app, shaper, host = group_app()
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.press("g")
            app.screen.query_one("#group-name", Input).value = "work"
            app.screen.query_one("#group-pids", Input).value = "42, 99"
            app.screen.query_one("#group-down", Input).value = "30"
            await pilot.click("#group-create")
            self.assertFalse(shaper.shares)
            self.assertFalse(app.screen.changed)
            self.assertIn("cannot verify pid 99", str(app.screen.query_one("#group-status", Static).render()))

    async def test_group_counts_once_for_overcommit(self):
        app, shaper, host = group_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("g")
            app.screen.query_one("#group-name", Input).value = "work"
            app.screen.query_one("#group-pids", Input).value = "42, 43"
            app.screen.query_one("#group-down", Input).value = "60"
            await pilot.click("#group-create")
            await pilot.press("escape")
            self.assertIsNone(app._over_commit())
