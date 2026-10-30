"""Editor for named, shared process limits."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from bandctl.groups import Group, active_groups
from bandctl.rates import RateError, parse_percent
from bandctl.shaper import ShaperError


class GroupsScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, shaper, processes, down_cap, up_cap):
        super().__init__()
        self.shaper = shaper
        self.processes = {proc.pid: proc for proc in processes}
        self.down_cap, self.up_cap = down_cap, up_cap
        self.changed = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Shared limits", id="group-title"),
            Static("Create: name, PIDs, shares. Add: name, PID.", id="group-help"),
            DataTable(id="group-table", cursor_type="row"),
            Horizontal(Input(placeholder="group name", id="group-name"),
                       Input(placeholder="PIDs (comma list)", id="group-pids"), id="group-fields"),
            Horizontal(Input(placeholder="down %", id="group-down"),
                       Input(placeholder="up %", id="group-up"), id="group-shares"),
            Static("", id="group-status"),
            Horizontal(Button("Create", id="group-create"), Button("Update", id="group-update"),
                       Button("Add", id="group-add"), Button("Close", id="group-close"), id="group-buttons"),
            id="group-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#group-table", DataTable)
        for title, width in (("Name", 10), ("Down", 4), ("Up", 4), ("PIDs", 14)):
            table.add_column(title, width=width)
        self._render_groups()
        self.query_one("#group-name", Input).focus()

    def _render_groups(self) -> None:
        table = self.query_one("#group-table", DataTable)
        table.clear()
        for group in active_groups(self.shaper.shares):
            table.add_row(Text(group.name), str(group.down_pct or "-"), str(group.up_pct or "-"),
                          ", ".join(str(pid) for pid in group.members))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        groups = active_groups(self.shaper.shares)
        if event.cursor_row >= len(groups):
            return
        group = groups[event.cursor_row]
        self.query_one("#group-name", Input).value = group.name
        self.query_one("#group-down", Input).value = str(group.down_pct or "")
        self.query_one("#group-up", Input).value = str(group.up_pct or "")
        self.query_one("#group-pids", Input).value = ""
        self._status("Enter a PID to add, or change shares and choose Update")

    def _status(self, message: str, bad: bool = False) -> None:
        status = self.query_one("#group-status", Static)
        status.set_class(bad, "-error")
        status.update(Text(message))

    def _pids(self) -> tuple[int, ...]:
        raw = self.query_one("#group-pids", Input).value
        parts = [part.strip() for part in raw.split(",")]
        if not parts or any(not part.isascii() or not part.isdigit() for part in parts):
            raise ValueError("enter PIDs separated by commas")
        return tuple(int(part) for part in parts)

    def _start(self, pid: int) -> int:
        proc = self.processes.get(pid)
        if proc is None or proc.start_ticks is None:
            raise ValueError(f"cannot verify pid {pid}; close and rescan")
        return proc.start_ticks

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action == "group-close":
            self.action_close()
            return
        name = self.query_one("#group-name", Input).value
        before = {pid: (share.revision, share.minor) for pid, share in self.shaper.shares.items()}
        try:
            if action == "group-add":
                pids = self._pids()
                if len(pids) != 1:
                    raise ValueError("enter one PID to add")
                self.shaper.add_group_member(name, pids[0], expected_start_ticks=self._start(pids[0]))
            else:
                down = parse_percent(self.query_one("#group-down", Input).value, allow_empty=True)
                up = parse_percent(self.query_one("#group-up", Input).value, allow_empty=True)
                if action == "group-create":
                    group = Group(name, down, up, self._pids())
                    starts = {pid: self._start(pid) for pid in group.members}
                    self.shaper.apply_group(group, starts, self.down_cap, self.up_cap)
                elif action == "group-update":
                    self.shaper.update_group(name, down, up, self.down_cap, self.up_cap)
                else:
                    return
        except (ValueError, RateError, ShaperError) as exc:
            self._render_groups()
            after = {pid: (share.revision, share.minor) for pid, share in self.shaper.shares.items()}
            self.changed = self.changed or before != after
            self._status(str(exc), bad=True)
            return
        self.changed = True
        self._render_groups()
        self._status("Group updated")

    def action_close(self) -> None:
        self.dismiss(self.changed)
