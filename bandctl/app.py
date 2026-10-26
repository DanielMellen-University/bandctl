"""Textual UI for per-process download and upload shares."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Input, Label, ProgressBar, RichLog, Static
from textual.widgets.data_table import RowDoesNotExist

from bandctl.iface import IfaceError, default_iface, link_speed_bps
from bandctl.procscan import ProcessInfo, scan_processes
from bandctl.rates import (
    RateError,
    Unit,
    format_budget_input,
    format_rate,
    over_commit_message,
    parse_budget,
    parse_percent,
    percent_to_bps,
)
from bandctl.shaper import Shaper, ShaperError
from bandctl.traffic import TrafficSampler, above_share, read_ss
from bandctl.widgets import RootRequired

_MISSING = object()

_COLUMNS_WIDE = (
    ("pid", "PID"),
    ("command", "Command"),
    ("user", "User"),
    ("sockets", "Sockets"),
    ("down_pct", "Down %"),
    ("up_pct", "Up %"),
    ("down_rate", "Down"),
    ("up_rate", "Up"),
)
_COLUMNS_NARROW = (
    ("pid", "PID"),
    ("command", "Command"),
    ("down_pct", "Down %"),
    ("up_pct", "Up %"),
    ("down_rate", "Down"),
    ("up_rate", "Up"),
)
_SORTS = ("name", "pid", "down", "up")
_SORT_LABEL = {"name": "name", "pid": "pid", "down": "download", "up": "upload"}


class BandctlApp(App):
    """Process table, percent fields, live bars, and an error log."""

    CSS_PATH = Path(__file__).with_name("theme.tcss")
    TITLE = "bandctl"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_filter", "Search", key_display="/"),
        Binding("u", "remove_limit", "Remove"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("n", "toggle_all", "All"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("b", "toggle_unit", "Unit"),
        Binding("a", "apply_limit", "Apply"),
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
    ) -> None:
        super().__init__()
        self._iface_arg = iface
        self._shaper_arg = shaper
        self._process_source = processes
        self._rates_source = rates
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
        self._selected: int | None = None
        self._shown: list[int] = []
        self._cols: tuple[str, ...] = ()
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
                Static("download  no cap", id="down-caption"),
                ProgressBar(total=100, show_eta=False, show_percentage=False, id="down-bar"),
                Static("upload  no cap", id="up-caption"),
                ProgressBar(total=100, show_eta=False, show_percentage=False, id="up-bar"),
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

    def on_resize(self, event) -> None:
        # The Resize message carries the new size. self.size can still be the
        # previous one while this handler runs.
        size = getattr(event, "size", None)
        if size is not None:
            self._width = size.width
            self._height = size.height
        if self.is_mounted:
            self._render()

    def _on_screen_size(self, _old, new) -> None:
        self._width = new.width
        self._height = new.height
        if self.is_mounted:
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
        if event.key == "enter" and self.focused is self.query_one("#procs", DataTable):
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
        self.query_one("#down-pct", Input).value = ""
        self.query_one("#up-pct", Input).value = ""
        self._note(message)
        self._render()

    async def _refresh(self) -> None:
        if self._busy:
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
        narrow = width < 88
        cramped = height < 28
        self.screen.set_class(narrow, "-narrow")
        self.screen.set_class(cramped, "-cramped")
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
        text = (
            f"bandctl  {iface}  {root}  {self.unit.value}  "
            f"link {link}  sort {_SORT_LABEL[self._sort]}"
        )
        if not self._show_all:
            text += "  above 1%"
        self.query_one("#titlebar", Static).update(text)
        down_cap, up_cap = self._effective_caps()
        self.query_one("#speed", Static).update(
            _cap_label("down", down_cap, self._down_budget, self.unit)
            + "    "
            + _cap_label("up", up_cap, self._up_budget, self.unit)
        )

    def _want_columns(self) -> tuple[tuple[str, str], ...]:
        width, _height = self._dims()
        if width < 88:
            return _COLUMNS_NARROW
        return _COLUMNS_WIDE

    def _visible(self) -> list[ProcessInfo]:
        query = self.query_one("#filter", Input).value.strip().lower()
        rows = []
        for proc in self._procs:
            if not self._show_all and not self._keep_row(proc):
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
        columns = self._want_columns()
        keys = tuple(key for key, _label in columns)
        rows = self._visible()
        pids = [proc.pid for proc in rows]
        if keys != self._cols or pids != self._shown:
            table.clear(columns=True)
            for key, label in columns:
                table.add_column(label, key=key)
            self._cols = keys
            for proc in rows:
                table.add_row(*[self._cell(proc, key) for key, _label in columns], key=str(proc.pid))
            self._shown = pids
            if self._selected not in pids:
                self._selected = pids[0] if pids else None
            if self._selected is not None:
                table.move_cursor(row=table.get_row_index(str(self._selected)), animate=False)
        else:
            for proc in rows:
                for key, _label in columns:
                    table.update_cell(str(proc.pid), key, self._cell(proc, key))

    def _cell(self, proc: ProcessInfo, key: str):
        share = self.shaper.shares.get(proc.pid) if self.shaper else None
        limited = bool(share and (share.down_pct or share.up_pct))
        down_bps, up_bps = self._usage.get(proc.pid, (0.0, 0.0))
        if key == "pid":
            text = str(proc.pid)
        elif key == "command":
            text = proc.cmdline
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
            if not self._show_all and self._rates_source is None and not self._sampler.primed:
                title = "Measuring traffic..."
            elif not self._show_all:
                title = "Nothing above 1%. Press n to list every process."
            else:
                title = "Select a process"
        elif proc is None:
            title = f"pid {self._selected} is gone"
        else:
            title = f"{proc.comm}  pid {proc.pid}"
        self.query_one("#detail-title", Static).update(title)
        share = self.shaper.shares.get(self._selected) if self.shaper and self._selected else None
        usage = self._usage.get(self._selected, (0.0, 0.0)) if self._selected else (0.0, 0.0)
        down_cap, up_cap = self._effective_caps()
        self._paint_bar("down", None if not share else share.down_pct, usage[0], down_cap)
        self._paint_bar("up", None if not share else share.up_pct, usage[1], up_cap)

    def _paint_bar(self, kind: str, percent: int | None, usage_bps: float, cap: int | None) -> None:
        bar = self.query_one(f"#{kind}-bar", ProgressBar)
        caption = self.query_one(f"#{kind}-caption", Static)
        if percent and cap:
            ceil = percent_to_bps(percent, cap)
            frac = 0.0 if ceil <= 0 else min(100.0, usage_bps / ceil * 100.0)
            bar.update(total=100, progress=frac)
            bar.set_class(frac >= 95, "hot")
            caption.update(
                f"{kind}  {format_rate(usage_bps, self.unit)} / {format_rate(ceil, self.unit)}  ({percent}%)"
            )
        else:
            bar.update(total=100, progress=0)
            bar.remove_class("hot")
            caption.update(f"{kind}  {format_rate(usage_bps, self.unit)}  no cap")

    def _over_commit(self) -> str | None:
        if self.shaper is None:
            return None
        shares = list(self.shaper.shares.values())
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
        self.query_one("#status", Static).update(Text(self._status_text, style=color))

    def _log(self, text: str) -> None:
        if not self.is_mounted:
            return
        stamp = time.strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"{stamp}  {text}")


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
