"""Screens that are not the main process view."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from bandctl.rates import RateError, parse_percent
from bandctl.rules import AutoRule
from bandctl.schedule import DailyWindow, parse_time


class RootRequired(ModalScreen[None]):
    """Shown once at startup when the process is not root."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("bandctl needs root to set limits.", id="root-title"),
            Static(
                "You can view processes and traffic. "
                "Limits are off, and no tc rules are installed.",
                id="root-body",
            ),
            Static(self.command, id="root-command", markup=False),
            Button("View processes", id="root-ok"),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "root-ok":
            self.dismiss(None)


class RulesScreen(ModalScreen[list[AutoRule]]):
    """Edit session rules, in order of precedence."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, rules: list[AutoRule]) -> None:
        super().__init__()
        self.rules = list(rules)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("Auto-apply rules (session)", id="rules-title"),
            Static("Times optional. First match wins.", id="rules-help"),
            DataTable(id="rule-table", cursor_type="row"),
            Horizontal(
                Input(placeholder="name contains", id="rule-name"),
                Input(placeholder="down %", id="rule-down"),
                Input(placeholder="up %", id="rule-up"),
                id="rule-fields",
            ),
            Horizontal(
                Input(placeholder="start HH:MM", id="rule-start"),
                Input(placeholder="end HH:MM", id="rule-end"),
                id="rule-times",
            ),
            Static("", id="rule-status"),
            Horizontal(
                Button("Add", id="rule-add"),
                Button("Delete", id="rule-delete"),
                Button("Close", id="rule-close"),
                id="rule-buttons",
            ),
            id="rules-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#rule-table", DataTable)
        table.add_column("Name contains", width=16)
        table.add_column("Down", width=5)
        table.add_column("Up", width=5)
        table.add_column("Daily", width=11)
        self._render_rules()
        self.query_one("#rule-name", Input).focus()

    def _render_rules(self) -> None:
        table = self.query_one("#rule-table", DataTable)
        table.clear()
        for rule in self.rules:
            window = "-" if rule.window is None else f"{rule.window.start:%H:%M}-{rule.window.end:%H:%M}"
            table.add_row(Text(rule.name), str(rule.down_pct or "-"), str(rule.up_pct or "-"), window)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in any rule field adds the rule, like Enter in a percent field
        # on the main screen applies a limit.
        event.stop()
        self._add_rule()

    def _status(self, text: str, *, bad: bool = False) -> None:
        status = self.query_one("#rule-status", Static)
        status.set_class(bad, "-error")
        status.update(Text(text))

    def _add_rule(self) -> None:
        try:
            down = parse_percent(self.query_one("#rule-down", Input).value, allow_empty=True)
            up = parse_percent(self.query_one("#rule-up", Input).value, allow_empty=True)
            start = self.query_one("#rule-start", Input).value.strip()
            end = self.query_one("#rule-end", Input).value.strip()
            if bool(start) != bool(end):
                raise ValueError("enter both times or leave both blank")
            window = DailyWindow(parse_time(start), parse_time(end)) if start else None
            rule = AutoRule(self.query_one("#rule-name", Input).value, down, up, window)
        except (RateError, ValueError) as exc:
            self._status(str(exc), bad=True)
            return
        self.rules.append(rule)
        self._render_rules()
        for query in ("#rule-name", "#rule-down", "#rule-up", "#rule-start", "#rule-end"):
            self.query_one(query, Input).value = ""
        self._status("Rule added")
        self.query_one("#rule-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rule-add":
            self._add_rule()
        elif event.button.id == "rule-delete":
            table = self.query_one("#rule-table", DataTable)
            if self.rules:
                self.rules.pop(table.cursor_row)
                self._render_rules()
                self._status("Rule deleted")
        elif event.button.id == "rule-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss(self.rules)
