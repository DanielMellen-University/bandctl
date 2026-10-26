"""Headless Textual pilot for the main screen. No root and no tc."""

from __future__ import annotations

import unittest

from textual.widgets import DataTable

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


def _app(fake, *, root=True):
    from bandctl.app import BandctlApp

    return BandctlApp(
        iface="eth0",
        shaper=fake,
        processes=lambda: list(PROCS),
        link_bps=100_000_000,
        is_root=root,
    )


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_keys_apply_and_resize(self):
        fake = FakeShaper()
        app = _app(fake)
        async with app.run_test(size=(120, 36)) as pilot:
            table = app.query_one("#procs", DataTable)
            self.assertEqual(table.row_count, 3)
            self.assertIn("user", app._cols)
            self.assertIn("eth0", str(app.query_one("#titlebar").render()))
            self.assertIn("Mbit/s", str(app.query_one("#titlebar").render()))

            await pilot.press("n")
            self.assertEqual(table.row_count, 2)
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
            self.assertGreaterEqual(table.row_count, 1)

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


if __name__ == "__main__":
    unittest.main()
