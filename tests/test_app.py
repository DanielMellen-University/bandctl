"""Headless Textual pilot for the main screen. No root and no tc."""

from __future__ import annotations

import unittest

from rich.text import Text
from textual.widgets import DataTable, Input
from textual.widgets._footer import FooterKey

from bandctl.app import BandctlApp, fit_columns, _COL_PAD
from bandctl.procscan import ProcessInfo
from bandctl.rates import Unit
from bandctl.shaper import Share, ShaperError
from bandctl.widgets import RootRequired


PROCS = [
    ProcessInfo(1, "systemd", "/sbin/systemd", "root", 4),
    ProcessInfo(42, "curl", "curl https://example.com", "daniel", 2),
    ProcessInfo(7, "bash", "bash", "daniel", 0),
]


class FakeShaper:
    def __init__(self) -> None:
        self.applied: list[tuple] = []
        self.removed: list[int] = []
        self.caps: list[tuple] = []
        self._shares: dict[int, Share] = {}
        self.torn = False

    def apply(self, pid, down_pct, up_pct, down_cap, up_cap, command=""):
        if down_pct and not down_cap:
            raise ShaperError("download capacity is unknown; enter a download budget")
        if up_pct and not up_cap:
            raise ShaperError("upload capacity is unknown; enter an upload budget")
        self.applied.append((pid, down_pct, up_pct, down_cap, up_cap, command))
        self._shares[pid] = Share(pid, down_pct, up_pct, 10, "/", command)
        down = "unlimited" if down_pct is None else f"{down_pct}%"
        up = "unlimited" if up_pct is None else f"{up_pct}%"
        return f"pid {pid} download {down}, upload {up}"

    def remove(self, pid):
        if pid not in self._shares:
            raise ShaperError(f"pid {pid} has no limit")
        self._shares.pop(pid)
        self.removed.append(pid)
        return f"removed limits on pid {pid}"

    def set_capacities(self, down, up):
        self.caps.append((down, up))

    def sample(self):
        return {pid: (30_000_000.0, 10_000_000.0) for pid in self._shares}

    def teardown(self):
        self.torn = True
        return []

    @property
    def shares(self):
        return dict(self._shares)


def _rates():
    # 100 Mbit/s link. curl is 40% down. systemd is 0.1%. bash is idle.
    return {
        42: (40_000_000.0, 5_000_000.0),
        1: (100_000.0, 0.0),
        7: (0.0, 0.0),
    }


