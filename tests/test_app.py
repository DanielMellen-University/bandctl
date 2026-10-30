"""Headless Textual pilot for the main screen. No root and no tc."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

from rich.text import Text
from textual.widgets import Button, DataTable, Footer, Input, RichLog, Static
from textual.widgets._footer import FooterKey

from bandctl.app import BandctlApp, fit_columns, _COL_PAD, _MIN_HEIGHT, _MIN_WIDTH
from bandctl.procscan import ProcessInfo
from bandctl.rates import Unit
from bandctl.shaper import Share, ShaperError
from bandctl.widgets import RootRequired, RulesScreen


PROCS = [
    ProcessInfo(1, "systemd", "/sbin/systemd", "root", 4, 1),
    ProcessInfo(42, "curl", "curl https://example.com", "daniel", 2, 42),
    ProcessInfo(7, "bash", "bash", "daniel", 0, 7),
]


class FakeShaper:
    def __init__(self) -> None:
        self.applied: list[tuple] = []
        self.removed: list[int] = []
        self.caps: list[tuple] = []
        self.expected_starts: list[int | None] = []
        self.revision = 0
        self._shares: dict[int, Share] = {}
        self.torn = False

    def apply(self, pid, down_pct, up_pct, down_cap, up_cap, command="", *, expected_start_ticks=None,
              only_if_unlimited=False):
        if only_if_unlimited and pid in self._shares:
            raise ShaperError("already limited")
        if down_pct and not down_cap:
            raise ShaperError("download capacity is unknown; enter a download budget")
        if up_pct and not up_cap:
            raise ShaperError("upload capacity is unknown; enter an upload budget")
        self.applied.append((pid, down_pct, up_pct, down_cap, up_cap, command))
        self.expected_starts.append(expected_start_ticks)
        self.revision += 1
        self._shares[pid] = Share(pid, down_pct, up_pct, 10, "/", command, expected_start_ticks, self.revision)
        down = "unlimited" if down_pct is None else f"{down_pct}%"
        up = "unlimited" if up_pct is None else f"{up_pct}%"
        return f"pid {pid} download {down}, upload {up}"

    def apply_tracked(self, pid, *args, **kwargs):
        message = self.apply(pid, *args, **kwargs)
        return message, self._shares[pid].revision

    def remove(self, pid, *, expected_revision=None):
        if pid not in self._shares:
            raise ShaperError(f"pid {pid} has no limit")
        if expected_revision is not None and self._shares[pid].revision != expected_revision:
            raise ShaperError(f"limit for pid {pid} changed")
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
    async def test_failed_recovery_is_shown_and_apply_is_disabled(self):
        from bandctl.shaper import Shaper
        from test_shaper import _shaper

        previous, host = _shaper()
        previous.apply(42, None, 10, None, 100_000_000)
        host.fail_substr = "tc qdisc add dev eth0 root"
        shaper = Shaper("eth0", host=host, state_path="/run/bandctl.json")
        app = _app(None)
        with patch("bandctl.app.Shaper", return_value=shaper):
            async with app.run_test(size=(80, 24)):
                self.assertIn("recovery failed", app._status_text)
                self.assertIn("forced failure", app._status_text)
                self.assertTrue(app.query_one("#apply", Button).disabled)
                self.assertTrue(app.query_one("#remove", Button).disabled)

    async def test_manual_apply_verifies_selected_process_identity(self):
        fake = FakeShaper()
        proc = ProcessInfo(42, "curl", "curl", "user", 1, 123)
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=_rates)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#down-pct", Input).value = "10"
            app.action_apply_limit()
            self.assertEqual(fake.expected_starts, [123])

    async def test_manual_apply_rejects_unknown_process_identity(self):
        fake = FakeShaper()
        proc = ProcessInfo(42, "curl", "curl", "user", 1)
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=_rates)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#down-pct", Input).value = "10"
            app.action_apply_limit()
            self.assertEqual(fake.applied, [])
            self.assertIn("cannot verify", app._status_text)

    async def test_session_total_is_visible_and_stays_in_bytes_when_units_change(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(120, 40)) as pilot:
            app._sampler._totals[42] = (1_000_000, 500_000)
            await app._refresh()
            self.assertIn("1.50 MB", str(app.query_one("#detail-title", Static).render()))
            self.assertIn("total", app._cols)
            self.assertEqual(str(app.query_one("#procs", DataTable).get_cell("42", "total")), "1.50 MB")
            await pilot.press("b")
            self.assertIn("1.50 MB", str(app.query_one("#detail-title", Static).render()))
            await pilot.resize_terminal(60, 20)
            await pilot.pause()
            self.assertNotIn("total", app._cols)
            title = app.query_one("#detail-title", Static)
            self.assertIn("1.50 MB", str(title.render()))
            self.assertLessEqual(len(str(title.render())), title.content_size.width)

    async def test_filter_matches_name_and_pid_and_restores_rows(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("n", "slash")
            field = app.query_one("#filter", Input)
            field.value = "CURL"
            await pilot.pause()
            self.assertEqual(app._shown, [42])
            field.value = "7"
            await pilot.pause()
            self.assertEqual(app._shown, [7])
            field.value = "no-such-process"
            await pilot.pause()
            self.assertEqual(app._shown, [])
            field.value = ""
            await pilot.pause()
            self.assertEqual(set(app._shown), {1, 7, 42})

    async def test_filter_finds_idle_processes_without_listing_everything(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app._shown, [42])
            await pilot.press("slash")
            field = app.query_one("#filter", Input)
            field.value = "bash"
            await pilot.pause()
            self.assertEqual(app._shown, [7])
            field.value = "1"
            await pilot.pause()
            self.assertEqual(set(app._shown), {1})
            field.value = "no-such-process"
            await pilot.pause()
            self.assertEqual(app._shown, [])
            title = str(app.query_one("#detail-title", Static).render())
            self.assertIn("No process matches no-such-process", title)
            field.value = ""
            await pilot.pause()
            self.assertEqual(app._shown, [42])

    async def test_scheduled_rule_applies_expires_and_respects_manual_override(self):
        from datetime import time
        from bandctl.rules import AutoRule
        from bandctl.schedule import DailyWindow

        fake = FakeShaper()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        current = [time(8, 59)]
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=lambda: {42: (2_000_000, 0)},
                         clock=lambda: current[0])
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None, DailyWindow(time(9), time(17)))]
            await app._apply_rules([proc])
            self.assertFalse(fake.shares)
            current[0] = time(9)
            await app._apply_rules([proc])
            self.assertEqual(fake.shares[42].down_pct, 30)
            current[0] = time(17)
            await app._apply_rules([proc])
            self.assertFalse(fake.shares)
            current[0] = time(9)
            await app._apply_rules([proc])
            self.assertIn(42, fake.shares)
            app.query_one("#down-pct", Input).value = "30"
            app.action_apply_limit()
            current[0] = time(17)
            await app._apply_rules([proc])
            self.assertIn(42, fake.shares)

    async def test_schedule_does_not_remove_a_concurrent_manual_change(self):
        from datetime import time
        from bandctl.rules import AutoRule
        from bandctl.schedule import DailyWindow

        class RacingRemove(FakeShaper):
            def remove(self, pid, *, expected_revision=None):
                if expected_revision is not None:
                    self.apply(pid, None, 70, None, 100_000_000)
                return super().remove(pid, expected_revision=expected_revision)

        fake = RacingRemove()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        current = [time(9)]
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=lambda: {}, clock=lambda: current[0])
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None, DailyWindow(time(9), time(17)))]
            await app._apply_rules([proc])
            current[0] = time(17)
            await app._apply_rules([proc])
            self.assertEqual(fake.shares[42].up_pct, 70)
            await app._apply_rules([proc])
            self.assertEqual(fake.shares[42].up_pct, 70)

    async def test_scheduled_rule_takes_precedence_over_an_automatic_fallback(self):
        from datetime import time
        from bandctl.rules import AutoRule
        from bandctl.schedule import DailyWindow

        fake = FakeShaper()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        current = [time(8)]
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=lambda: {}, clock=lambda: current[0])
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None, DailyWindow(time(9), time(17))),
                          AutoRule("steam", None, 10)]
            await app._apply_rules([proc])
            self.assertEqual((fake.shares[42].down_pct, fake.shares[42].up_pct), (None, 10))
            current[0] = time(9)
            await app._apply_rules([proc])
            self.assertEqual((fake.shares[42].down_pct, fake.shares[42].up_pct), (30, None))
            current[0] = time(17)
            await app._apply_rules([proc])
            self.assertEqual((fake.shares[42].down_pct, fake.shares[42].up_pct), (None, 10))

    def test_late_table_event_after_screen_closes_is_ignored(self):
        app = _app(FakeShaper())
        event = SimpleNamespace(data_table=SimpleNamespace(id="procs"))
        with patch.object(BandctlApp, "screen_stack", new_callable=PropertyMock, return_value=[]):
            app.on_data_table_row_highlighted(event)

    async def test_late_refresh_after_screen_closes_is_ignored(self):
        app = _app(FakeShaper())
        with patch.object(BandctlApp, "screen_stack", new_callable=PropertyMock, return_value=[]):
            await app._refresh()
        self.assertFalse(app._busy)

    async def test_auto_rule_applies_to_new_process_and_respects_manual_removal(self):
        from bandctl.rules import AutoRule

        fake = FakeShaper()
        procs = [ProcessInfo(42, "steam", "steam", "user", 1, 100)]
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: list(procs),
                         link_bps=100_000_000, is_root=True, rates=lambda: {42: (2_000_000, 0)})
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None)]
            await app._refresh()
            self.assertEqual(fake.applied[-1][:5], (42, 30, None, 100_000_000, 100_000_000))
            self.assertEqual(fake.expected_starts[-1], 100)
            app.action_remove_limit()
            await app._refresh()
            self.assertEqual(len(fake.applied), 1)
            procs.append(ProcessInfo(43, "steamwebhelper", "steamwebhelper", "user", 0, 101))
            await app._refresh()
            self.assertEqual(fake.applied[-1][0], 43)

    async def test_failed_auto_rule_reports_error_and_waits_before_retry(self):
        from bandctl.rules import AutoRule

        class FailingShaper(FakeShaper):
            attempts = 0

            def apply(self, *args, **kwargs):
                self.attempts += 1
                raise ShaperError("forced failure")

        fake = FailingShaper()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=lambda: {})
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None)]
            await app._refresh()
            self.assertIn("forced failure", app._status_text)
            await app._refresh()
            self.assertEqual(fake.attempts, 1)

    async def test_auto_rule_leaves_a_limit_added_during_apply(self):
        from bandctl.rules import AutoRule

        class RacingShaper(FakeShaper):
            def apply(self, pid, *args, **kwargs):
                self._shares[pid] = Share(pid, None, 70, 10, "/", "manual")
                return super().apply(pid, *args, **kwargs)

        fake = RacingShaper()
        proc = ProcessInfo(42, "steam", "steam", "user", 1, 100)
        app = BandctlApp(iface="eth0", shaper=fake, processes=lambda: [proc],
                         link_bps=100_000_000, is_root=True, rates=lambda: {})
        async with app.run_test(size=(100, 30)) as pilot:
            app._rules = [AutoRule("steam", 30, None)]
            await app._refresh()
            self.assertEqual(fake.shares[42].up_pct, 70)
            await app._refresh()
            self.assertEqual(fake.shares[42].up_pct, 70)

    async def test_rule_editor_enter_adds_and_colors_errors_only(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, RulesScreen)
            screen = app.screen
            status = screen.query_one("#rule-status", Static)
            screen.query_one("#rule-name", Input).value = "steam"
            screen.query_one("#rule-down", Input).value = "abc"
            screen.query_one("#rule-down", Input).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(screen.query_one("#rule-table", DataTable).row_count, 0)
            self.assertTrue(status.has_class("-error"))
            screen.query_one("#rule-down", Input).value = "20"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(screen.query_one("#rule-table", DataTable).row_count, 1)
            self.assertIn("Rule added", str(status.render()))
            self.assertFalse(status.has_class("-error"))
            self.assertIs(app.screen, screen)

    async def test_rule_editor_adds_and_deletes_rules(self):
        fake = FakeShaper()
        fake._shares[42] = Share(42, 30, None, 10, "/", "curl")
        app = _app(fake)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("t")
            await pilot.pause()
            self.assertIsInstance(app.screen, RulesScreen)
            app.screen.query_one("#rule-add", Button).active_effect_duration = 0
            app.screen.query_one("#rule-name", Input).value = "steam"
            app.screen.query_one("#rule-down", Input).value = "30"
            await pilot.click("#rule-add")
            self.assertEqual(app.screen.query_one("#rule-table", DataTable).row_count, 1)
            app.screen.query_one("#rule-table", DataTable).focus()
            await pilot.press("u", "q")
            self.assertIsInstance(app.screen, RulesScreen)
            self.assertIn(42, fake.shares)
            await pilot.click("#rule-delete")
            self.assertEqual(app.screen.query_one("#rule-table", DataTable).row_count, 0)
            app.screen.query_one("#rule-name", Input).value = "steam"
            app.screen.query_one("#rule-up", Input).value = "10"
            app.screen.query_one("#rule-start", Input).value = "22:00"
            await pilot.click("#rule-add")
            self.assertIn("both times", str(app.screen.query_one("#rule-status", Static).render()))
            app.screen.query_one("#rule-end", Input).value = "06:00"
            await pilot.click("#rule-add")
            self.assertEqual(app.screen.query_one("#rule-table", DataTable).row_count, 1,
                             str(app.screen.query_one("#rule-status", Static).render()))
            await pilot.click("#rule-close")
            await pilot.pause()
            self.assertEqual([(rule.name, rule.down_pct, rule.up_pct) for rule in app._rules], [("steam", None, 10)])
            self.assertEqual(str(app._rules[0].window.start), "22:00:00")
            self.assertEqual(str(app._rules[0].window.end), "06:00:00")

    async def test_rule_editor_fits_supported_sizes(self):
        for width, height in ((60, 18), (60, 20), (80, 24), (100, 30), (120, 40), (160, 50), (77, 38)):
            with self.subTest(size=(width, height)):
                app = _app(FakeShaper())
                async with app.run_test(size=(width, height)) as pilot:
                    await pilot.press("t")
                    await pilot.pause()
                    dialog = app.screen.query_one("#rules-dialog").region
                    self.assertLessEqual(dialog.right, width)
                    self.assertLessEqual(dialog.bottom, height)
                    for query in ("#rules-title", "#rules-help", "#rule-table", "#rule-name", "#rule-down",
                                  "#rule-up", "#rule-start", "#rule-end", "#rule-status", "#rule-add",
                                  "#rule-delete", "#rule-close"):
                        region = app.screen.query_one(query).region
                        self.assertGreaterEqual(region.width, 3, query)
                        self.assertGreaterEqual(region.height, 1, query)
                        self.assertTrue(dialog.contains_region(region), f"{width}x{height} {query}")

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
            self.assertIn("view processes and traffic", str(app.screen.query_one("#root-body").render()))
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

    async def test_graphs_follow_the_selected_process(self):
        app = _app(FakeShaper())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._paint_detail()
            self.assertIn("█", str(app.query_one("#down-history", Static).render()))
            await pilot.press("n", "down")
            await pilot.pause()
            self.assertEqual(app._selected, 1)
            self.assertIn("█", str(app.query_one("#down-history", Static).render()))
            self.assertIn("▁", str(app.query_one("#up-history", Static).render()))

    async def test_graph_history_is_cleared_when_pid_is_reused(self):
        processes = [ProcessInfo(42, "old", "old", "user", 1, 10)]
        app = BandctlApp(iface="eth0", shaper=FakeShaper(), processes=lambda: list(processes),
                         link_bps=100_000_000, is_root=True, rates=lambda: {42: (5_000_000, 0)})
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            import time
            app._history.record(time.monotonic() - 10, {42}, {42: (9_000_000, 0)})
            processes[0] = ProcessInfo(42, "new", "new", "user", 1, 20)
            await app._refresh()
            graph = app._history.graph(42, 0, time.monotonic(), 60)
            self.assertEqual(graph.strip(), "▁")
            app._sampler._totals[42] = (500, 200)
            processes.clear()
            await app._refresh()
            processes.append(ProcessInfo(42, "newer", "newer", "user", 1, 30))
            await app._refresh()
            self.assertNotIn(42, app._sampler.totals)

    async def test_narrow_capped_rate_caption_fits(self):
        fake = FakeShaper()
        fake._shares[42] = Share(42, 30, None, 10, "/", "curl")
        app = _app(fake)
        async with app.run_test(size=(60, 20)) as pilot:
            await pilot.pause()
            caption = app.query_one("#down-caption", Static)
            self.assertIn("30%", str(caption.render()))
            self.assertLessEqual(len(str(caption.render())), caption.content_size.width)

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
            self.assertIn("60 columns", message)
            self.assertIn("18 rows", message)
            await pilot.resize_terminal(55, 30)
            await pilot.pause()
            self.assertIn("-too-small", app.screen.classes)
            await pilot.resize_terminal(80, 17)
            await pilot.pause()
            self.assertIn("-too-small", app.screen.classes)
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


# Many rows, long command lines, and two shares, so every part of the
# screen has something to show.
MANY = [
    ProcessInfo(
        1000 + index,
        f"proc{index}",
        f"/usr/bin/proc{index} --a-long-option value --another-long-option value",
        "daniel",
        index % 5,
    )
    for index in range(40)
]
LAYOUT_SIZES = (
    (_MIN_WIDTH, _MIN_HEIGHT),
    (60, 20),
    (64, 24),
    (80, 24),
    (100, 30),
    (107, 31),
    (108, 32),
    (120, 40),
    (160, 50),
    (77, 38),
    (87, 44),
    (200, 60),
)
# Top to bottom, the order the main screen stacks its parts in.
_STACK = ("#titlebar", "#capacity", "#filter", "#procs", "#detail", "#log", "#status", "Footer")


class LayoutTests(unittest.IsolatedAsyncioTestCase):
    """Every supported size keeps each part of the screen visible and usable."""

    async def _check(self, width: int, height: int) -> None:
        fake = FakeShaper()
        fake._shares[1001] = Share(1001, 30, None, 10, "/", "proc1")
        fake._shares[1002] = Share(1002, None, 10, 11, "/", "proc2")
        app = BandctlApp(
            iface="eth0",
            shaper=fake,
            processes=lambda: list(MANY),
            link_bps=10_000_000_000,
            is_root=True,
            rates=lambda: {proc.pid: (5_000_000_000.0, 900_000_000.0) for proc in MANY},
        )
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause()
            await pilot.pause()
            where = f"{width}x{height}"
            screen = app.screen
            self.assertNotIn("-too-small", screen.classes, where)

            previous_bottom = 0
            for query in _STACK:
                widget = app.query_one(query)
                region = widget.region
                self.assertTrue(widget.display, f"{where} {query} hidden")
                self.assertGreaterEqual(region.height, 1, f"{where} {query} has no height")
                self.assertLessEqual(region.right, width, f"{where} {query} too wide")
                self.assertLessEqual(region.bottom, height, f"{where} {query} below the screen")
                self.assertGreaterEqual(region.y, previous_bottom, f"{where} {query} overlaps")
                previous_bottom = region.bottom
            self.assertEqual(app.query_one(Footer).region.bottom, height, where)

            table = app.query_one("#procs", DataTable)
            # Header plus at least five process rows.
            self.assertGreaterEqual(table.size.height, 6, where)
            self.assertLessEqual(table.virtual_size.width, table.size.width, where)
            self.assertFalse(table.show_horizontal_scrollbar, where)
            command = dict((key, size) for key, _label, size in fit_columns(width))["command"]
            self.assertGreaterEqual(command, 12, where)

            detail = app.query_one("#detail").region
            for query in ("#detail-title", "#pct-row", "#down-pct", "#up-pct", "#apply", "#remove",
                          "#down-caption", "#down-bar", "#down-history", "#up-caption", "#up-bar", "#up-history"):
                region = app.query_one(query).region
                self.assertGreaterEqual(region.height, 1, f"{where} {query}")
                self.assertGreaterEqual(region.width, 3, f"{where} {query}")
                self.assertTrue(detail.contains_region(region), f"{where} {query} outside the detail panel")
            row = app.query_one("#pct-row").region
            for query in ("#apply", "#remove"):
                button = app.query_one(query, Button)
                # The label fits on one line, so the button is no taller than its row.
                self.assertLessEqual(button.region.height, row.height, f"{where} {query} wraps")
                self.assertGreaterEqual(button.content_size.width, len(str(button.label)), f"{where} {query}")

            log = app.query_one("#log", RichLog)
            self.assertGreaterEqual(log.content_size.height, 2, f"{where} log shows under two lines")
            self.assertFalse(log.show_horizontal_scrollbar, where)

            speed = app.query_one("#speed", Static)
            for line in str(speed.render()).splitlines():
                self.assertLessEqual(len(line), speed.content_size.width, f"{where} speed label clipped: {line}")
            self.assertLessEqual(len(str(speed.render()).splitlines()), speed.content_size.height, where)

            # The detail panel and table still work at this size.
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app._selected, app._shown[1], where)

    async def test_supported_sizes(self):
        for width, height in LAYOUT_SIZES:
            with self.subTest(size=f"{width}x{height}"):
                await self._check(width, height)

    async def test_breakpoints(self):
        app = _app(FakeShaper())
        expected = {
            (60, 20): {"-narrow", "-stacked", "-compact", "-short"},
            (80, 24): {"-stacked", "-compact"},
            (100, 30): {"-stacked", "-compact"},
            (77, 38): {"-narrow", "-stacked"},
            (120, 40): set(),
            (160, 50): {"-tall"},
        }
        names = {"-narrow", "-stacked", "-compact", "-short", "-tall"}
        async with app.run_test(size=(120, 40)) as pilot:
            for (width, height), classes in expected.items():
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(set(app.screen.classes) & names, classes, f"{width}x{height}")

    async def test_not_root_dialog_fits_a_small_terminal(self):
        for width, height in ((60, 20), (100, 30)):
            app = _app(FakeShaper(), root=False)
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, RootRequired)
                dialog = app.screen.query_one("#dialog").region
                self.assertLessEqual(dialog.right, width)
                self.assertLessEqual(dialog.bottom, height)
                button = app.screen.query_one("#root-ok", Button)
                self.assertGreaterEqual(button.content_size.width, len("View processes"))
                self.assertTrue(dialog.contains_region(button.region))

    def test_narrow_columns_keep_a_readable_command(self):
        plan = {key: (label, size) for key, label, size in fit_columns(60)}
        self.assertEqual(plan["down_pct"], ("Dn%", 3))
        self.assertEqual(plan["up_pct"], ("Up%", 3))
        self.assertGreaterEqual(plan["command"][1], 12)
        wide = {key: (label, size) for key, label, size in fit_columns(120)}
        self.assertEqual(wide["down_pct"], ("Down %", 6))


if __name__ == "__main__":
    unittest.main()
