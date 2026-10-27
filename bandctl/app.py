"""Textual UI for per-process download and upload shares."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, RichLog, Static

from bandctl.group_screen import GroupsScreen
from bandctl.iface import IfaceError, default_iface, link_speed_bps
from bandctl.history import RateHistory
from bandctl.procscan import ProcessInfo, scan_processes
from bandctl.rates import (
    RateError,
    Unit,
    format_budget_input,
    format_bytes,
    format_rate,
    over_commit_message,
    parse_budget,
    parse_percent,
    percent_to_bps,
)
from bandctl.shaper import Shaper, ShaperError
from bandctl.settings_screen import SettingsScreen
from bandctl.settings_session import import_session
from bandctl.rules import AutoRule, RuleTracker, ScheduledOwnership
from bandctl.traffic import TrafficSampler, above_share, read_ss
from bandctl.widgets import RootRequired, RulesScreen

_MISSING = object()

# (key, header, content width, dropped first when the terminal is narrow).
# Rendered width is the content width plus two cells of padding.
_COLUMNS = (
    ("pid", "PID", 7, False),
    ("user", "User", 10, True),
    ("sockets", "Sockets", 7, True),
    ("down_pct", "Down %", 6, False),
    ("up_pct", "Up %", 5, False),
    ("down_rate", "Down", 12, False),
    ("up_rate", "Up", 12, False),
    ("total", "TCP total", 9, True),
)
# Narrower headers and rate cells, used when the command column would
# otherwise be squeezed below _COMMAND_MIN cells.
_TIGHT = {"down_pct": ("Dn%", 3), "up_pct": ("Up%", 3), "down_rate": ("Down", 11), "up_rate": ("Up", 11)}
_COMMAND_MIN = 16
_COL_PAD = 2
# Layout breakpoints. Below _STACK_BELOW columns the meters sit under the
# controls. Below _COMPACT_BELOW rows the inputs and buttons drop their
# borders and take one row each, so the table keeps its space. Below
# _SHORT_BELOW rows the log shrinks to two lines.
_MIN_WIDTH = 60
_MIN_HEIGHT = 18
_STACK_BELOW = 108
_COMPACT_BELOW = 32
_SHORT_BELOW = 22
_TALL_FROM = 42
_SORTS = ("name", "pid", "down", "up")
_SORT_LABEL = {"name": "name", "pid": "pid", "down": "download", "up": "upload"}


class BandctlApp(App):
    """Process table, percent fields, live bars, and an error log."""

    CSS_PATH = Path(__file__).with_name("theme.tcss")
    TITLE = "bandctl"
    # Footer order is most used first, so a narrow terminal clips the least
    # needed key (the table already refreshes every second).
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "apply_limit", "Apply"),
        Binding("u", "remove_limit", "Remove"),
        Binding("g", "edit_groups", "Groups"),
        Binding("x", "edit_settings", "Config"),
        Binding("slash", "focus_filter", "Search", key_display="/"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("n", "toggle_all", "All"),
        Binding("b", "toggle_unit", "Unit"),
        Binding("t", "edit_rules", "Rules"),
        Binding("r", "refresh_now", "Refresh", show=False),
        Binding("escape", "focus_table", "Table", show=False),
    ]

    def __init__(
        self,
        iface: str | None = None,
        *,
        shaper: Shaper | None = None,
        processes=None,
        link_bps: int | None | object = _MISSING,
        is_root: bool | None = None,
        rates=None,
        clock=None,
        startup_settings: str | None = None,
    ) -> None:
        super().__init__()
        self._iface_arg = iface
        self._startup_settings = startup_settings
        self._shaper_arg = shaper
        self._process_source = processes
        self._rates_source = rates
        self._clock = clock or (lambda: datetime.now().astimezone().time())
        self._link_bps_arg = link_bps
        self.is_root = os.geteuid() == 0 if is_root is None else is_root
        self.iface = iface or ""
        self.shaper = shaper
        self.unit = Unit.MBIT
        self._sort = "name"
        self._show_all = False
        self._sampler = TrafficSampler()
        self._ss_failed = False
        self._ss_warned = False
        self._procs: list[ProcessInfo] = []
        self._usage: dict[int, tuple[float, float]] = {}
        self._session_totals: dict[int, tuple[int, int]] = {}
        self._history = RateHistory()
        self._rules: list[AutoRule] = []
        self._rule_tracker = RuleTracker()
        self._scheduled = ScheduledOwnership()
        self._process_starts: dict[int, int | None] = {}
        self._process_seen: dict[int, float] = {}
        self._selected: int | None = None
        self._shown: list[int] = []
        self._cols: tuple[str, ...] = ()
        self._col_widths: tuple[int, ...] = ()
        self._command_width = 20
        self._detected_bps: int | None = None
        self._down_budget: int | None = None
        self._up_budget: int | None = None
        self._pushed_caps: tuple[int | None, int | None] = (None, None)
        self._busy = False
        self._status_text = ""
        self._status_bad = False
        self._width = 0
        self._height = 0
        self._flow_warning: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        yield Static("bandctl", id="titlebar")
        yield Static("", id="too-small")
        yield Horizontal(
            Static("", id="speed"),
            Input(placeholder="down budget", id="down-budget"),
            Input(placeholder="up budget", id="up-budget"),
            id="capacity",
        )
        yield Input(placeholder="Filter by name, pid, or user", id="filter")
        yield DataTable(id="procs", cursor_type="row", zebra_stripes=True)
        yield Horizontal(
            Vertical(
                Static("Select a process", id="detail-title"),
                Horizontal(
                    Label("down %"),
                    Input(placeholder="30", id="down-pct"),
                    Label("up %"),
                    Input(placeholder="10", id="up-pct"),
                    Button("Apply", id="apply"),
                    Button("Remove", id="remove"),
                    id="pct-row",
                ),
                id="detail-controls",
            ),
            Vertical(
                Vertical(
                    Static("download  no cap", id="down-caption"),
                    Horizontal(ProgressBar(total=100, show_eta=False, show_percentage=False, id="down-bar"), Static("", id="down-history", classes="history"), classes="meter-values"),
                    id="down-meter",
                    classes="meter",
                ),
                Vertical(
                    Static("upload  no cap", id="up-caption"),
                    Horizontal(ProgressBar(total=100, show_eta=False, show_percentage=False, id="up-bar"), Static("", id="up-history", classes="history"), classes="meter-values"),
                    id="up-meter",
                    classes="meter",
                ),
                id="meters",
            ),
            id="detail",
        )
        yield RichLog(id="log", max_lines=200, wrap=True, markup=False, highlight=False)
        yield Static("", id="status")
        yield Footer(compact=False, show_command_palette=False)

    async def on_mount(self) -> None:
        self._resolve_iface()
        if self._shaper_arg is not None:
            self.shaper = self._shaper_arg
        elif self.is_root and self.iface:
            self.shaper = Shaper(self.iface)
            for note in self.shaper.recover():
                self._log(note)
        await self._refresh()
        if self.is_root and not self._ss_warned:
            self._note("ready")
        else:
            self.query_one("#apply", Button).disabled = True
            self.query_one("#remove", Button).disabled = True
            banner = self.query_one("#banner", Static)
            banner.update("Not root. Limits stay off.  " + self.sudo_command())
            banner.add_class("-show")
            self._note("read-only, limits are off", bad=True)
        if self._startup_settings:
            if not self.is_root or self.shaper is None:
                self._note("cannot import settings without root and an interface", bad=True)
            else:
                try:
                    settings = import_session(self._startup_settings, self.shaper, self.iface,
                                              self._detected_bps, self._procs)
                except ValueError as exc:
                    self._note(f"settings import: {exc}", bad=True)
                else:
                    self._settings_closed(settings)
        self.set_interval(1.0, self._refresh)
        self.watch(self.screen, "size", self._on_screen_size, init=False)
        self.query_one("#procs", DataTable).focus()
        # Push after the main screen is built. A modal installed earlier
        # would hide the widgets on_mount still has to query.
        if not self.is_root:
            self.push_screen(RootRequired(self.sudo_command()))

    def on_unmount(self) -> None:
        shaper = self.shaper
        if shaper is not None and self.is_root:
            shaper.teardown()

    def sudo_command(self) -> str:
        extra = f" --iface {self._iface_arg}" if self._iface_arg else ""
        # The installed command's argv is `bandctl`. A checkout is `python -m`.
        if Path(sys.argv[0]).name == "bandctl":
            return f"sudo bandctl{extra}"
        return f"sudo {sys.executable} -m bandctl{extra}"

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.is_mounted and self.screen_stack and isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen)):
            if action in {"quit", "apply_limit", "remove_limit", "focus_filter", "cycle_sort", "toggle_all",
                          "toggle_unit", "edit_rules", "edit_groups", "edit_settings", "refresh_now", "focus_table"}:
                return False
        return super().check_action(action, parameters)

    def on_resize(self, event) -> None:
        # The Resize message carries the new size. self.size can still be the
        # previous one while this handler runs.
        size = getattr(event, "size", None)
        if size is not None:
            self._width = size.width
            self._height = size.height
        if self.is_mounted and self.screen_stack and not isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen)):
            self._render()

    def _on_screen_size(self, _old, new) -> None:
        self._width = new.width
        self._height = new.height
        if self.is_mounted and self.screen_stack and not isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen)):
            self._render()

    def _dims(self) -> tuple[int, int]:
        if self._width and self._height:
            return self._width, self._height
        return self.size.width, self.size.height

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("down-pct", "up-pct"):
            event.stop()
            self.action_apply_limit()
        elif event.input.id in ("down-budget", "up-budget"):
            event.stop()
            self._commit_budgets(announce=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.action_apply_limit()
        elif event.button.id == "remove":
            self.action_remove_limit()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if (not self.screen_stack or isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen))
                or event.data_table.id != "procs"):
            return
        try:
            pid = int(str(event.row_key.value))
        except (TypeError, ValueError):
            return
        self._selected = pid
        self._fill_inputs(pid)
        # Limited rows are orange except the cursor. Repaint now, otherwise
        # the previous row stays plain and this one stays orange for a second.
        self._render_table()
        self._paint_detail()

    def on_key(self, event) -> None:
        # Enter in the table applies. Enter inside a percent field is handled
        # by Input.Submitted, and a click only moves the highlight.
        if (event.key == "enter" and not isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen))
                and self.focused is self.query_one("#procs", DataTable)):
            self.action_apply_limit()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#procs", DataTable).focus()

    def action_toggle_all(self) -> None:
        self._show_all = not self._show_all
        if self._show_all:
            self._note("showing every process")
        else:
            self._note("showing processes above 1%")
        self._render()

    def action_cycle_sort(self) -> None:
        index = _SORTS.index(self._sort)
        self._sort = _SORTS[(index + 1) % len(_SORTS)]
        self._note(f"sort by {_SORT_LABEL[self._sort]}")
        self._render()

    def action_toggle_unit(self) -> None:
        if not self._commit_budgets(announce=False):
            return
        self.unit = Unit.MB if self.unit is Unit.MBIT else Unit.MBIT
        self._write_budget_inputs()
        self._note(f"units are {self.unit.value}")
        self._render()

    def action_refresh_now(self) -> None:
        self.run_worker(self._refresh(), exclusive=True)

    def action_edit_rules(self) -> None:
        if not self.is_root:
            self._note("not running as root. " + self.sudo_command(), bad=True)
            return
        if self._busy:
            self._note("refresh in progress; try again")
            return
        self.push_screen(RulesScreen(self._rules), self._rules_closed)

    def _rules_closed(self, rules: list[AutoRule] | None) -> None:
        if rules is not None:
            self._rules = rules
            self._note(f"{len(rules)} auto-apply rules")
            self.action_refresh_now()

    def action_edit_groups(self) -> None:
        if not self.is_root:
            self._note("not running as root. " + self.sudo_command(), bad=True)
            return
        if self._busy:
            self._note("refresh in progress; try again")
            return
        if self.shaper is None:
            self._note("no interface to shape", bad=True)
            return
        if not self._commit_budgets(announce=False):
            return
        self.push_screen(GroupsScreen(self.shaper, self._procs, *self._effective_caps()), self._groups_closed)

    def _groups_closed(self, changed: bool | None) -> None:
        if changed:
            self._pushed_caps = self._effective_caps()
            self._note("groups updated")
            self.action_refresh_now()

    def action_edit_settings(self) -> None:
        if not self.is_root:
            self._note("not running as root. " + self.sudo_command(), bad=True)
            return
        if self._busy:
            self._note("refresh in progress; try again")
            return
        if self.shaper is None:
            self._note("no interface to shape", bad=True)
            return
        self.push_screen(SettingsScreen(self.shaper, self.iface, self._detected_bps,
                                        self._down_budget, self._up_budget, self._rules, self._procs),
                         self._settings_closed)

    def _settings_closed(self, settings) -> None:
        if settings is None:
            return
        self._rules = list(settings.rules)
        self._down_budget, self._up_budget = settings.down_budget, settings.up_budget
        self._write_budget_inputs()
        self._pushed_caps = self._effective_caps()
        self._note("settings imported")
        self._render()
        self.action_refresh_now()

    def action_apply_limit(self) -> None:
        if not self.is_root:
            self._note("not running as root. " + self.sudo_command(), bad=True)
            return
        if self._selected is None:
            self._note("select a process first", bad=True)
            return
        if self.shaper is None:
            self._note("no interface to shape", bad=True)
            return
        # A budget typed in the header counts even if that field never saw Enter.
        if not self._commit_budgets(announce=False):
            return
        try:
            down = parse_percent(self.query_one("#down-pct", Input).value, allow_empty=True)
            up = parse_percent(self.query_one("#up-pct", Input).value, allow_empty=True)
        except RateError as exc:
            self._note(str(exc), bad=True)
            return
        proc = self._proc(self._selected)
        command = proc.cmdline if proc else str(self._selected)
        down_cap, up_cap = self._effective_caps()
        try:
            message = self.shaper.apply(self._selected, down, up, down_cap, up_cap, command)
        except ShaperError as exc:
            self._note(str(exc), bad=True)
            return
        if proc is not None:
            self._scheduled.forget(proc)
            self._rule_tracker.applied(proc)
        self._pushed_caps = (down_cap, up_cap)
        extra = self._over_commit()
        if extra:
            message = f"{message}. {extra}"
        self._note(message)
        self._render()

    def action_remove_limit(self) -> None:
        if not self.is_root:
            self._note("not running as root. " + self.sudo_command(), bad=True)
            return
        if self._selected is None or self.shaper is None:
            self._note("select a process first", bad=True)
            return
        try:
            message = self.shaper.remove(self._selected)
        except ShaperError as exc:
            self._note(str(exc), bad=True)
            return
        proc = self._proc(self._selected)
        if proc is not None:
            self._scheduled.forget(proc)
            self._rule_tracker.applied(proc)
        self.query_one("#down-pct", Input).value = ""
        self.query_one("#up-pct", Input).value = ""
        self._note(message)
        self._render()

    async def _refresh(self) -> None:
        if self._busy or isinstance(self.screen, (RootRequired, RulesScreen, GroupsScreen, SettingsScreen)):
            return
        self._busy = True
        try:
            procs = await asyncio.to_thread(self._load_procs)
            usage = await asyncio.to_thread(self._load_usage)
            if self._ss_failed and not self._ss_warned:
                self._ss_warned = True
                self._note("could not read traffic with ss. Press n to list every process.", bad=True)
            # sample() also refreshes flower filters. The table uses _load_usage.
            if self.shaper is not None:
                await asyncio.to_thread(self.shaper.sample)
            if self._link_bps_arg is _MISSING and self.iface:
                detected = await asyncio.to_thread(link_speed_bps, self.iface)
                self._detected_bps = detected
            self._procs = procs
            self._usage = usage
            now = time.monotonic()
            for proc in procs:
                previous = self._process_starts.get(proc.pid)
                if previous is not None and proc.start_ticks is not None and previous != proc.start_ticks:
                    self._history.forget(proc.pid)
                    self._sampler.forget_total(proc.pid)
                    usage.pop(proc.pid, None)
                if proc.start_ticks is not None or proc.pid not in self._process_starts:
                    self._process_starts[proc.pid] = proc.start_ticks
                self._process_seen[proc.pid] = now
            for pid, seen in list(self._process_seen.items()):
                if now - seen > 300:
                    self._process_seen.pop(pid, None)
                    self._process_starts.pop(pid, None)
            self._sampler.retain_processes({proc.pid for proc in procs}, now)
            self._session_totals = self._sampler.totals
            valid = self._rates_source is not None or (not self._ss_failed and self._sampler.primed)
            self._history.record(time.monotonic(), {proc.pid for proc in procs}, usage if valid else None)
            await self._apply_rules(procs)
            self._push_caps()
            self._render()
            if self._status_text.startswith("refresh failed:"):
                self._note("ready")
            self._report_flow_warning()
        except Exception as exc:  # one bad scan must not stop the clock
            message = f"refresh failed: {exc}"
            if message != self._status_text:
                self._note(message, bad=True)
        finally:
            self._busy = False

    async def _apply_rules(self, procs: list[ProcessInfo]) -> None:
        if not self.is_root or self.shaper is None:
            return
        now = time.monotonic()
        current = self._clock()
        shares = self.shaper.shares
        limits = {pid: (share.down_pct, share.up_pct, share.revision) for pid, share in shares.items()}
        for proc, revision in self._scheduled.expired(procs, self._rules, limits, current, now):
            try:
                message = await asyncio.to_thread(self.shaper.remove, proc.pid, expected_revision=revision)
            except (ShaperError, OSError) as exc:
                self._scheduled.failed(proc, time.monotonic())
                self._note(f"scheduled limit for pid {proc.pid}: {exc}", bad=True)
            else:
                self._scheduled.forget(proc)
                self._rule_tracker.released(proc)
                self._note(f"schedule ended: {message}")
        candidates = self._rule_tracker.candidates(procs, self._rules, set(self.shaper.shares), now, current)
        for proc, rule in candidates:
            if proc.pid in self.shaper.shares:
                self._rule_tracker.applied(proc)
                continue
            down_cap, up_cap = self._effective_caps()
            try:
                message, revision = await asyncio.to_thread(
                    self.shaper.apply_tracked, proc.pid, rule.down_pct, rule.up_pct, down_cap, up_cap,
                    proc.cmdline, expected_start_ticks=proc.start_ticks, only_if_unlimited=True,
                )
            except (ShaperError, OSError) as exc:
                if proc.pid in self.shaper.shares:
                    self._rule_tracker.applied(proc)
                    continue
                self._rule_tracker.failed(proc, time.monotonic())
                self._note(f"auto rule for pid {proc.pid}: {exc}", bad=True)
            else:
                self._rule_tracker.applied(proc)
                self._scheduled.applied(proc, rule, revision)
                self._pushed_caps = (down_cap, up_cap)
                self._note(f"auto rule: {message}")

    def _report_flow_warning(self) -> None:
        warning = getattr(self.shaper, "flow_warning", None) if self.shaper else None
        if warning:
            if warning != self._flow_warning:
                self._flow_warning = warning
                self._note(warning, bad=True)
            return
        previous = self._flow_warning
        self._flow_warning = None
        if previous and self._status_text == previous:
            self._note("ready")

    def _load_procs(self) -> list[ProcessInfo]:
        if self._process_source is not None:
            return list(self._process_source())
        return scan_processes()

    def _load_usage(self) -> dict[int, tuple[float, float]]:
        if self._rates_source is not None:
            return {int(pid): (float(down), float(up)) for pid, (down, up) in self._rates_source().items()}
        snapshot = read_ss()
        if snapshot is None:
            self._ss_failed = True
            return {}
        self._ss_failed = False
        return self._sampler.rates(snapshot, time.monotonic())

    def _resolve_iface(self) -> None:
        if self._iface_arg:
            self.iface = self._iface_arg
        elif not self.iface:
            try:
                self.iface = default_iface()
            except IfaceError as exc:
                self.iface = ""
                self._note(str(exc), bad=True)
        if self._link_bps_arg is _MISSING:
            self._detected_bps = link_speed_bps(self.iface) if self.iface else None
        else:
            self._detected_bps = self._link_bps_arg  # type: ignore[assignment]

    def _effective_caps(self) -> tuple[int | None, int | None]:
        down = self._down_budget or self._detected_bps
        up = self._up_budget or self._detected_bps
        return down, up

    def _commit_budgets(self, *, announce: bool) -> bool:
        try:
            down = parse_budget(self.query_one("#down-budget", Input).value, self.unit)
            up = parse_budget(self.query_one("#up-budget", Input).value, self.unit)
        except RateError as exc:
            self._note(str(exc), bad=True)
            return False
        self._down_budget = down
        self._up_budget = up
        self._push_caps()
        if announce:
            self._note("capacity updated")
            self._render()
        return True

    def _write_budget_inputs(self) -> None:
        down = self.query_one("#down-budget", Input)
        up = self.query_one("#up-budget", Input)
        down.value = "" if self._down_budget is None else format_budget_input(self._down_budget, self.unit)
        up.value = "" if self._up_budget is None else format_budget_input(self._up_budget, self.unit)

    def _push_caps(self) -> None:
        if self.shaper is None or not self.shaper.shares:
            return
        caps = self._effective_caps()
        if caps == self._pushed_caps:
            return
        try:
            self.shaper.set_capacities(*caps)
        except ShaperError as exc:
            self._note(str(exc), bad=True)
            return
        self._pushed_caps = caps

    def _render(self) -> None:
        if not self.is_mounted:
            return
        width, height = self._dims()
        too_small = width < _MIN_WIDTH or height < _MIN_HEIGHT
        self.screen.set_class(too_small, "-too-small")
        self.screen.set_class(width < 80, "-narrow")
        self.screen.set_class(width < _STACK_BELOW, "-stacked")
        self.screen.set_class(height < _COMPACT_BELOW, "-compact")
        self.screen.set_class(height < _SHORT_BELOW, "-short")
        self.screen.set_class(height >= _TALL_FROM, "-tall")
        self.query_one(Footer).compact = width < 100
        if too_small:
            self.query_one("#too-small", Static).update(
                "This terminal is too small.\n\n"
                f"bandctl needs {_MIN_WIDTH} columns and {_MIN_HEIGHT} rows.\n"
                "Make the window bigger."
            )
        self._paint_header()
        self._render_table()
        self._paint_detail()

    def _paint_header(self) -> None:
        root = "root" if self.is_root else "read-only"
        iface = self.iface or "no interface"
        if self._detected_bps:
            link = format_rate(self._detected_bps, self.unit)
        else:
            link = "unknown"
        width, _height = self._dims()
        if width < 80:
            text = f"{iface}  {root}  {self.unit.value}  link {link}"
        else:
            text = (
                f"bandctl  {iface}  {root}  {self.unit.value}  "
                f"link {link}  sort {_SORT_LABEL[self._sort]}"
            )
            if not self._show_all:
                text += "  above 1%"
        self.query_one("#titlebar", Static).update(text)
        down_cap, up_cap = self._effective_caps()
        down_label = _cap_label("down", down_cap, self._down_budget, self.unit)
        up_label = _cap_label("up", up_cap, self._up_budget, self.unit)
        gap = "\n" if width < _STACK_BELOW else "    "
        self.query_one("#speed", Static).update(down_label + gap + up_label)

    def _column_plan(self) -> list[tuple[str, str, int]]:
        width, _height = self._dims()
        return fit_columns(width)

    def _visible(self) -> list[ProcessInfo]:
        query = self.query_one("#filter", Input).value.strip().lower()
        rows = []
        for proc in self._procs:
            # A search looks through every process, not just the busy ones,
            # so an idle process can still be found by name or pid.
            if not query and not self._show_all and not self._keep_row(proc):
                continue
            if query and not _matches(proc, query):
                continue
            rows.append(proc)
        rows.sort(key=self._sort_key)
        return rows

    def _sort_key(self, proc: ProcessInfo):
        down, up = self._usage.get(proc.pid, (0.0, 0.0))
        if self._sort == "pid":
            return (proc.pid,)
        if self._sort == "down":
            return (-down, proc.pid)
        if self._sort == "up":
            return (-up, proc.pid)
        return (0 if proc.sockets else 1, proc.comm.lower(), proc.pid)

    def _denoms(self) -> tuple[float, float]:
        """Link capacity, or the measured total when the link speed is unknown."""
        down_cap, up_cap = self._effective_caps()
        if down_cap and up_cap:
            return float(down_cap), float(up_cap)
        total_down = sum(pair[0] for pair in self._usage.values())
        total_up = sum(pair[1] for pair in self._usage.values())
        return (
            float(down_cap) if down_cap else total_down,
            float(up_cap) if up_cap else total_up,
        )

    def _keep_row(self, proc: ProcessInfo) -> bool:
        share = self.shaper.shares.get(proc.pid) if self.shaper else None
        if share and (share.down_pct or share.up_pct):
            return True
        down, up = self._usage.get(proc.pid, (0.0, 0.0))
        down_den, up_den = self._denoms()
        return above_share(down, up, down_den, up_den)

    def _render_table(self) -> None:
        table = self.query_one("#procs", DataTable)
        columns = self._column_plan()
        keys = tuple(key for key, _label, _width in columns)
        widths = tuple(width for _key, _label, width in columns)
        for key, _label, width in columns:
            if key == "command":
                self._command_width = width
        rows = self._visible()
        pids = [proc.pid for proc in rows]
        if keys != self._cols or widths != self._col_widths or pids != self._shown:
            table.clear(columns=True)
            for key, label, width in columns:
                table.add_column(label, width=width, key=key)
            self._cols = keys
            self._col_widths = widths
            for proc in rows:
                table.add_row(*[self._cell(proc, key) for key, _label, _width in columns], key=str(proc.pid))
            self._shown = pids
            if self._selected not in pids:
                self._selected = pids[0] if pids else None
            if self._selected is not None:
                table.move_cursor(row=table.get_row_index(str(self._selected)), animate=False)
        else:
            for proc in rows:
                for key, _label, _width in columns:
                    table.update_cell(str(proc.pid), key, self._cell(proc, key))

    def _cell(self, proc: ProcessInfo, key: str):
        share = self.shaper.shares.get(proc.pid) if self.shaper else None
        limited = bool(share and (share.down_pct or share.up_pct))
        down_bps, up_bps = self._usage.get(proc.pid, (0.0, 0.0))
        if key == "pid":
            text = str(proc.pid)
        elif key == "command":
            text = _clip(proc.cmdline, self._command_width)
        elif key == "user":
            text = proc.user
        elif key == "sockets":
            text = str(proc.sockets)
        elif key == "down_pct":
            text = "-" if not share or share.down_pct is None else str(share.down_pct)
        elif key == "up_pct":
            text = "-" if not share or share.up_pct is None else str(share.up_pct)
        elif key == "down_rate":
            text = format_rate(down_bps, self.unit) if down_bps > 0 or (share and share.down_pct) else "-"
        elif key == "total":
            total = self._session_totals.get(proc.pid)
            text = format_bytes(sum(total)) if total is not None else "-"
        else:
            text = format_rate(up_bps, self.unit) if up_bps > 0 or (share and share.up_pct) else "-"
        # The selected row is painted by the green cursor. Orange text on
        # that green is hard to read, so only the other limited rows use it.
        if limited and proc.pid != self._selected:
            return Text(str(text), style="#f0a35e")
        return text

    def _fill_inputs(self, pid: int) -> None:
        focused = self.focused
        if focused in (self.query_one("#down-pct"), self.query_one("#up-pct")):
            return
        share = self.shaper.shares.get(pid) if self.shaper else None
        self.query_one("#down-pct", Input).value = "" if not share or share.down_pct is None else str(share.down_pct)
        self.query_one("#up-pct", Input).value = "" if not share or share.up_pct is None else str(share.up_pct)

    def _paint_detail(self) -> None:
        proc = self._proc(self._selected) if self._selected is not None else None
        if proc is None and self._selected is None:
            query = self.query_one("#filter", Input).value.strip()
            if query:
                title = f"No process matches {query}"
            elif not self._show_all and self._rates_source is None and not self._sampler.primed:
                title = "Measuring traffic..."
            elif not self._show_all:
                title = "Nothing above 1%. Press n to list every process."
            else:
                title = "Select a process"
        elif proc is None:
            title = f"pid {self._selected} is gone"
        else:
            total = self._session_totals.get(proc.pid)
            data = format_bytes(sum(total)) if total is not None else "--"
            share = self.shaper.shares.get(proc.pid) if self.shaper else None
            group = f"  group {share.group}" if share and share.group else ""
            title = f"{proc.comm}  pid {proc.pid}{group}  TCP total {data}"
        self.query_one("#detail-title", Static).update(title)
        share = self.shaper.shares.get(self._selected) if self.shaper and self._selected else None
        usage = self._usage.get(self._selected, (0.0, 0.0)) if self._selected else (0.0, 0.0)
        down_cap, up_cap = self._effective_caps()
        self._paint_bar("down", None if not share else share.down_pct, usage[0], down_cap)
        self._paint_bar("up", None if not share else share.up_pct, usage[1], up_cap)
        for direction, kind in enumerate(("down", "up")):
            graph = self.query_one(f"#{kind}-history", Static)
            graph.update(self._history.graph(self._selected, direction, time.monotonic(), graph.content_size.width))

    def _paint_bar(self, kind: str, percent: int | None, usage_bps: float, cap: int | None) -> None:
        bar = self.query_one(f"#{kind}-bar", ProgressBar)
        caption = self.query_one(f"#{kind}-caption", Static)
        if percent and cap:
            ceil = percent_to_bps(percent, cap)
            frac = 0.0 if ceil <= 0 else min(100.0, usage_bps / ceil * 100.0)
            bar.update(total=100, progress=frac)
            bar.set_class(frac >= 95, "hot")
            current = format_rate(usage_bps, self.unit)
            ceiling = format_rate(ceil, self.unit)
            if self._dims()[0] < 80:
                current = current.removesuffix(f" {self.unit.value}")
                caption.update(f"{kind} {current}/{ceiling} {percent}%")
            else:
                caption.update(f"{kind}  {current} / {ceiling}  ({percent}%)")
        else:
            bar.update(total=100, progress=0)
            bar.remove_class("hot")
            caption.update(f"{kind}  {format_rate(usage_bps, self.unit)}  no cap")

    def _over_commit(self) -> str | None:
        if self.shaper is None:
            return None
        shares = list({share.minor: share for share in self.shaper.shares.values()}.values())
        messages = [
            over_commit_message("download", [share.down_pct for share in shares if share.down_pct]),
            over_commit_message("upload", [share.up_pct for share in shares if share.up_pct]),
        ]
        parts = [message for message in messages if message]
        return " ".join(parts) if parts else None

    def _proc(self, pid: int | None) -> ProcessInfo | None:
        if pid is None:
            return None
        for proc in self._procs:
            if proc.pid == pid:
                return proc
        return None

    def _note(self, text: str, *, bad: bool = False) -> None:
        self._status_text = text
        self._status_bad = bad
        if self.is_mounted:
            self._paint_status()
            self._log(text)
        else:
            self._status_text = text

    def _paint_status(self) -> None:
        color = "#f0a35e" if self._status_bad else "#ffffff"
        try:
            status = self.query_one("#status", Static)
        except NoMatches:  # the screen is closing
            return
        status.update(Text(self._status_text, style=color))

    def _log(self, text: str) -> None:
        if not self.is_mounted:
            return
        stamp = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#log", RichLog)
        except NoMatches:  # the screen is closing
            return
        log.write(f"{stamp}  {text}")


def fit_columns(table_width: int) -> list[tuple[str, str, int]]:
    """Column keys, headers, and content widths that fit in ``table_width``.

    One column is reserved for the scrollbar. The command column takes whatever
    is left. The total, user, and socket columns are dropped as needed.
    """
    budget = max(table_width - 1, 1)
    chosen = list(_COLUMNS)

    def overhead(columns: list[tuple[str, str, int, bool]], command_width: int) -> int:
        return sum(width + _COL_PAD for _key, _label, width, _optional in columns) + command_width + _COL_PAD

    while overhead(chosen, 12) > budget:
        optional = [index for index, item in enumerate(chosen) if item[3]]
        if not optional:
            break
        total = next((index for index in optional if chosen[index][0] == "total"), None)
        chosen.pop(total if total is not None else optional[0])
    if budget - overhead(chosen, 0) < _COMMAND_MIN:
        chosen = [
            (key, *_TIGHT[key], optional) if key in _TIGHT else (key, label, width, optional)
            for key, label, width, optional in chosen
        ]
    command_width = budget - overhead(chosen, 0)
    if command_width < 4:
        command_width = 4
    fitted: list[tuple[str, str, int]] = []
    for key, label, width, _optional in chosen:
        fitted.append((key, label, width))
        if key == "pid":
            fitted.append(("command", "Command", command_width))
    return fitted


def _clip(text: str, width: int) -> str:
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _matches(proc: ProcessInfo, query: str) -> bool:
    haystack = f"{proc.comm} {proc.cmdline} {proc.user} {proc.pid}".lower()
    return query in haystack


def _cap_label(kind: str, cap: int | None, budget: int | None, unit: Unit) -> str:
    if cap is None:
        shown = "unset"
    else:
        shown = format_rate(cap, unit)
    source = "budget" if budget else ("link" if cap else "unset")
    return f"{kind} {shown} ({source})"