def _app(fake, *, root=True, link_bps=100_000_000, rates=None):
    return BandctlApp(
        iface="eth0",
        shaper=fake,
        processes=lambda: list(PROCS),
        link_bps=link_bps,
        is_root=root,
        rates=_rates if rates is None else rates,
    )


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_keys_apply_and_resize(self):
        fake = FakeShaper()
        app = _app(fake)
        async with app.run_test(size=(120, 36)) as pilot:
            table = app.query_one("#procs", DataTable)
            self.assertEqual(table.row_count, 1)
            self.assertEqual(app._shown, [42])
            self.assertEqual(str(table.get_cell("42", "down_rate")), "40.0 Mbit/s")
            self.assertIn("above 1%", str(app.query_one("#titlebar").render()))
            self.assertIn("user", app._cols)
            self.assertIn("eth0", str(app.query_one("#titlebar").render()))
            self.assertIn("Mbit/s", str(app.query_one("#titlebar").render()))
            key_style = app.query(FooterKey).first().get_component_styles("footer-key--key")
            self.assertEqual(key_style.background.hex.lower(), "#8fd19a")
            self.assertEqual(key_style.color.hex.lower(), "#1a1a1a")
            desc_style = app.query(FooterKey).first().get_component_styles("footer-key--description")
            self.assertEqual(desc_style.color.hex.lower(), "#ffffff")

            await pilot.press("n")
            self.assertEqual(table.row_count, 3)
            self.assertIn("every process", app._status_text)
            await pilot.press("n")
            self.assertEqual(table.row_count, 1)
            await pilot.press("n")
            self.assertEqual(table.row_count, 3)

            await pilot.press("s")
            self.assertIn("sort by pid", app._status_text)

            await pilot.press("b")
            self.assertEqual(app.unit, Unit.MB)
            self.assertIn("MB/s", str(app.query_one("#titlebar").render()))
            await pilot.press("b")
            # Back to name order: networked first, then command. curl, systemd, bash.
            await pilot.press("s", "s", "s")
            self.assertEqual(app._shown, [42, 1, 7])
            await pilot.pause()
            self.assertEqual(app._selected, 42)
            await pilot.click("#down-pct")
            await pilot.press("3", "0")
            await pilot.click("#up-pct")
            await pilot.press("1", "0")
            await pilot.click("#apply")
            self.assertEqual(fake.applied[-1][0], 42)
            self.assertEqual(fake.applied[-1][1], 30)
            self.assertEqual(fake.applied[-1][2], 10)
            self.assertIn("30%", app._status_text)
            cell = table.get_cell("42", "down_pct")
            self.assertEqual(str(cell), "30")
            self.assertNotIsInstance(cell, Text)

            await pilot.press("escape", "down")
            await pilot.pause()
            moved = table.get_cell("42", "down_pct")
            self.assertIsInstance(moved, Text)
            self.assertIn("f0a35e", str(moved.style))
            await pilot.press("up")

            await pilot.press("u")
            self.assertEqual(fake.removed, [42])
            self.assertIn("removed", app._status_text)

            await pilot.click("#down-pct")
            await pilot.press("0")
            await pilot.press("enter")
            self.assertIn("percent", app._status_text)

            await pilot.resize_terminal(70, 24)
            await pilot.pause()
            self.assertLess(app.size.width, 88)
            self.assertNotIn("user", app._cols)
            self.assertIn("-stacked", app.screen.classes)
            self.assertGreaterEqual(table.row_count, 1)
            self.assertLessEqual(table.virtual_size.width, table.size.width)

            await pilot.resize_terminal(120, 36)
            await pilot.press("escape")
            await pilot.press("slash")
            await pilot.press("b", "a", "s", "h")
            await pilot.pause()
            self.assertEqual(table.row_count, 1)

        self.assertTrue(fake.torn)

    async def test_read_only_without_root(self):
        fake = FakeShaper()
        app = _app(fake, root=False)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            self.assertIsInstance(app.screen, RootRequired)
            self.assertIn("sudo", str(app.screen.query_one("#root-command").render()))
            await pilot.click("#root-ok")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, RootRequired)
            self.assertIn("-show", app.query_one("#banner").classes)
            await pilot.press("a")
            self.assertIn("not running as root", app._status_text)
            self.assertEqual(fake.applied, [])
        self.assertFalse(fake.torn)

    async def test_apply_uses_a_budget_that_was_not_submitted(self):
        fake = FakeShaper()
        app = _app(fake, link_bps=None)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            self.assertIsNone(app._down_budget)
            await pilot.click("#down-budget")
            await pilot.press("1", "0", "0")
            await pilot.click("#down-pct")
            await pilot.press("3", "0")
            await pilot.click("#apply")
            self.assertEqual(fake.applied[-1][:5], (42, 30, None, 100_000_000, None))
            self.assertEqual(app.query_one("#down-budget", Input).value, "100")

    async def test_a_share_stays_visible_after_its_traffic_drops(self):
        fake = FakeShaper()
        rates = {42: (40_000_000.0, 0.0), 1: (100_000.0, 0.0), 7: (0.0, 0.0)}
        app = _app(fake, rates=lambda: dict(rates))
        async with app.run_test(size=(120, 36)) as pilot:
            self.assertEqual(app._shown, [42])
            fake._shares[42] = Share(42, 30, None, 10, "/", "curl")
            rates[42] = (0.0, 0.0)
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app._shown, [42])
            self.assertEqual(str(app.query_one("#procs", DataTable).get_cell("42", "down_rate")), "0.00 Mbit/s")

    def test_columns_fit_the_width(self):
        for width in (64, 70, 90, 120, 200):
            plan = fit_columns(width)
            total = sum(col_width + _COL_PAD for _key, _label, col_width in plan)
            self.assertLessEqual(total, width - 1, plan)
            self.assertIn("command", [key for key, _label, _width in plan])
        narrow = [key for key, _label, _width in fit_columns(70)]
        self.assertNotIn("user", narrow)
        self.assertNotIn("sockets", narrow)
        self.assertIn("down_rate", narrow)
        wide = [key for key, _label, _width in fit_columns(120)]
        self.assertEqual(wide[:4], ["pid", "command", "user", "sockets"])

    async def test_a_tiny_terminal_asks_for_more_room(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(40, 12)) as pilot:
            self.assertIn("-too-small", app.screen.classes)
            message = str(app.query_one("#too-small").render())
            self.assertIn("64", message)
            self.assertIn("24", message)
            await pilot.resize_terminal(80, 30)
            await pilot.pause()
            self.assertNotIn("-too-small", app.screen.classes)
            self.assertIn("-stacked", app.screen.classes)
            table = app.query_one("#procs", DataTable)
            self.assertLessEqual(table.virtual_size.width, table.size.width)
            await pilot.resize_terminal(140, 40)
            await pilot.pause()
            self.assertNotIn("-stacked", app.screen.classes)
            self.assertIn("user", app._cols)
            self.assertLessEqual(table.virtual_size.width, table.size.width)


if __name__ == "__main__":
    unittest.main()
